# -*- coding: utf-8 -*-
from datetime import date
from types import SimpleNamespace

import pandas as pd

from src.services.strategy_evaluation_service import StrategyEvaluationService


class _Db:
    def __init__(self):
        self.run = None
        self.picks = {}

    def upsert_strategy_evaluation_run(self, payload):
        self.run = {"id": 1, **payload}
        return self.run

    def replace_strategy_evaluation_picks(self, run_id, strategy, picks):
        self.picks[strategy] = list(picks)
        return len(picks)


def test_daily_run_shares_snapshot_and_isolates_strategy_failure():
    snapshot = pd.DataFrame([{"code": "600036", "name": "招商银行", "price": 40.0}])
    calls = []

    def runner(strategy, *, snapshot_df, max_output, **kwargs):
        calls.append((strategy, id(snapshot_df), kwargs))
        if strategy == "broken":
            raise RuntimeError("bad strategy")
        return SimpleNamespace(
            picks=[SimpleNamespace(code="600036", name="招商银行", screen_score=80, final_score=81)],
            strategy_version="1.0", snapshot_source="tushare", degradation=[],
        )

    db = _Db()
    service = StrategyEvaluationService(
        db_manager=db,
        strategy_loader=lambda: ["alpha", "broken", "beta"],
        snapshot_provider=lambda selection_date: snapshot,
        screen_runner=runner,
        top_n=5,
    )

    result = service.run_daily(date(2026, 8, 19))

    assert result["status"] == "partial"
    assert result["completed_strategy_count"] == 2
    assert result["failed_strategy_count"] == 1
    assert len(result["engine_version"]) <= 32
    assert {snapshot_id for _, snapshot_id, _ in calls} == {id(snapshot)}
    assert all(call[2]["use_llm"] is False for call in calls)
    assert set(db.picks) == {"alpha", "beta"}


def test_run_cohort_only_evaluates_matching_strategies_and_records_entry_price():
    snapshot = pd.DataFrame([{"code": "600036", "name": "招商银行", "price": 40.0}])

    def runner(strategy, **kwargs):
        return SimpleNamespace(
            picks=[SimpleNamespace(code="600036", name="招商银行", price=40.5, screen_score=80, final_score=81)],
            strategy_version="1.0",
        )

    db = _Db()
    service = StrategyEvaluationService(
        db_manager=db,
        strategy_loader=lambda: {
            "alpha": "preopen_previous_close",
            "dragon": "opening_0945",
        },
        snapshot_provider=lambda selection_date: snapshot,
        screen_runner=runner,
    )

    result = service.run_cohort(date(2026, 8, 19), "opening_0945")

    assert result["cohort"] == "opening_0945"
    assert len(result["engine_version"]) <= 32
    assert result["strategy_count"] == 1
    assert set(db.picks) == {"dragon"}
    assert db.picks["dragon"][0]["auction_price"] == 40.5
    assert db.run["summary"]["cohort"] == "opening_0945"


def test_notification_status_is_persisted_in_run_summary():
    db = _Db()
    service = StrategyEvaluationService(
        db_manager=db,
        strategy_loader=lambda: {"alpha": "preopen_previous_close"},
        snapshot_provider=lambda selection_date: pd.DataFrame(),
        screen_runner=lambda strategy, **kwargs: SimpleNamespace(picks=[], strategy_version="1"),
    )
    result = service.run_daily(date(2026, 8, 19))

    service.record_notification_status(result, success=False, error="all channels failed")

    assert db.run["summary"]["notification"] == {
        "status": "failed",
        "error": "all channels failed",
    }


def test_intraday_cohort_requests_realtime_snapshot_mode():
    requested = []

    def snapshot_provider(selection_date, *, cohort):
        requested.append(cohort)
        frame = pd.DataFrame()
        frame.attrs["snapshot_mode"] = "realtime"
        return frame

    service = StrategyEvaluationService(
        db_manager=_Db(),
        strategy_loader=lambda: {"capital_heat": "intraday_1000"},
        snapshot_provider=snapshot_provider,
        screen_runner=lambda strategy, **kwargs: SimpleNamespace(picks=[], strategy_version="1"),
    )

    service.run_cohort(date(2026, 8, 19), "intraday_1000")

    assert requested == ["intraday_1000"]
