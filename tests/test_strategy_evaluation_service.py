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
    assert {snapshot_id for _, snapshot_id, _ in calls} == {id(snapshot)}
    assert all(call[2]["use_llm"] is False for call in calls)
    assert set(db.picks) == {"alpha", "beta"}
