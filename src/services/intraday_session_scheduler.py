# -*- coding: utf-8 -*-
"""Wall-clock coordinator for A-share intraday monitoring tasks."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, time
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from src.core.trading_calendar import MarketPhase
from src.services.intraday_market_monitor import MarketTrendState

logger = logging.getLogger(__name__)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ACTIVE_PHASES = {MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION}
_OPENING_SCREENING_TIME = "09:45"
_OPENING_SCREENING_STRATEGY = "dragon_board"


def intraday_capabilities_enabled(config: Any) -> bool:
    return bool(
        getattr(config, "intraday_watch_enabled", False)
        or getattr(config, "intraday_market_monitor_enabled", False)
        or getattr(config, "intraday_screening_enabled", False)
        or getattr(config, "strategy_evaluation_enabled", False)
        or getattr(config, "notification_digest_enabled", False)
    )


class IntradaySessionCoordinator:
    """Claim deterministic wall-clock slots and launch the corresponding work."""

    def __init__(
        self,
        *,
        config_provider: Optional[Callable[[], Any]] = None,
        market_monitor: Optional[Any] = None,
        screening_worker: Optional[Any] = None,
        watch_worker: Optional[Any] = None,
        phase_provider: Optional[Callable[[datetime], MarketPhase]] = None,
        task_launcher: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.config_provider = config_provider or self._default_config_provider
        self.market_monitor = market_monitor
        self.screening_worker = screening_worker
        self.watch_worker = watch_worker
        self.phase_provider = phase_provider or self._default_phase_provider
        self.task_launcher = task_launcher or self._launch_daemon
        self._claimed_slots: set[str] = set()
        self._claim_date: Optional[str] = None
        self._screening_running = False
        self._watch_running = False
        self._strategy_evaluation_scheduler = None
        self._running_lock = threading.Lock()

    @staticmethod
    def _default_config_provider():
        from src.config import get_config

        return get_config()

    @staticmethod
    def _default_phase_provider(now: datetime) -> MarketPhase:
        from src.core.trading_calendar import infer_market_phase

        return infer_market_phase("cn", current_time=now)

    @staticmethod
    def _launch_daemon(task: Callable[[], None]) -> None:
        threading.Thread(target=task, daemon=True).start()

    @staticmethod
    def _local_now(value: Optional[datetime]) -> datetime:
        current = value or datetime.now(_SHANGHAI)
        if current.tzinfo is None:
            return current.replace(tzinfo=_SHANGHAI)
        return current.astimezone(_SHANGHAI)

    def _ensure_components(self, config: Any) -> None:
        needs_market = bool(
            getattr(config, "intraday_market_monitor_enabled", False)
            or getattr(config, "intraday_screening_enabled", False)
        )
        if needs_market and self.market_monitor is None:
            from src.services.intraday_market_monitor import IntradayMarketMonitor

            self.market_monitor = IntradayMarketMonitor(
                threshold_pct=getattr(config, "intraday_market_trend_threshold_pct", 0.35),
                drop_alert_pct=getattr(config, "intraday_market_drop_alert_pct", 1.5),
                notifications_enabled=getattr(
                    config, "intraday_market_monitor_enabled", False
                ),
            )
        elif self.market_monitor is not None:
            threshold = getattr(config, "intraday_market_trend_threshold_pct", None)
            if threshold is not None and hasattr(self.market_monitor, "threshold_pct"):
                self.market_monitor.threshold_pct = max(float(threshold), 0.0)
            drop_threshold = getattr(config, "intraday_market_drop_alert_pct", None)
            if drop_threshold is not None and hasattr(self.market_monitor, "drop_alert_pct"):
                self.market_monitor.drop_alert_pct = max(float(drop_threshold), 0.0)
            if hasattr(self.market_monitor, "notifications_enabled"):
                self.market_monitor.notifications_enabled = bool(
                    getattr(config, "intraday_market_monitor_enabled", False)
                )
        if getattr(config, "intraday_screening_enabled", False) and self.screening_worker is None:
            from src.services.intraday_screening_worker import IntradayScreeningWorker

            self.screening_worker = IntradayScreeningWorker(config_provider=self.config_provider)
        if getattr(config, "intraday_watch_enabled", False) and self.watch_worker is None:
            from src.services.intraday_watch_worker import IntradayWatchWorker

            self.watch_worker = IntradayWatchWorker(config_provider=self.config_provider)

    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        current = self._local_now(now)
        config = self.config_provider()
        result: Dict[str, Any] = {"executed": [], "phase": "unknown"}
        if not intraday_capabilities_enabled(config):
            return result
        if getattr(config, "strategy_evaluation_enabled", False):
            if self._strategy_evaluation_scheduler is None:
                from src.services.strategy_evaluation_scheduler import StrategyEvaluationScheduler
                self._strategy_evaluation_scheduler = StrategyEvaluationScheduler(config_provider=self.config_provider)
            evaluation_result = self._strategy_evaluation_scheduler.tick(current)
            result["executed"].extend(evaluation_result.get("executed", []))
        self._ensure_components(config)

        phase = self.phase_provider(current)
        result["phase"] = getattr(phase, "value", str(phase))
        if phase in {MarketPhase.NON_TRADING, MarketPhase.UNKNOWN, MarketPhase.PREMARKET}:
            return result

        date_key = current.date().isoformat()
        if date_key != self._claim_date:
            self._claim_date = date_key
            self._claimed_slots.clear()

        market_enabled = bool(getattr(config, "intraday_market_monitor_enabled", False))
        screening_enabled = bool(getattr(config, "intraday_screening_enabled", False))
        market_due = (market_enabled or screening_enabled) and self._is_interval_slot(
            current,
            max(1, int(getattr(config, "intraday_market_monitor_interval_minutes", 10) or 10)),
        )
        market_state = getattr(
            self.market_monitor,
            "current_state",
            MarketTrendState.NEUTRAL,
        )
        if market_due and self._phase_allows_slot(phase, current):
            key = self._slot_key("market", current)
            if self._claim(key):
                try:
                    snapshot = self.market_monitor.run_once(current)
                    market_state = getattr(snapshot, "state", market_state)
                    result["executed"].append("market")
                except Exception as exc:  # noqa: BLE001 - one sample must not stop later tasks.
                    logger.exception("[IntradaySession] market sample failed: %s", exc)

        screening_times = set(getattr(config, "intraday_screening_times", None) or ["10:00", "14:00"])
        current_time = current.strftime("%H:%M")
        opening_screening_due = screening_enabled and current_time == _OPENING_SCREENING_TIME
        screening_due = screening_enabled and current_time in screening_times
        if (opening_screening_due or screening_due) and phase in _ACTIVE_PHASES:
            key = self._slot_key("screening", current)
            strategy = _OPENING_SCREENING_STRATEGY if opening_screening_due else None
            if self._claim(key) and self._launch_screening(
                market_state,
                current,
                strategy=strategy,
            ):
                result["executed"].append("screening")

        watch_due = bool(getattr(config, "intraday_watch_enabled", False)) and self._is_interval_slot(
            current,
            max(1, int(getattr(config, "intraday_watch_interval_minutes", 30) or 30)),
        )
        if watch_due and self._phase_allows_slot(phase, current):
            key = self._slot_key("watch", current)
            if self._claim(key) and self._launch_watch(current):
                result["executed"].append("watch")

        digest_times = set(
            getattr(config, "notification_digest_times", None) or ["10:05", "11:35", "15:15"]
        )
        if bool(getattr(config, "notification_digest_enabled", False)) and current_time in digest_times:
            key = self._slot_key("digest", current)
            if self._claim(key):
                try:
                    from src.services.notification_digest_service import (
                        NotificationDigestService,
                    )
                    from src.storage import DatabaseManager

                    digest = NotificationDigestService(
                        DatabaseManager(),
                        max_events=int(
                            getattr(config, "notification_digest_max_events", 30) or 30
                        ),
                    )
                    stats = digest.flush(slot=current_time, now=current)
                    if stats.get("event_count"):
                        result["executed"].append("digest")
                        logger.info(
                            "[IntradaySession] %s 简报已处理 %s 条事件（sent=%s）",
                            current_time,
                            stats.get("event_count"),
                            stats.get("sent"),
                        )
                except Exception as exc:  # noqa: BLE001 - 简报失败不影响盘中任务。
                    logger.exception("[IntradaySession] digest flush failed: %s", exc)
        return result

    @staticmethod
    def _is_interval_slot(current: datetime, interval_minutes: int) -> bool:
        clock = current.time().replace(tzinfo=None)
        if time(9, 30) <= clock <= time(11, 30):
            elapsed = (current.hour * 60 + current.minute) - (9 * 60 + 30)
        elif time(13, 30) <= clock <= time(15, 0):
            elapsed = (current.hour * 60 + current.minute) - (13 * 60 + 30)
        else:
            return False
        return elapsed % interval_minutes == 0

    @staticmethod
    def _phase_allows_slot(phase: MarketPhase, current: datetime) -> bool:
        if phase in _ACTIVE_PHASES:
            return True
        clock = (current.hour, current.minute)
        return (
            phase is MarketPhase.LUNCH_BREAK and clock == (11, 30)
        ) or (
            phase is MarketPhase.POSTMARKET and clock == (15, 0)
        )

    @staticmethod
    def _slot_key(task_name: str, current: datetime) -> str:
        return f"{current.strftime('%Y-%m-%d %H:%M')}:{task_name}"

    def _claim(self, key: str) -> bool:
        if key in self._claimed_slots:
            return False
        self._claimed_slots.add(key)
        return True

    def _launch_screening(
        self,
        state: MarketTrendState,
        current: datetime,
        *,
        strategy: Optional[str] = None,
    ) -> bool:
        with self._running_lock:
            if self._screening_running:
                logger.warning("[IntradaySession] screening still running; skip slot %s", current)
                return False
            self._screening_running = True

        def run() -> None:
            try:
                self.screening_worker.run_once(state, current, strategy=strategy)
            finally:
                with self._running_lock:
                    self._screening_running = False

        self.task_launcher(run)
        return True

    def _launch_watch(self, current: datetime) -> bool:
        with self._running_lock:
            if self._watch_running:
                logger.warning("[IntradaySession] watch still running; skip slot %s", current)
                return False
            self._watch_running = True

        def run() -> None:
            try:
                self.watch_worker.run_once(now=current)
            finally:
                with self._running_lock:
                    self._watch_running = False

        self.task_launcher(run)
        return True
