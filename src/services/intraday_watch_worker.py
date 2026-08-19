# -*- coding: utf-8 -*-
"""
盘内盯盘后台任务（轻量、不调 LLM）

职责：在 A 股（或配置的市场）开盘时段内，每隔 N 分钟对 STOCK_LIST 持仓做一次
纯技术面快照（趋势/均线/乖离/MACD/RSI/量能/支撑压力），拼成简短 markdown 简报，
通过通知服务推送到已配置渠道（推荐 wechat_work_app → 个人微信）。

与主分析流程（run_full_analysis）的区别：
- 不调 LLM、不抓新闻、不生成完整报告，只复用 StockTrendAnalyzer（纯 pandas）
- 自带「盘中时段」门控（AlertWorker 没有），非盘中/午休/节假日直接跳过

照抄 AlertWorker 的自包含 try/except 结构，单次失败不会杀死调度后台线程。
"""
import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# 盘中阶段中文标签（用于简报抬头显示）
_PHASE_LABEL = {
    "intraday": "盘中",
    "closing_auction": "尾盘竞价",
    "lunch_break": "午休",
    "premarket": "盘前",
    "postmarket": "盘后",
    "non_trading": "休市",
    "unknown": "未知",
}

# 每只持仓日K回看天数（算 MA60 / MACD / RSI 足够）
_DAILY_LOOKBACK_DAYS = 120

_MARKET_LABEL = {"cn": "A股", "hk": "港股", "us": "美股"}


class IntradayWatchWorker:
    """盘内每 N 分钟对持仓做轻量技术面盯盘并推送。"""

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        fetcher_manager: Optional[Any] = None,
        notifier: Optional[Any] = None,
        now_provider: Optional[Callable[[], float]] = None,
        position_provider: Optional[Callable[[], Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self._fetcher_manager = fetcher_manager
        self.notifier = notifier
        self.now_provider = now_provider or time.time
        self.position_provider = position_provider or self._default_position_provider

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    def _get_fetcher_manager(self):
        if self._fetcher_manager is None:
            from data_provider.base import DataFetcherManager

            self._fetcher_manager = DataFetcherManager()
        return self._fetcher_manager

    @staticmethod
    def _default_position_provider() -> Dict[str, Dict[str, Any]]:
        from src.services.intraday_position_scorer import normalize_position_symbol
        from src.services.portfolio_service import PortfolioService

        snapshot = PortfolioService().get_portfolio_snapshot(include_realtime=False)
        positions: Dict[str, Dict[str, Any]] = {}
        for account in snapshot.get("accounts", []):
            for position in account.get("positions", []):
                key = normalize_position_symbol(position.get("symbol"))
                if not key:
                    continue
                existing = positions.get(key)
                if existing is None:
                    positions[key] = dict(position)
                    continue
                old_qty = float(existing.get("quantity") or 0)
                new_qty = float(position.get("quantity") or 0)
                total_qty = old_qty + new_qty
                if total_qty <= 0:
                    continue
                existing["avg_cost"] = (
                    float(existing.get("avg_cost") or 0) * old_qty
                    + float(position.get("avg_cost") or 0) * new_qty
                ) / total_qty
                existing["quantity"] = total_qty
        return positions

    def run_once(self, now: Optional[datetime] = None) -> Dict[str, int]:
        """跑一轮盯盘。全程 try/except，单次失败不影响调度线程。"""
        stats = {"checked": 0, "analyzed": 0, "notified": 0, "skipped": 0, "failed": 0}

        try:
            config = self.config_provider()
        except Exception as exc:
            logger.warning("[IntradayWatch] 加载配置失败: %s", exc)
            return stats

        if not getattr(config, "intraday_watch_enabled", False):
            logger.debug("[IntradayWatch] 未启用 (intraday_watch_enabled=false)，跳过")
            return stats

        # 1) 盘中时段门控（只有 INTRADAY / CLOSING_AUCTION 才真正分析+推送）
        market = (getattr(config, "intraday_watch_market", "cn") or "cn").lower()
        current = now or datetime.now()
        phase = self._current_phase(market, current)
        if phase is None:
            # 判断失败（如 exchange-calendars 未装），保守跳过，避免非盘中打扰
            logger.info("[IntradayWatch] 市场阶段判断失败，跳过本轮")
            stats["skipped"] = 1
            return stats
        from src.core.trading_calendar import MarketPhase

        boundary_slot = market == "cn" and (
            (phase is MarketPhase.LUNCH_BREAK and (current.hour, current.minute) == (11, 30))
            or (phase is MarketPhase.POSTMARKET and (current.hour, current.minute) == (15, 0))
        )
        if phase not in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION) and not boundary_slot:
            logger.info("[IntradayWatch] 当前非 %s 盘中（%s），跳过", market, phase.value)
            stats["skipped"] = 1
            return stats

        # 2) 持仓代码（STOCK_LIST）
        stock_codes: List[str] = list(getattr(config, "stock_list", []) or [])
        if not stock_codes:
            logger.info("[IntradayWatch] STOCK_LIST 为空，跳过")
            stats["skipped"] = 1
            return stats
        stats["checked"] = len(stock_codes)

        # 3) 逐只分析（不调 LLM）
        from src.stock_analyzer import StockTrendAnalyzer

        analyzer = StockTrendAnalyzer()
        fetcher = self._get_fetcher_manager()
        positions: Dict[str, Dict[str, Any]] = {}
        try:
            positions = self.position_provider()
        except Exception as exc:
            logger.warning("[IntradayWatch] 读取持仓失败，降级为自选技术观察: %s", exc)
        items: List[Dict[str, Any]] = []
        for code in stock_codes:
            try:
                item = self._analyze_one(analyzer, fetcher, code, positions)
                if item is not None:
                    items.append(item)
                    stats["analyzed"] += 1
            except Exception as exc:
                logger.warning("[IntradayWatch] 分析 %s 失败: %s", code, exc)
                stats["failed"] += 1

        if not items:
            logger.info("[IntradayWatch] 本轮无可用分析结果，跳过推送")
            return stats

        # 4) 拼简报 + 推送
        try:
            content = self._format_intraday_watch(market, phase, items, now=current)
            if self._notify(content):
                stats["notified"] = 1
                logger.info("[IntradayWatch] 盯盘简报已推送（%d/%d 只）", len(items), stats["checked"])
        except Exception as exc:
            logger.error("[IntradayWatch] 推送失败: %s", exc)

        return stats

    # ---- 内部 ----

    def _current_phase(self, market: str, current: Optional[datetime] = None):
        """返回当前市场阶段（MarketPhase 枚举）；判断失败返回 None。"""
        try:
            from src.core.trading_calendar import infer_market_phase

            return infer_market_phase(market, current_time=current)
        except Exception as exc:
            logger.warning("[IntradayWatch] infer_market_phase(%s) 失败: %s", market, exc)
            return None

    def _analyze_one(
        self,
        analyzer,
        fetcher,
        code: str,
        positions: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """单只持仓：取日K → 技术面；再取实时盘口。任一失败返回 None。"""
        df, _source = fetcher.get_daily_data(code, days=_DAILY_LOOKBACK_DAYS)
        if df is None or getattr(df, "empty", True) or len(df) < 20:
            logger.warning("[IntradayWatch] %s 日K数据不足（<20 行），跳过", code)
            return None

        trend = analyzer.analyze(df, code)
        quote = None
        try:
            quote = fetcher.get_realtime_quote(code, log_final_failure=False)
        except Exception as exc:
            logger.debug("[IntradayWatch] %s 实时行情获取失败: %s", code, exc)
        from src.services.intraday_position_scorer import (
            IntradayPositionScorer,
            normalize_position_symbol,
        )

        position = (positions or {}).get(normalize_position_symbol(code))
        score = IntradayPositionScorer().score(trend, quote, position)
        return {
            "code": code,
            "trend": trend,
            "quote": quote,
            "position": position,
            "score": score,
        }

    def _format_intraday_watch(
        self,
        market: str,
        phase: str,
        items: List[Dict[str, Any]],
        *,
        now: Optional[datetime] = None,
    ) -> str:
        """拼盯盘简报（纯文本 + emoji，适配企微应用消息 text 模式 / 微信阅读）。"""
        now_str = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
        label = _MARKET_LABEL.get(market, market.upper())
        phase_label = _PHASE_LABEL.get(getattr(phase, "value", ""), str(phase))
        lines: List[str] = [
            f"📊 {label}盯盘速报",
            f"⏰ {now_str} · {phase_label} · 共 {len(items)} 只",
            "━" * 16,
            "",
        ]
        ordered_items = sorted(
            items,
            key=lambda item: not bool(
                getattr(item.get("score"), "support_breached", False)
            ),
        )
        for item in ordered_items:
            lines.append(self._format_one_stock(item))
            lines.append("")
        return "\n".join(lines).rstrip()

    def _format_one_stock(self, item: Dict[str, Any]) -> str:
        code = item["code"]
        trend = item["trend"]
        quote = item["quote"]

        name = getattr(quote, "name", None) or code
        # 现价优先实时行情，回落到趋势分析里的收盘价
        price = getattr(quote, "price", None)
        if price is None:
            price = getattr(trend, "current_price", None)

        change_pct = getattr(quote, "change_pct", None)
        change_amount = getattr(quote, "change_amount", None)
        change_str = self._fmt_change(change_pct, change_amount)

        ma_alignment = getattr(trend, "ma_alignment", "") or ""
        bias_ma5 = getattr(trend, "bias_ma5", None)
        ma5 = getattr(trend, "ma5", None)
        ma10 = getattr(trend, "ma10", None)
        ma20 = getattr(trend, "ma20", None)

        macd_status = self._enum_value(getattr(trend, "macd_status", None))
        rsi6 = getattr(trend, "rsi_6", None)
        rsi_status = self._enum_value(getattr(trend, "rsi_status", None))
        vol_status = self._enum_value(getattr(trend, "volume_status", None))
        volume_ratio = getattr(quote, "volume_ratio", None)
        turnover = getattr(quote, "turnover_rate", None)

        support = self._fmt_levels(getattr(trend, "support_levels", []))
        resistance = self._fmt_levels(getattr(trend, "resistance_levels", []))

        score = item.get("score")
        breached = bool(getattr(score, "support_breached", False))
        parts: List[str] = [
            f"🚨【跌破支撑】{name} {code}" if breached else f"{name} {code}"
        ]
        if score is not None:
            if breached:
                parts.append(
                    f"⚠️ 现价 {_fmt_num(price)} < 最近支撑 "
                    f"{_fmt_num(score.nearest_support)}（跌破 "
                    f"{_fmt_num(score.support_break_pct, '.2f')}%）"
                )
            identity = "持仓" if score.is_held else "自选观察"
            parts.append(
                f"综合评分 {score.score}/100 · {identity} · "
                f"{score.recommendation} · 风险{score.risk_level}"
            )
            if score.is_held:
                position_bits = []
                if score.avg_cost is not None:
                    position_bits.append(f"成本{_fmt_num(score.avg_cost)}")
                if score.pnl_pct is not None:
                    position_bits.append(f"浮盈亏{_fmt_num(score.pnl_pct, '+.2f')}%")
                if score.defense_price is not None:
                    position_bits.append(f"防守位{_fmt_num(score.defense_price)}")
                if position_bits:
                    parts.append(" ".join(position_bits))
            if score.positive_reasons:
                parts.append("加分：" + "、".join(score.positive_reasons))
            if score.negative_reasons:
                parts.append("风险：" + "、".join(score.negative_reasons))
        # 价格行
        price_line = f"现价 {_fmt_num(price)} {change_str}".rstrip()
        parts.append(price_line)
        # 均线行
        ma_bits = []
        if ma_alignment:
            ma_bits.append(ma_alignment)
        ma_vals = []
        for label, val in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20)):
            if val is not None:
                ma_vals.append(f"{label}={_fmt_num(val)}")
        if ma_vals:
            ma_bits.append("/".join(ma_vals))
        if bias_ma5 is not None:
            ma_bits.append(f"乖离{_fmt_num(bias_ma5, '+.2f')}%")
        if ma_bits:
            parts.append(" ".join(ma_bits))
        # 指标行
        ind_bits = []
        if macd_status:
            ind_bits.append(f"MACD {macd_status}")
        if rsi6 is not None:
            ind_bits.append(f"RSI6={_fmt_num(rsi6, '.0f')}")
        if rsi_status:
            ind_bits.append(rsi_status)
        if volume_ratio is not None:
            ind_bits.append(f"量比{_fmt_num(volume_ratio, '.2f')}")
        if turnover is not None:
            ind_bits.append(f"换手{_fmt_num(turnover, '.2f')}%")
        if vol_status:
            ind_bits.append(vol_status)
        if ind_bits:
            parts.append(" ".join(ind_bits))
        # 支撑压力行
        if support or resistance:
            parts.append(f"支撑{support or '-'} / 压力{resistance or '-'}")
        return "\n".join(parts)

    def _notify(self, content: str) -> bool:
        """通过 NotificationService 推送到 alert 路由（用户配 NOTIFICATION_ALERT_CHANNELS）。"""
        from src.notification import NotificationService

        notifier = self.notifier or NotificationService()
        result = notifier.send_with_results(content, route_type="alert")
        return bool(getattr(result, "success", False))

    # ---- 格式化小工具 ----

    @staticmethod
    def _enum_value(val: Any) -> str:
        if val is None:
            return ""
        return getattr(val, "value", None) or str(val)

    @staticmethod
    def _fmt_levels(levels: Any) -> str:
        if not levels:
            return ""
        try:
            return "/".join(_fmt_num(v) for v in levels[:2])
        except Exception:
            return ""

    @staticmethod
    def _fmt_change(change_pct: Any, change_amount: Any) -> str:
        bits = []
        if change_pct is not None:
            arrow = "🔺" if change_pct >= 0 else "🔻"
            bits.append(f"{arrow}{_fmt_num(change_pct, '+.2f')}%")
        if change_amount is not None:
            bits.append(f"({_fmt_num(change_amount, '+.2f')})")
        return " ".join(bits)


def _fmt_num(val: Any, spec: str = ".2f") -> str:
    """安全格式化数值；None / 非数返回 '-'。"""
    try:
        return format(float(val), spec)
    except (TypeError, ValueError):
        return "-"


def run_intraday_watch_loop(
    *,
    config_provider: Optional[Callable[[], Any]] = None,
    worker_factory: Callable[..., IntradayWatchWorker] = IntradayWatchWorker,
    coordinator_factory: Optional[Callable[..., Any]] = None,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """Poll the wall-clock intraday coordinator until the process is stopped."""
    provider = config_provider or IntradayWatchWorker._default_config_provider
    config = provider()
    from src.services.intraday_session_scheduler import (
        IntradaySessionCoordinator,
        intraday_capabilities_enabled,
    )

    if not intraday_capabilities_enabled(config):
        logger.error(
            "[IntradayWatch] 未启用任何盘中任务，请检查 INTRADAY_WATCH_ENABLED、"
            "INTRADAY_MARKET_MONITOR_ENABLED 或 INTRADAY_SCREENING_ENABLED"
        )
        return 2

    stopper = stop_event or threading.Event()
    coordinator_cls = coordinator_factory or IntradaySessionCoordinator
    coordinator_kwargs: Dict[str, Any] = {"config_provider": provider}
    if getattr(config, "intraday_watch_enabled", False):
        coordinator_kwargs["watch_worker"] = worker_factory(config_provider=provider)
    coordinator = coordinator_cls(**coordinator_kwargs)

    logger.info(
        "[IntradayWatch] 盘中墙钟调度已启动；每30秒检查一次到期时间槽"
    )
    while not stopper.is_set():
        result = coordinator.tick()
        executed = result.get("executed") or []
        if executed:
            logger.info("[IntradayWatch] 已触发时间槽任务: %s", ",".join(executed))
        if stopper.wait(30):
            break

    logger.info("[IntradayWatch] 独立盯盘已停止")
    return 0
