# -*- coding: utf-8 -*-
"""交易计划 Schema（Task 5/7）。

交易计划系统只生成规则化风险管理建议，不自动下单、不承诺收益。
动作枚举与结构对齐实施计划（2026-08-31）第 10 节。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class TradingAction(str, Enum):
    HOLD = "hold"                    # 持有
    OBSERVE = "observe"              # 观察（数据不足或接近阈值）
    REDUCE = "reduce"                # 建议减仓（给出数量）
    EXIT = "exit"                    # 建议清仓（跌破关键支撑等）
    ADD_IF_CONFIRMED = "add_if_confirmed"  # 确认条件后可加仓（给出最大数量）
    BLOCKED = "blocked"              # 当前禁止该方向动作


class DrawdownRiskState(str, Enum):
    NORMAL = "normal"
    CAUTION = "caution"
    DEFENSIVE = "defensive"
    DRAWDOWN_LOCK = "drawdown_lock"


# 允许对外暴露的动作集合（防止误输出无条件"买入"）
VALID_TRADING_ACTIONS = {item.value for item in TradingAction}


@dataclass(frozen=True)
class RiskProfile:
    """均衡型风险档位（用户 2026-08-31 确认：最大组合回撤 8%）。"""

    name: str = "balanced"
    per_trade_risk_pct: float = 0.008       # 单笔风险预算：净值 0.8%
    daily_loss_circuit_pct: float = 0.015   # 单日亏损熔断 1.5%
    weekly_drawdown_reduce_pct: float = 0.04  # 单周回撤降仓 4%
    max_drawdown_pct: float = 0.08          # 组合最大回撤 8%（锁定线）
    caution_drawdown_pct: float = 0.04      # 警惕线
    defensive_drawdown_pct: float = 0.06    # 防守线
    position_cap_normal: float = 0.15       # 普通单股上限
    position_cap_high_vol: float = 0.10     # 高波动（创业板/科创）上限
    position_cap_defensive: float = 0.20    # 防守型大盘股上限（预留）
    industry_cap: float = 0.30              # 同行业/主题上限（依赖行业映射，v1 暂不自动判定）
    min_cash_pct: float = 0.20              # 最低现金比例
    lot_size: int = 100                     # A 股交易单位
    target_exposure_sustained_up: float = 0.80
    target_exposure_neutral: float = 0.60
    target_exposure_sustained_down: float = 0.40


@dataclass(frozen=True)
class TradingPlanItem:
    stock_code: str
    stock_name: str
    current_quantity: float
    current_price: float
    current_weight_pct: float
    position_cap_pct: float
    volatility_tier: str                     # normal | high_vol | defensive
    action: str
    action_reason: str
    unrealized_pnl_pct: Optional[float] = None
    nearest_support: Optional[float] = None
    support_breached: bool = False
    support_break_pct: Optional[float] = None
    stop_price: Optional[float] = None
    trailing_stop_price: Optional[float] = None
    take_profit_levels: List[float] = field(default_factory=list)
    max_reduce_quantity: Optional[float] = None
    max_additional_quantity: float = 0.0
    risk_budget_amount: float = 0.0
    entry_condition: Optional[str] = None
    reduce_condition: Optional[str] = None
    exit_condition: Optional[str] = None
    blocking_reasons: List[str] = field(default_factory=list)
    data_quality: str = "ok"                 # ok | partial | missing


@dataclass(frozen=True)
class PortfolioTradingPlan:
    generated_at: datetime
    as_of: str
    total_equity: float
    invested_amount: float
    cash_amount: float
    exposure_pct: float
    target_exposure_pct: float
    cash_pct: float
    peak_equity: float
    drawdown_pct: float
    risk_state: str
    new_positions_allowed: bool
    portfolio_blocking_reasons: List[str] = field(default_factory=list)
    items: List[TradingPlanItem] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "generated_at": self.generated_at.isoformat(),
            "as_of": self.as_of,
            "total_equity": self.total_equity,
            "invested_amount": self.invested_amount,
            "cash_amount": self.cash_amount,
            "exposure_pct": self.exposure_pct,
            "target_exposure_pct": self.target_exposure_pct,
            "cash_pct": self.cash_pct,
            "peak_equity": self.peak_equity,
            "drawdown_pct": self.drawdown_pct,
            "risk_state": self.risk_state,
            "new_positions_allowed": self.new_positions_allowed,
            "portfolio_blocking_reasons": list(self.portfolio_blocking_reasons),
            "limitations": list(self.limitations),
            "items": [
                {
                    "stock_code": item.stock_code,
                    "stock_name": item.stock_name,
                    "current_quantity": item.current_quantity,
                    "current_price": item.current_price,
                    "current_weight_pct": item.current_weight_pct,
                    "position_cap_pct": item.position_cap_pct,
                    "volatility_tier": item.volatility_tier,
                    "action": item.action,
                    "action_reason": item.action_reason,
                    "unrealized_pnl_pct": item.unrealized_pnl_pct,
                    "nearest_support": item.nearest_support,
                    "support_breached": item.support_breached,
                    "support_break_pct": item.support_break_pct,
                    "stop_price": item.stop_price,
                    "trailing_stop_price": item.trailing_stop_price,
                    "take_profit_levels": list(item.take_profit_levels),
                    "max_reduce_quantity": item.max_reduce_quantity,
                    "max_additional_quantity": item.max_additional_quantity,
                    "risk_budget_amount": item.risk_budget_amount,
                    "entry_condition": item.entry_condition,
                    "reduce_condition": item.reduce_condition,
                    "exit_condition": item.exit_condition,
                    "blocking_reasons": list(item.blocking_reasons),
                    "data_quality": item.data_quality,
                }
                for item in self.items
            ],
        }
