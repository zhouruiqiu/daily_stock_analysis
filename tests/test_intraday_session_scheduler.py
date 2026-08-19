# -*- coding: utf-8 -*-
"""Tests for wall-clock intraday session scheduling."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from src.config import Config
from src.core.trading_calendar import MarketPhase
from src.services.intraday_market_monitor import MarketTrendState
from src.services.intraday_session_scheduler import IntradaySessionCoordinator


class IntradaySessionConfigTest(TestCase):
    def test_intraday_session_config_parses_runtime_values(self) -> None:
        env = {
            "INTRADAY_MARKET_MONITOR_ENABLED": "true",
            "INTRADAY_MARKET_MONITOR_INTERVAL_MINUTES": "10",
            "INTRADAY_MARKET_TREND_THRESHOLD_PCT": "0.35",
            "INTRADAY_MARKET_DROP_ALERT_PCT": "1.5",
            "INTRADAY_SCREENING_ENABLED": "true",
            "INTRADAY_SCREENING_TIMES": "14:00,10:00,14:00,invalid",
            "INTRADAY_SCREENING_MAX_RESULTS": "5",
            "STRATEGY_EVALUATION_ENABLED": "true",
            "STRATEGY_EVALUATION_TIME": "09:25",
            "STRATEGY_EVALUATION_TOP_N": "5",
            "STRATEGY_EVALUATION_HORIZONS": "5,1,3,3",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.intraday_market_monitor_enabled)
        self.assertEqual(config.intraday_market_monitor_interval_minutes, 10)
        self.assertEqual(config.intraday_market_trend_threshold_pct, 0.35)
        self.assertEqual(config.intraday_market_drop_alert_pct, 1.5)
        self.assertTrue(config.intraday_screening_enabled)
        self.assertEqual(config.intraday_screening_times, ["10:00", "14:00"])
        self.assertEqual(config.intraday_screening_max_results, 5)
        self.assertTrue(config.strategy_evaluation_enabled)
        self.assertEqual(config.strategy_evaluation_time, "09:25")
        self.assertEqual(config.strategy_evaluation_top_n, 5)
        self.assertEqual(config.strategy_evaluation_horizons, [1, 3, 5])


class _MarketMonitor:
    def __init__(self, events, state=MarketTrendState.NEUTRAL):
        self.events = events
        self.current_state = state

    def run_once(self, now):
        self.events.append(("market", now.strftime("%H:%M")))
        return SimpleNamespace(state=self.current_state)


class _ScreeningWorker:
    def __init__(self, events):
        self.events = events

    def run_once(self, state, now, *, strategy=None):
        self.events.append(("screening", now.strftime("%H:%M"), state.value, strategy))
        return {"selected": 1}


class _WatchWorker:
    def __init__(self, events):
        self.events = events

    def run_once(self, now=None):
        self.events.append(("watch", now.strftime("%H:%M")))
        return {"analyzed": 1}


def _runtime_config(**overrides):
    values = {
        "intraday_watch_enabled": True,
        "intraday_watch_interval_minutes": 30,
        "intraday_market_monitor_enabled": True,
        "intraday_market_monitor_interval_minutes": 10,
        "intraday_screening_enabled": True,
        "intraday_screening_times": ["10:00", "14:00"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _coordinator(events, *, config=None, state=MarketTrendState.NEUTRAL, phase=None):
    return IntradaySessionCoordinator(
        config_provider=lambda: config or _runtime_config(),
        market_monitor=_MarketMonitor(events, state=state),
        screening_worker=_ScreeningWorker(events),
        watch_worker=_WatchWorker(events),
        phase_provider=lambda _now: phase or MarketPhase.INTRADAY,
        task_launcher=lambda task: task(),
    )


def test_watch_uses_fixed_wall_clock_slots_and_starts_afternoon_at_1330() -> None:
    events = []
    coordinator = _coordinator(events)

    for hour, minute in ((9, 30), (10, 0), (10, 30), (11, 0), (11, 30),
                         (13, 0), (13, 30), (14, 0), (14, 30), (15, 0)):
        phase = MarketPhase.LUNCH_BREAK if (hour, minute) in {(11, 30), (13, 0)} else (
            MarketPhase.POSTMARKET if (hour, minute) == (15, 0) else MarketPhase.INTRADAY
        )
        coordinator.phase_provider = lambda _now, value=phase: value
        coordinator.tick(datetime(2026, 8, 14, hour, minute))

    watch_times = [event[1] for event in events if event[0] == "watch"]
    assert watch_times == [
        "09:30", "10:00", "10:30", "11:00", "11:30",
        "13:30", "14:00", "14:30", "15:00",
    ]


def test_restart_at_1007_does_not_shift_next_watch_to_1037() -> None:
    events = []
    coordinator = _coordinator(events)

    coordinator.tick(datetime(2026, 8, 14, 10, 7))
    coordinator.tick(datetime(2026, 8, 14, 10, 30))

    assert [event for event in events if event[0] == "watch"] == [("watch", "10:30")]


def test_same_slot_runs_once_and_1000_order_is_market_screening_watch() -> None:
    events = []
    coordinator = _coordinator(events, state=MarketTrendState.SUSTAINED_UP)

    coordinator.tick(datetime(2026, 8, 14, 10, 0, 1))
    coordinator.tick(datetime(2026, 8, 14, 10, 0, 45))

    assert events == [
        ("market", "10:00"),
        ("screening", "10:00", "sustained_up", None),
        ("watch", "10:00"),
    ]


def test_screening_only_runs_at_configured_1000_and_1400_slots() -> None:
    events = []
    coordinator = _coordinator(events)

    for hour, minute in ((9, 50), (10, 0), (10, 10), (13, 50), (14, 0), (14, 10)):
        coordinator.tick(datetime(2026, 8, 14, hour, minute))

    screening_times = [event[1] for event in events if event[0] == "screening"]
    assert screening_times == ["10:00", "14:00"]


def test_0945_opening_slot_runs_dragon_board_once() -> None:
    events = []
    coordinator = _coordinator(events, state=MarketTrendState.NEUTRAL)

    coordinator.tick(datetime(2026, 8, 14, 9, 45, 1))
    coordinator.tick(datetime(2026, 8, 14, 9, 45, 45))

    assert [event for event in events if event[0] == "screening"] == [
        ("screening", "09:45", "neutral", "dragon_board"),
    ]


def test_non_trading_day_runs_no_intraday_tasks() -> None:
    events = []
    coordinator = _coordinator(events, phase=MarketPhase.NON_TRADING)

    coordinator.tick(datetime(2026, 8, 15, 10, 0))

    assert events == []
