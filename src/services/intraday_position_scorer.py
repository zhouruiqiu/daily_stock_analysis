# -*- coding: utf-8 -*-
"""Deterministic intraday technical and position-aware scoring."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class IntradayPositionScore:
    score: int
    recommendation: str
    risk_level: str
    is_held: bool
    technical_score: int
    position_score: Optional[int] = None
    avg_cost: Optional[float] = None
    pnl_pct: Optional[float] = None
    defense_price: Optional[float] = None
    nearest_support: Optional[float] = None
    support_breached: bool = False
    support_break_pct: Optional[float] = None
    positive_reasons: List[str] = field(default_factory=list)
    negative_reasons: List[str] = field(default_factory=list)


def normalize_position_symbol(symbol: Any) -> str:
    value = str(symbol or "").strip().upper()
    if value.startswith(("SH", "SZ")) and value[2:].isdigit():
        value = value[2:]
    if value.endswith((".SH", ".SZ")):
        value = value[:-3]
    return value


class IntradayPositionScorer:
    """Score an intraday snapshot without an LLM."""

    def score(
        self,
        trend: Any,
        quote: Any,
        position: Optional[Dict[str, Any]] = None,
    ) -> IntradayPositionScore:
        technical, positive, negative = self._technical_score(trend, quote)
        is_held = bool(position and float(position.get("quantity") or 0) > 0)

        if not is_held:
            score = round(technical / 70 * 100)
            return IntradayPositionScore(
                score=self._clamp(score),
                recommendation=self._observation_recommendation(score),
                risk_level=self._risk_level(score),
                is_held=False,
                technical_score=technical,
                positive_reasons=positive[:3],
                negative_reasons=negative[:3],
            )

        position_points, avg_cost, pnl_pct, defense, pos_good, pos_bad = (
            self._position_score(trend, quote, position or {})
        )
        nearest_support, support_breached, support_break_pct = self._support_breach(
            trend,
            quote,
        )
        score = self._clamp(technical + position_points)
        return IntradayPositionScore(
            score=score,
            recommendation=self._hold_recommendation(score),
            risk_level=self._risk_level(score),
            is_held=True,
            technical_score=technical,
            position_score=position_points,
            avg_cost=avg_cost,
            pnl_pct=pnl_pct,
            defense_price=defense,
            nearest_support=nearest_support,
            support_breached=support_breached,
            support_break_pct=support_break_pct,
            positive_reasons=(positive + pos_good)[:3],
            negative_reasons=(negative + pos_bad)[:3],
        )

    def _technical_score(self, trend: Any, quote: Any) -> Tuple[int, List[str], List[str]]:
        score = 35
        good: List[str] = []
        bad: List[str] = []
        trend_value = self._value(getattr(trend, "trend_status", None))
        if "强势多头" in trend_value:
            score += 15
            good.append("强势多头排列")
        elif "多头" in trend_value:
            score += 10
            good.append("均线保持多头")
        elif "强势空头" in trend_value:
            score -= 18
            bad.append("强势空头排列")
        elif "空头" in trend_value:
            score -= 12
            bad.append("均线处于空头")

        macd = self._value(getattr(trend, "macd_status", None))
        if macd in {"零轴上金叉", "金叉", "多头", "上穿零轴"}:
            score += 10
            good.append(f"MACD {macd}")
        elif macd in {"死叉", "空头", "下穿零轴"}:
            score -= 10
            bad.append(f"MACD {macd}")

        rsi = self._value(getattr(trend, "rsi_status", None))
        if rsi == "强势买入":
            score += 5
            good.append("RSI 动能健康")
        elif rsi == "超买":
            score -= 5
            bad.append("RSI 超买")
        elif rsi == "弱势":
            score -= 5
            bad.append("RSI 偏弱")
        elif rsi == "超卖":
            score -= 3
            bad.append("RSI 超卖，趋势仍弱")

        volume = self._value(getattr(trend, "volume_status", None))
        if volume in {"放量上涨", "缩量回调"}:
            score += 5
            good.append(volume)
        elif volume == "放量下跌":
            score -= 8
            bad.append("放量下跌")

        bias = self._float(getattr(trend, "bias_ma5", None))
        if bias is not None and abs(bias) <= 3:
            score += 5
            good.append("价格贴近 MA5")
        elif bias is not None and bias > 7:
            score -= 7
            bad.append("短线乖离过大")
        elif bias is not None and bias < -5:
            score -= 5
            bad.append("跌破 MA5 较远")

        change_pct = self._float(getattr(quote, "change_pct", None))
        if change_pct is not None and change_pct <= -4:
            score -= 5
            bad.append("盘中跌幅较大")
        return max(0, min(70, round(score))), good, bad

    def _position_score(
        self, trend: Any, quote: Any, position: Dict[str, Any]
    ) -> Tuple[int, Optional[float], Optional[float], Optional[float], List[str], List[str]]:
        score = 15
        good: List[str] = []
        bad: List[str] = []
        avg_cost = self._float(position.get("avg_cost"))
        price = self._float(getattr(quote, "price", None)) or self._float(
            getattr(trend, "current_price", None)
        )
        pnl_pct = None
        if avg_cost and price is not None:
            pnl_pct = round((price - avg_cost) / avg_cost * 100, 2)
        elif position.get("unrealized_pnl_pct") is not None:
            pnl_pct = self._float(position.get("unrealized_pnl_pct"))

        if pnl_pct is None:
            bad.append("成本或现价缺失")
        elif pnl_pct >= 10:
            score += 10
            good.append("持仓浮盈较厚")
        elif pnl_pct >= 3:
            score += 7
            good.append("持仓处于浮盈")
        elif pnl_pct >= -3:
            score += 3
            good.append("价格接近持仓成本")
        elif pnl_pct > -8:
            score -= 5
            bad.append("持仓出现中等浮亏")
        else:
            score -= 12
            bad.append("持仓浮亏较大")

        defense_candidates = [
            self._float(value)
            for value in list(getattr(trend, "support_levels", []) or [])[:2]
        ]
        defense_candidates = [value for value in defense_candidates if value and value > 0]
        if avg_cost and avg_cost > 0:
            defense_candidates.append(avg_cost * 0.95)
        defense = round(max(defense_candidates), 2) if defense_candidates else None
        if defense is not None and price is not None and price < defense:
            score -= 5
            bad.append("现价跌破防守位")
        return max(0, min(30, round(score))), avg_cost, pnl_pct, defense, good, bad

    def _support_breach(
        self,
        trend: Any,
        quote: Any,
    ) -> Tuple[Optional[float], bool, Optional[float]]:
        supports = [
            self._float(value)
            for value in list(getattr(trend, "support_levels", []) or [])[:2]
        ]
        supports = [value for value in supports if value is not None and value > 0]
        nearest_support = round(max(supports), 2) if supports else None
        realtime_price = self._float(getattr(quote, "price", None))
        if (
            nearest_support is None
            or realtime_price is None
            or realtime_price <= 0
            or realtime_price >= nearest_support
        ):
            return nearest_support, False, None
        break_pct = round(
            (nearest_support - realtime_price) / nearest_support * 100,
            2,
        )
        return nearest_support, True, break_pct

    @staticmethod
    def _hold_recommendation(score: int) -> str:
        if score >= 80:
            return "强势持有"
        if score >= 65:
            return "继续持有"
        if score >= 50:
            return "谨慎持有"
        if score >= 35:
            return "考虑减仓"
        return "高风险"

    @staticmethod
    def _observation_recommendation(score: int) -> str:
        if score >= 80:
            return "强势观察"
        if score >= 65:
            return "积极观察"
        if score >= 50:
            return "中性观察"
        if score >= 35:
            return "谨慎观察"
        return "风险观察"

    @staticmethod
    def _risk_level(score: int) -> str:
        if score >= 70:
            return "低"
        if score >= 50:
            return "中"
        return "高"

    @staticmethod
    def _value(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clamp(score: Any) -> int:
        return max(0, min(100, round(float(score))))
