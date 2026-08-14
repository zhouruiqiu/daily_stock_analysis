# -*- coding: utf-8 -*-
"""Run one screening strategy selected from the current market trend state."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from src.services.intraday_market_monitor import MarketTrendState

logger = logging.getLogger(__name__)

_STRATEGY_BY_STATE = {
    MarketTrendState.SUSTAINED_UP: "dragon_board",
    MarketTrendState.SUSTAINED_DOWN: "low_volatility_quality",
    MarketTrendState.NEUTRAL: "shrink_pullback",
}
_STRATEGY_LABEL = {
    "dragon_board": "强势题材",
    "low_volatility_quality": "低波质量",
    "shrink_pullback": "缩量回踩",
}
_STATE_REASON = {
    MarketTrendState.SUSTAINED_UP: "主要指数持续走强，使用进攻型题材策略",
    MarketTrendState.SUSTAINED_DOWN: "主要指数持续走弱，使用防守型低波策略",
    MarketTrendState.NEUTRAL: "大盘未形成明显单边趋势，使用趋势回踩策略",
}


def select_strategy(state: MarketTrendState) -> str:
    try:
        normalized = MarketTrendState(state)
    except ValueError:
        normalized = MarketTrendState.NEUTRAL
    return _STRATEGY_BY_STATE[normalized]


class IntradayScreeningWorker:
    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        screening_service: Optional[Any] = None,
        notifier: Optional[Any] = None,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self._screening_service = screening_service
        self._notifier = notifier

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    def _get_screening_service(self, config: Any):
        if self._screening_service is None:
            from src.services.screening_service import ScreeningService
            from src.storage import DatabaseManager

            self._screening_service = ScreeningService(
                config=config,
                db_manager=DatabaseManager(),
            )
        return self._screening_service

    def _get_notifier(self):
        if self._notifier is None:
            from src.notification import NotificationService

            self._notifier = NotificationService()
        return self._notifier

    def run_once(
        self,
        state: MarketTrendState,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        executed_at = now or datetime.now()
        normalized_state = MarketTrendState(state)
        strategy = select_strategy(normalized_state)
        stats: Dict[str, Any] = {
            "strategy": strategy,
            "selected": 0,
            "notified": 0,
            "failed": 0,
        }
        try:
            config = self.config_provider()
            max_results = max(
                1,
                min(int(getattr(config, "intraday_screening_max_results", 5) or 5), 20),
            )
            result = self._get_screening_service(config).screen(
                strategy=strategy,
                market="cn",
                max_results=max_results,
            )
            candidates = list(result.get("candidates") or [])[:max_results]
            stats["selected"] = len(candidates)
            content = self._format_result(
                state=normalized_state,
                strategy=strategy,
                executed_at=executed_at,
                result=result,
                candidates=candidates,
            )
        except Exception as exc:  # noqa: BLE001 - one failed run must not stop scheduling.
            logger.exception("[IntradayScreening] run failed: %s", exc)
            stats["failed"] = 1
            content = (
                "⚠️ 盘中动态选股执行失败\n"
                f"⏰ {executed_at.strftime('%Y-%m-%d %H:%M')}\n"
                f"策略：{_STRATEGY_LABEL[strategy]}\n"
                f"原因：{exc}"
            )

        try:
            dispatch = self._get_notifier().send_with_results(content, route_type="alert")
            stats["notified"] = int(bool(getattr(dispatch, "success", False)))
        except Exception as exc:  # noqa: BLE001 - notification failure is isolated.
            logger.warning("[IntradayScreening] notification failed: %s", exc)
        return stats

    @staticmethod
    def _format_result(
        *,
        state: MarketTrendState,
        strategy: str,
        executed_at: datetime,
        result: Dict[str, Any],
        candidates: list[Dict[str, Any]],
    ) -> str:
        lines = [
            "🎯 盘中动态选股",
            f"⏰ {executed_at.strftime('%Y-%m-%d %H:%M')}",
            f"大盘状态：{state.value}",
            f"本次策略：{_STRATEGY_LABEL[strategy]}",
            f"选择原因：{_STATE_REASON[state]}",
            "━" * 16,
        ]
        if not candidates:
            lines.append("本次没有符合条件的股票，继续观察。")
        else:
            for index, candidate in enumerate(candidates, start=1):
                code = str(candidate.get("code") or "-")
                name = str(candidate.get("name") or code)
                score = candidate.get("final_score")
                if score is None:
                    score = candidate.get("screen_score")
                try:
                    score_text = f"{float(score):.1f}"
                except (TypeError, ValueError):
                    score_text = "-"
                risk_level = str(candidate.get("risk_level") or "-")
                flags = [str(flag) for flag in candidate.get("risk_flags") or [] if flag]
                risk_text = "、".join(flags[:2]) or risk_level
                lines.append(
                    f"{index}. {name} {code}｜评分{score_text}｜风险{risk_text}"
                )

        source = str(result.get("snapshot_source") or "unknown")
        lines.append(f"数据源：{source}")
        degradation = [str(item) for item in result.get("degradation") or [] if item]
        if any("LLM ranking failed" in item for item in degradation):
            lines.append("降级：LLM不可用，已使用量化分回退")
        elif degradation:
            lines.append("降级提示：" + "；".join(degradation[:2]))
        return "\n".join(lines)
