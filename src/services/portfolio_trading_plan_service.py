# -*- coding: utf-8 -*-
"""组合交易计划服务（Task 5/6/7）。

规则化风险管理引擎：基于持仓快照、大盘状态、实时行情与技术支撑位，
输出每只持仓的 hold/observe/reduce/exit/add_if_confirmed/blocked 建议
与组合层仓位约束。只生成计划，不自动下单、不承诺收益。

红线（实施计划第 19 节）：
- 不得输出无条件"买入"；
- 数据缺失/过期时降级为 observe 并标注 data_quality，不给确定性建议；
- 禁止仅因亏损而补仓；
- 峰值净值持久化，服务重启不重置。
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from src.schemas.trading_plan import (
    DrawdownRiskState,
    PortfolioTradingPlan,
    RiskProfile,
    TradingAction,
    TradingPlanItem,
)

logger = logging.getLogger(__name__)

_DAILY_LOOKBACK_DAYS = 120
_TRAILING_STOP_WINDOW = 10          # 近 N 日最低价作为移动止损参考
_HIGH_VOL_PREFIXES = ("300", "301", "302", "688", "689")  # 创业板/科创

_DISCLAIMER = "规则化风险管理计划，不构成收益保证或自动交易指令"


def classify_volatility_tier(stock_code: str) -> str:
    """按代码前缀判定波动档位（v1 启发式：创业板/科创=高波动）。"""
    code = str(stock_code or "").strip().upper()
    bare = code.split(".")[0]
    for prefix in ("SH", "SZ", "BJ"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]
            break
    if bare.startswith(_HIGH_VOL_PREFIXES):
        return "high_vol"
    return "normal"


def floor_to_lot(quantity: float, lot_size: int = 100) -> int:
    """向下取整到 A 股交易单位。"""
    if quantity <= 0:
        return 0
    return int(math.floor(quantity / lot_size) * lot_size)


class PortfolioTradingPlanService:
    def __init__(
        self,
        db_manager: Any,
        *,
        portfolio_service: Optional[Any] = None,
        risk_profile: Optional[RiskProfile] = None,
        data_fetcher: Optional[Any] = None,
        trend_analyzer: Optional[Any] = None,
    ):
        self.db = db_manager
        self._portfolio_service = portfolio_service
        self.risk_profile = risk_profile or RiskProfile()
        self._data_fetcher = data_fetcher
        self._trend_analyzer = trend_analyzer

    # ---- 对外入口 ----

    def build_plan(
        self,
        *,
        account_id: Optional[int] = None,
        market_state: str = "neutral",
        now: Optional[datetime] = None,
    ) -> PortfolioTradingPlan:
        generated_at = now or datetime.now()
        snapshot = self._get_portfolio_service().get_portfolio_snapshot(account_id=account_id)

        account = self._pick_account(snapshot, account_id)
        equity = float(account.get("total_equity") or 0.0)
        invested = float(account.get("total_market_value") or 0.0)
        cash = float(account.get("total_cash") or 0.0)
        resolved_account_id = int(account.get("account_id") or 0)

        profile = self.risk_profile
        exposure_pct = self._safe_pct(invested, equity)
        cash_pct = self._safe_pct(cash, equity)
        target_exposure = self._target_exposure(market_state, profile)

        peak_equity, drawdown_pct, risk_state = self._update_risk_state(
            resolved_account_id, equity, generated_at
        )

        limitations: List[str] = [_DISCLAIMER]

        blocking: List[str] = []
        new_positions_allowed = True
        if cash_pct < profile.min_cash_pct * 100:
            new_positions_allowed = False
            blocking.append(
                f"现金比例 {cash_pct:.1f}% 低于 {profile.min_cash_pct:.0%} 下限，优先恢复现金"
            )
        if exposure_pct > target_exposure * 100 + 5:
            new_positions_allowed = False
            blocking.append(
                f"当前仓位 {exposure_pct:.1f}% 高于目标 {target_exposure:.0%}，先降后加"
            )
        if risk_state == DrawdownRiskState.DRAWDOWN_LOCK.value:
            new_positions_allowed = False
            blocking.append("组合处于 8% 回撤锁定状态，禁止新增风险仓位")
        if not new_positions_allowed:
            blocking.append("禁止亏损补仓（红线规则）")

        positions = list(account.get("positions") or [])
        items = [self._build_item(position, equity, risk_state, new_positions_allowed)
                 for position in positions]
        items.sort(key=lambda item: -item.current_weight_pct)

        if not profile.industry_cap:
            pass  # pragma: no cover
        limitations.append("行业集中度上限依赖行业映射，v1 暂不自动判定")

        return PortfolioTradingPlan(
            generated_at=generated_at,
            as_of=str(account.get("as_of") or generated_at.date().isoformat()),
            total_equity=round(equity, 2),
            invested_amount=round(invested, 2),
            cash_amount=round(cash, 2),
            exposure_pct=round(exposure_pct, 2),
            target_exposure_pct=round(target_exposure * 100, 1),
            cash_pct=round(cash_pct, 2),
            peak_equity=round(peak_equity, 2),
            drawdown_pct=round(drawdown_pct, 2),
            risk_state=risk_state,
            new_positions_allowed=new_positions_allowed,
            portfolio_blocking_reasons=blocking,
            items=items,
            limitations=limitations,
        )

    # ---- 单只持仓 ----

    def _build_item(
        self,
        position: Dict[str, Any],
        equity: float,
        risk_state: str,
        new_positions_allowed: bool,
    ) -> TradingPlanItem:
        profile = self.risk_profile
        code = str(position.get("symbol") or "")
        name = str(position.get("name") or "") or code
        quantity = float(position.get("quantity") or 0)
        snapshot_price = float(position.get("last_price") or 0)
        market_value = float(position.get("market_value_base") or 0)
        pnl_pct = position.get("unrealized_pnl_pct")

        trend, quote, quality = self._load_market_context(code)
        price = self._float(getattr(quote, "price", None)) or snapshot_price
        if price <= 0:
            price = snapshot_price

        weight_pct = self._safe_pct(market_value, equity)
        tier = classify_volatility_tier(code)
        cap_pct = self._position_cap(tier) * 100

        supports = [
            self._float(value)
            for value in list(getattr(trend, "support_levels", []) or [])
            if self._float(value) and self._float(value) > 0
        ]
        nearest_support = round(max(supports), 2) if supports else None
        resistances = [
            self._float(value)
            for value in list(getattr(trend, "resistance_levels", []) or [])
            if self._float(value) and self._float(value) > 0
        ]
        take_profits = sorted({round(value, 2) for value in resistances[:2]})

        support_breached = bool(nearest_support and 0 < price < nearest_support)
        break_pct = (
            round((nearest_support - price) / nearest_support * 100, 2)
            if support_breached and nearest_support
            else None
        )
        stop_price = nearest_support
        trailing_stop = self._trailing_stop(code, price)

        cap_amount = equity * self._position_cap(tier)
        excess_amount = market_value - cap_amount
        max_reduce_quantity = (
            floor_to_lot(excess_amount / price) if excess_amount > 0 and price > 0 else 0
        ) or None

        risk_budget = equity * profile.per_trade_risk_pct
        max_additional, entry_condition = self._additional_allowance(
            price=price,
            stop_price=stop_price,
            cap_amount=cap_amount,
            market_value=market_value,
            risk_budget=risk_budget,
        )

        action, reason = self._decide_action(
            weight_pct=weight_pct,
            cap_pct=cap_pct,
            support_breached=support_breached,
            break_pct=break_pct,
            pnl_pct=pnl_pct,
            risk_state=risk_state,
            quality=quality,
        )

        item_block: List[str] = []
        if not new_positions_allowed and max_additional > 0:
            max_additional = 0
            item_block.append("组合层面禁止新增仓位（现金/回撤约束）")
        if pnl_pct is not None and pnl_pct < 0:
            item_block.append("浮亏状态禁止补仓摊薄（红线规则）")

        reduce_condition = None
        exit_condition = None
        if nearest_support:
            reduce_condition = f"收盘价跌破 {nearest_support} 或仓位超过 {cap_pct:.0f}% 上限"
            exit_condition = f"有效跌破 {nearest_support} 超过 3% 且次日无法收回"
        if action == TradingAction.ADD_IF_CONFIRMED.value and not entry_condition:
            entry_condition = "站稳最近支撑且大盘状态改善后分批"

        return TradingPlanItem(
            stock_code=code,
            stock_name=name,
            current_quantity=quantity,
            current_price=round(price, 3),
            current_weight_pct=round(weight_pct, 2),
            position_cap_pct=round(cap_pct, 1),
            volatility_tier=tier,
            action=action,
            action_reason=reason,
            unrealized_pnl_pct=pnl_pct,
            nearest_support=nearest_support,
            support_breached=support_breached,
            support_break_pct=break_pct,
            stop_price=stop_price,
            trailing_stop_price=trailing_stop,
            take_profit_levels=take_profits,
            max_reduce_quantity=max_reduce_quantity,
            max_additional_quantity=max_additional,
            risk_budget_amount=round(risk_budget, 2),
            entry_condition=entry_condition,
            reduce_condition=reduce_condition,
            exit_condition=exit_condition,
            blocking_reasons=item_block,
            data_quality=quality,
        )

    def _decide_action(
        self,
        *,
        weight_pct: float,
        cap_pct: float,
        support_breached: bool,
        break_pct: Optional[float],
        pnl_pct: Optional[float],
        risk_state: str,
        quality: str,
    ) -> Tuple[str, str]:
        if quality == "missing":
            return (
                TradingAction.OBSERVE.value,
                "日K或实时行情缺失，暂不给确定性建议",
            )
        locked = risk_state == DrawdownRiskState.DRAWDOWN_LOCK.value
        if support_breached:
            if break_pct is not None and break_pct >= 3:
                return (
                    TradingAction.EXIT.value,
                    f"实时价跌破有效支撑 {break_pct:.1f}%，按纪律离场等待企稳",
                )
            if pnl_pct is not None and pnl_pct <= -15:
                return (
                    TradingAction.REDUCE.value,
                    f"跌破支撑且浮亏 {pnl_pct:.1f}%，先降仓位控制单票风险",
                )
            return (
                TradingAction.REDUCE.value,
                "实时价跌破最近支撑，降低敞口等待重新站回",
            )
        if weight_pct > cap_pct:
            if locked:
                return (
                    TradingAction.REDUCE.value,
                    f"回撤锁定状态且仓位 {weight_pct:.1f}% 超上限 {cap_pct:.0f}%，必须减仓",
                )
            return (
                TradingAction.REDUCE.value,
                f"仓位 {weight_pct:.1f}% 超出{ '高波动' if cap_pct < 15 else '普通' }单股上限 {cap_pct:.0f}%，降低集中度",
            )
        if locked:
            return (
                TradingAction.OBSERVE.value,
                "组合回撤锁定中，仅保留趋势与支撑仍有效的仓位",
            )
        if weight_pct >= cap_pct * 0.85:
            return (
                TradingAction.OBSERVE.value,
                f"仓位 {weight_pct:.1f}% 接近上限 {cap_pct:.0f}%，禁止自然加仓",
            )
        if pnl_pct is not None and pnl_pct <= -20 and quality != "ok":
            return (
                TradingAction.OBSERVE.value,
                "深度浮亏且数据质量下降，等待完整数据再决策",
            )
        return (
            TradingAction.HOLD.value,
            f"仓位 {weight_pct:.1f}% 在上限内，支撑 { '有效' if not support_breached else '失效' }，按计划持有",
        )

    def _additional_allowance(
        self,
        *,
        price: float,
        stop_price: Optional[float],
        cap_amount: float,
        market_value: float,
        risk_budget: float,
    ) -> Tuple[int, Optional[str]]:
        profile = self.risk_profile
        if price <= 0:
            return 0, None
        minimum_stop_pct = 0.05
        stop_distance = max(
            price - (stop_price or 0.0),
            price * minimum_stop_pct,
        )
        if stop_distance <= 0:
            return 0, None
        risk_based = floor_to_lot(risk_budget / stop_distance, profile.lot_size)
        cap_based = floor_to_lot(
            (cap_amount - market_value) / price, profile.lot_size
        )
        allowed = max(0, min(risk_based, cap_based))
        if allowed <= 0:
            return 0, None
        condition = (
            f"确认条件：站稳支撑 {stop_price} 且大盘非下跌状态，单笔风险 ≤ "
            f"{risk_budget:.0f} 元（止损距离 {stop_distance:.2f} 元）"
            if stop_price
            else "确认条件：大盘非下跌状态且止损位明确后分批"
        )
        return allowed, condition

    # ---- 状态机（Task 7）----

    def _update_risk_state(
        self,
        account_id: int,
        equity: float,
        now: datetime,
    ) -> Tuple[float, float, str]:
        profile = self.risk_profile
        from src.storage import PortfolioRiskState

        with self.db.session_scope() as session:
            row = (
                session.query(PortfolioRiskState)
                .filter(PortfolioRiskState.account_id == account_id)
                .one_or_none()
            )
            peak = float(row.peak_equity) if row else 0.0
            new_peak = max(peak, equity)
            drawdown = (new_peak - equity) / new_peak if new_peak > 0 else 0.0

            if drawdown >= profile.max_drawdown_pct:
                state = DrawdownRiskState.DRAWDOWN_LOCK.value
            elif drawdown >= profile.defensive_drawdown_pct:
                state = DrawdownRiskState.DEFENSIVE.value
            elif drawdown >= profile.caution_drawdown_pct:
                state = DrawdownRiskState.CAUTION.value
            else:
                state = DrawdownRiskState.NORMAL.value

            if row is None:
                session.add(
                    PortfolioRiskState(
                        account_id=account_id,
                        peak_equity=new_peak,
                        risk_state=state,
                        drawdown_pct=drawdown * 100,
                        locked_at=now.replace(tzinfo=None) if state == DrawdownRiskState.DRAWDOWN_LOCK.value else None,
                    )
                )
            else:
                row.peak_equity = new_peak
                row.risk_state = state
                row.drawdown_pct = drawdown * 100
                if state == DrawdownRiskState.DRAWDOWN_LOCK.value and row.locked_at is None:
                    row.locked_at = now.replace(tzinfo=None)

        return new_peak, drawdown * 100, state

    # ---- 数据上下文 ----

    def _load_market_context(self, code: str) -> Tuple[Optional[Any], Optional[Any], str]:
        trend = None
        quote = None
        quality = "ok"
        try:
            fetcher = self._get_data_fetcher()
            df, _source = fetcher.get_daily_data(code, days=_DAILY_LOOKBACK_DAYS)
            if df is not None and not getattr(df, "empty", True) and len(df) >= 20:
                trend = self._get_trend_analyzer().analyze(df, code)
            try:
                quote = fetcher.get_realtime_quote(code, log_final_failure=False)
            except Exception as exc:
                logger.debug("[TradingPlan] %s 实时行情失败: %s", code, exc)
        except Exception as exc:
            logger.warning("[TradingPlan] %s 行情数据失败: %s", code, exc)
        if trend is None:
            quality = "missing" if quote is None else "partial"
        elif quote is None:
            quality = "partial"
        return trend, quote, quality

    def _trailing_stop(self, code: str, fallback_price: float) -> Optional[float]:
        try:
            fetcher = self._get_data_fetcher()
            df, _source = fetcher.get_daily_data(code, days=_TRAILING_STOP_WINDOW + 5)
            if df is None or getattr(df, "empty", True):
                return None
            lows = [self._float(value) for value in df.get("low", [])]
            lows = [value for value in lows if value and value > 0]
            if not lows:
                return None
            return round(min(lows[-_TRAILING_STOP_WINDOW:]), 2)
        except Exception:
            return None

    # ---- 工具 ----

    def _pick_account(self, snapshot: Dict[str, Any], account_id: Optional[int]) -> Dict[str, Any]:
        accounts = list(snapshot.get("accounts") or [])
        if account_id is not None:
            for account in accounts:
                if int(account.get("account_id") or 0) == int(account_id):
                    return account
        if not accounts:
            return {
                "account_id": account_id or 0,
                "as_of": None,
                "total_equity": 0.0,
                "total_market_value": 0.0,
                "total_cash": 0.0,
                "positions": [],
            }
        return max(
            accounts,
            key=lambda account: float(account.get("total_equity") or 0),
        )

    def _get_portfolio_service(self):
        if self._portfolio_service is None:
            from src.services.portfolio_service import PortfolioService

            self._portfolio_service = PortfolioService()
        return self._portfolio_service

    def _get_data_fetcher(self):
        if self._data_fetcher is None:
            from data_provider.base import DataFetcherManager

            self._data_fetcher = DataFetcherManager()
        return self._data_fetcher

    def _get_trend_analyzer(self):
        if self._trend_analyzer is None:
            from src.stock_analyzer import StockTrendAnalyzer

            self._trend_analyzer = StockTrendAnalyzer()
        return self._trend_analyzer

    @staticmethod
    def _target_exposure(market_state: str, profile: RiskProfile) -> float:
        mapping = {
            "sustained_up": profile.target_exposure_sustained_up,
            "neutral": profile.target_exposure_neutral,
            "sustained_down": profile.target_exposure_sustained_down,
        }
        return mapping.get(str(market_state or "neutral"), profile.target_exposure_neutral)

    def _position_cap(self, tier: str) -> float:
        profile = self.risk_profile
        if tier == "high_vol":
            return profile.position_cap_high_vol
        if tier == "defensive":
            return profile.position_cap_defensive
        return profile.position_cap_normal

    @staticmethod
    def _safe_pct(numerator: float, denominator: float) -> float:
        if denominator <= 0:
            return 0.0
        return numerator / denominator * 100

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            result = float(value)
            return result if result == result else None  # NaN guard
        except (TypeError, ValueError):
            return None
