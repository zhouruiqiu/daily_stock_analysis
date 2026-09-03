# -*- coding: utf-8 -*-
"""Behavior tests for market-regime-driven intraday screening."""

from datetime import datetime
from types import SimpleNamespace

from src.services.intraday_market_monitor import MarketTrendState
from src.services.intraday_screening_worker import (
    IntradayScreeningWorker,
    select_strategy,
)


class _Notifier:
    def __init__(self):
        self.messages = []

    def send_with_results(self, content, *, route_type, bypass_digest):
        assert bypass_digest is True
        self.messages.append((content, route_type))
        return type("Dispatch", (), {"success": True})()


class _ScreeningService:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def screen(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _config(max_results=5):
    return SimpleNamespace(
        intraday_screening_enabled=True,
        intraday_screening_max_results=max_results,
    )


def test_strategy_mapping_matches_market_regime() -> None:
    assert select_strategy(MarketTrendState.SUSTAINED_UP) == "dragon_board"
    assert select_strategy(MarketTrendState.SUSTAINED_DOWN) == "low_volatility_quality"
    assert select_strategy(MarketTrendState.NEUTRAL) == "shrink_pullback"


def test_screening_formats_at_most_five_candidates_and_degradation() -> None:
    candidates = [
        {
            "code": f"60000{i}",
            "name": f"候选{i}",
            "screen_score": 80 - i,
            "risk_level": "low",
            "risk_flags": [],
        }
        for i in range(6)
    ]
    service = _ScreeningService({
        "candidates": candidates,
        "candidate_count": 6,
        "snapshot_source": "tushare",
        "llm_ranked": False,
        "degradation": ["LLM ranking failed: fell back to screen_score"],
    })
    notifier = _Notifier()
    worker = IntradayScreeningWorker(
        config_provider=lambda: _config(),
        screening_service=service,
        notifier=notifier,
    )

    stats = worker.run_once(
        MarketTrendState.NEUTRAL,
        datetime(2026, 8, 14, 10, 0),
    )

    assert stats == {"strategy": "shrink_pullback", "selected": 5, "notified": 1, "failed": 0}
    assert service.calls == [{"strategy": "shrink_pullback", "market": "cn", "max_results": 5}]
    content, route = notifier.messages[0]
    assert route == "alert"
    assert "缩量回踩" in content
    assert "600004" in content
    assert "600005" not in content
    assert "量化分回退" in content


def test_zero_candidates_still_notifies_completed_run() -> None:
    notifier = _Notifier()
    worker = IntradayScreeningWorker(
        config_provider=lambda: _config(),
        screening_service=_ScreeningService({
            "candidates": [],
            "candidate_count": 0,
            "snapshot_source": "sina",
            "llm_ranked": True,
            "degradation": [],
        }),
        notifier=notifier,
    )

    stats = worker.run_once(
        MarketTrendState.SUSTAINED_DOWN,
        datetime(2026, 8, 14, 14, 0),
    )

    assert stats["selected"] == 0
    assert stats["notified"] == 1
    assert "本次没有符合条件的股票" in notifier.messages[0][0]


def test_explicit_opening_strategy_uses_dragon_board_reason() -> None:
    notifier = _Notifier()
    service = _ScreeningService({
        "candidates": [],
        "candidate_count": 0,
        "snapshot_source": "sina",
        "llm_ranked": False,
        "degradation": [],
    })
    worker = IntradayScreeningWorker(
        config_provider=lambda: _config(),
        screening_service=service,
        notifier=notifier,
    )

    worker.run_once(
        MarketTrendState.NEUTRAL,
        datetime(2026, 8, 14, 9, 45),
        strategy="dragon_board",
    )

    assert service.calls == [
        {"strategy": "dragon_board", "market": "cn", "max_results": 5},
    ]
    assert "本次策略：强势题材" in notifier.messages[0][0]
    assert "开盘后观察强势题材" in notifier.messages[0][0]


def test_screening_exception_is_isolated_and_reported() -> None:
    notifier = _Notifier()
    worker = IntradayScreeningWorker(
        config_provider=lambda: _config(),
        screening_service=_ScreeningService(error=RuntimeError("provider timeout")),
        notifier=notifier,
    )

    stats = worker.run_once(
        MarketTrendState.SUSTAINED_UP,
        datetime(2026, 8, 14, 10, 0),
    )

    assert stats["failed"] == 1
    assert stats["notified"] == 1
    assert "选股执行失败" in notifier.messages[0][0]
