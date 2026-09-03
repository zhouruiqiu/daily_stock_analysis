# -*- coding: utf-8 -*-
"""Behavior tests for the lightweight intraday market monitor."""

from datetime import datetime

from src.services.intraday_market_monitor import (
    IntradayMarketMonitor,
    MarketTrendState,
)


def _rows(sh: float, sz: float, cyb: float, *, change_pct: float = 0.0):
    return [
        {"code": "sh000001", "name": "上证指数", "current": sh, "change_pct": change_pct},
        {"code": "sz399001", "name": "深证成指", "current": sz, "change_pct": change_pct},
        {"code": "sz399006", "name": "创业板指", "current": cyb, "change_pct": change_pct},
    ]


class _SequenceProvider:
    def __init__(self, values):
        self.values = list(values)

    def __call__(self):
        return self.values.pop(0)


class _Notifier:
    def __init__(self):
        self.messages = []

    def send_with_results(self, content, *, route_type, bypass_digest):
        assert bypass_digest is True
        self.messages.append((content, route_type))
        return type("Dispatch", (), {"success": True})()


def test_can_sample_for_screening_without_sending_market_alerts() -> None:
    notifier = _Notifier()
    monitor = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(3006, 10030, 2008),
            _rows(3015, 10060, 2016),
        ]),
        notifier=notifier,
        threshold_pct=0.35,
        notifications_enabled=False,
    )

    monitor.run_once(datetime(2026, 8, 14, 9, 30))
    monitor.run_once(datetime(2026, 8, 14, 9, 40))
    result = monitor.run_once(datetime(2026, 8, 14, 9, 50))

    assert result.state is MarketTrendState.SUSTAINED_UP
    assert notifier.messages == []


def test_three_rising_samples_enter_sustained_up_once() -> None:
    notifier = _Notifier()
    monitor = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(3006, 10030, 2008),
            _rows(3015, 10060, 2016),
        ]),
        notifier=notifier,
        threshold_pct=0.35,
    )

    monitor.run_once(datetime(2026, 8, 14, 9, 30))
    monitor.run_once(datetime(2026, 8, 14, 9, 40))
    result = monitor.run_once(datetime(2026, 8, 14, 9, 50))

    assert result.state is MarketTrendState.SUSTAINED_UP
    assert len(notifier.messages) == 1
    assert "持续上涨" in notifier.messages[0][0]
    assert notifier.messages[0][1] == "alert"


def test_three_falling_samples_enter_sustained_down() -> None:
    monitor = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(2992, 9965, 1990),
            _rows(2984, 9930, 1980),
        ]),
        notifier=_Notifier(),
        threshold_pct=0.35,
    )

    monitor.run_once(datetime(2026, 8, 14, 10, 0))
    monitor.run_once(datetime(2026, 8, 14, 10, 10))
    result = monitor.run_once(datetime(2026, 8, 14, 10, 20))

    assert result.state is MarketTrendState.SUSTAINED_DOWN


def test_large_daily_drop_alerts_on_first_sample_without_monotonic_sequence() -> None:
    notifier = _Notifier()
    monitor = IntradayMarketMonitor(
        index_provider=lambda: _rows(2950, 9700, 1900, change_pct=-2.0),
        notifier=notifier,
        threshold_pct=0.35,
        drop_alert_pct=1.5,
    )

    result = monitor.run_once(datetime(2026, 8, 14, 9, 30))

    assert result.state is MarketTrendState.SUSTAINED_DOWN
    assert result.daily_changes == {
        "上证指数": -2.0,
        "深证成指": -2.0,
        "创业板指": -2.0,
    }
    assert len(notifier.messages) == 1
    assert "相对昨收大幅下跌" in notifier.messages[0][0]


def test_below_threshold_and_insufficient_indices_stay_neutral() -> None:
    small_move = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(3001, 10002, 2001),
            _rows(3002, 10004, 2002),
        ]),
        notifier=_Notifier(),
        threshold_pct=0.35,
    )
    for minute in (30, 40, 50):
        result = small_move.run_once(datetime(2026, 8, 14, 9, minute))
    assert result.state is MarketTrendState.NEUTRAL

    one_index = IntradayMarketMonitor(
        index_provider=lambda: [
            {"code": "sh000001", "name": "上证指数", "current": 3015, "change_pct": 0.5}
        ],
        notifier=_Notifier(),
        threshold_pct=0.35,
    )
    result = one_index.run_once(datetime(2026, 8, 14, 10, 0))
    assert result.state is MarketTrendState.NEUTRAL
    assert result.valid_index_count == 1


def test_afternoon_session_discards_morning_samples() -> None:
    monitor = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(3010, 10040, 2010),
            _rows(3020, 10080, 2020),
            _rows(3040, 10150, 2040),
        ]),
        notifier=_Notifier(),
        threshold_pct=0.35,
    )
    monitor.run_once(datetime(2026, 8, 14, 11, 10))
    monitor.run_once(datetime(2026, 8, 14, 11, 20))
    morning = monitor.run_once(datetime(2026, 8, 14, 11, 30))
    afternoon = monitor.run_once(datetime(2026, 8, 14, 13, 30))

    assert morning.state is MarketTrendState.SUSTAINED_UP
    assert afternoon.state is MarketTrendState.NEUTRAL
    assert afternoon.sample_count == 1


def test_state_transition_notifies_once_then_notifies_recovery() -> None:
    notifier = _Notifier()
    monitor = IntradayMarketMonitor(
        index_provider=_SequenceProvider([
            _rows(3000, 10000, 2000),
            _rows(3010, 10040, 2010),
            _rows(3020, 10080, 2020),
            _rows(3020, 10080, 2020),
        ]),
        notifier=notifier,
        threshold_pct=0.35,
    )
    for minute in (0, 10, 20, 30):
        monitor.run_once(datetime(2026, 8, 14, 10, minute))

    assert len(notifier.messages) == 2
    assert "持续上涨" in notifier.messages[0][0]
    assert "趋势解除" in notifier.messages[1][0]
    assert monitor.current_state is MarketTrendState.NEUTRAL
