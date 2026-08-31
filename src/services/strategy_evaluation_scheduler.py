# -*- coding: utf-8 -*-
"""Wall-clock scheduler for strategy cohorts and outcome evaluation."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from src.core.trading_calendar import is_market_open
from src.services.strategy_evaluation_service import StrategyEvaluationService
from src.services.strategy_outcome_service import StrategyOutcomeService

logger = logging.getLogger(__name__)

COHORT_SCHEDULE = {
    "09:25": "preopen_previous_close",
    "09:45": "opening_0945",
    "10:00": "intraday_1000",
}


def _launch_daemon(task: Callable[[], None]) -> None:
    threading.Thread(target=task, name="strategy-evaluation-worker", daemon=True).start()


class StrategyEvaluationScheduler:
    def __init__(
        self,
        *,
        config_provider: Callable[[], Any],
        daily_service: Optional[Any] = None,
        outcome_service: Optional[Any] = None,
        notifier: Optional[Any] = None,
        trading_day_provider: Callable[[Any], bool] = lambda day: is_market_open("cn", day),
        task_launcher: Callable[[Callable[[], None]], None] = _launch_daemon,
    ) -> None:
        self.config_provider = config_provider
        self.daily_service = daily_service or StrategyEvaluationService()
        self.outcome_service = outcome_service or StrategyOutcomeService()
        self.notifier = notifier
        self.trading_day_provider = trading_day_provider
        self.task_launcher = task_launcher
        self._claims: set[str] = set()

    def _notify(self, content: str) -> Any:
        notifier = self.notifier
        if notifier is None:
            from src.notification import NotificationService

            notifier = NotificationService()
        return notifier.send_with_results(content, route_type="alert")

    @staticmethod
    def _daily_message(result: dict[str, Any]) -> str:
        labels = {
            "preopen_previous_close": "盘前",
            "opening_0945": "开盘",
            "intraday_1000": "盘中",
        }
        cohort = str(result.get("cohort") or result.get("summary", {}).get("cohort") or "")
        lines = [
            f"🧪 全策略{labels.get(cohort, '')}选股",
            f"状态：{result.get('status')}",
            f"完成：{result.get('completed_strategy_count', 0)}/{result.get('strategy_count', 0)}",
        ]
        for strategy, picks in (result.get("summary", {}).get("strategies", {}) or {}).items():
            codes = "、".join(item.get("stock_code", "") for item in picks)
            lines.append(f"{strategy}：{codes or '-'}")
        return "\n".join(lines)

    def _is_last_trading_day_of_week(self, current_date: Any) -> bool:
        days_until_sunday = 6 - current_date.weekday()
        return not any(
            self.trading_day_provider(current_date + timedelta(days=offset))
            for offset in range(1, days_until_sunday + 1)
        )

    def _run_cohort(self, day: Any, cohort: str, config: Any, errors: list[str]) -> None:
        try:
            result = self.daily_service.run_cohort(day, cohort)
            if getattr(config, "strategy_evaluation_daily_notify", True):
                notification_error = ""
                try:
                    dispatch = self._notify(self._daily_message(result))
                    success = bool(getattr(dispatch, "success", dispatch is True))
                    if not success:
                        notification_error = str(
                            getattr(dispatch, "message", "send failed") or "send failed"
                        )
                except Exception as exc:  # noqa: BLE001 - persist the failed attempt.
                    success = False
                    notification_error = str(exc)
                recorder = getattr(self.daily_service, "record_notification_status", None)
                if callable(recorder):
                    recorder(
                        result,
                        success=success,
                        error=notification_error,
                    )
        except Exception as exc:  # noqa: BLE001 - worker failure must not stop the wall clock.
            logger.exception("[StrategyEvaluation] cohort %s failed: %s", cohort, exc)
            errors.append(f"{cohort}: {exc}")

    def _run_outcomes(self, day: Any, config: Any, errors: list[str]) -> None:
        try:
            self.outcome_service.run_due(
                day,
                horizons=list(getattr(config, "strategy_evaluation_horizons", [1, 3, 5])),
            )
            if (
                getattr(config, "strategy_evaluation_weekly_notify", True)
                and self._is_last_trading_day_of_week(day)
            ):
                lines = ["🏆 全策略周榜"]
                cohort_labels = {
                    "preopen_previous_close": "盘前策略榜",
                    "opening_0945": "开盘策略榜",
                    "intraday_1000": "盘中策略榜",
                }
                for cohort, label in cohort_labels.items():
                    board = self.outcome_service.get_leaderboard(
                        "5d", window=20, cohort=cohort
                    )
                    ranked = [item for item in board.get("items", []) if item.get("rank")]
                    lines.append(f"【{label}】")
                    lines.extend(
                        f"{item['rank']}. {item['strategy']} "
                        f"超额{item['avg_excess_return_pct']:+.2f}%"
                        for item in ranked[:5]
                    )
                    if not ranked:
                        lines.append("样本积累中")
                self._notify("\n".join(lines))
        except Exception as exc:  # noqa: BLE001 - outcome failure must not stop the wall clock.
            logger.exception("[StrategyEvaluation] outcome run failed: %s", exc)
            errors.append(f"outcomes: {exc}")

    def tick(self, now: Optional[datetime] = None) -> dict[str, Any]:
        current = now or datetime.now(ZoneInfo("Asia/Shanghai"))
        config = self.config_provider()
        executed: list[str] = []
        errors: list[str] = []
        if not getattr(config, "strategy_evaluation_enabled", False):
            return {"executed": executed, "errors": errors}
        if not self.trading_day_provider(current.date()):
            return {"executed": executed, "errors": errors}

        time_text = current.strftime("%H:%M")
        cohort_schedule = dict(COHORT_SCHEDULE)
        configured_preopen = str(getattr(config, "strategy_evaluation_time", "09:25"))
        if configured_preopen != "09:25":
            cohort_schedule.pop("09:25")
            cohort_schedule[configured_preopen] = "preopen_previous_close"

        cohort = cohort_schedule.get(time_text)
        if cohort:
            key = f"{current.date()}:{time_text}:{cohort}"
            if key not in self._claims:
                self._claims.add(key)
                self.task_launcher(
                    lambda day=current.date(), selected=cohort: self._run_cohort(
                        day, selected, config, errors
                    )
                )
                executed.append(f"{cohort}_queued")

        if time_text == "15:10":
            key = f"{current.date()}:{time_text}:outcomes"
            if key not in self._claims:
                self._claims.add(key)
                self.task_launcher(lambda day=current.date(): self._run_outcomes(day, config, errors))
                executed.append("outcomes")
        return {"executed": executed, "errors": errors}
