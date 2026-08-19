# -*- coding: utf-8 -*-
from datetime import date
import os

import pytest

from src.config import Config
from src.storage import DatabaseManager


@pytest.fixture()
def isolated_db(tmp_path):
    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(tmp_path / "strategy-evaluation.db")
    Config.reset_instance()
    DatabaseManager.reset_instance()
    db = DatabaseManager.get_instance()
    try:
        yield db
    finally:
        DatabaseManager.reset_instance()
        Config.reset_instance()
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous


def test_run_and_picks_are_idempotent(isolated_db):
    run = isolated_db.upsert_strategy_evaluation_run({
        "run_id": "eval-20260819",
        "market": "cn",
        "selection_date": date(2026, 8, 19),
        "status": "running",
        "engine_version": "v1",
        "top_n": 5,
        "benchmark_code": "000300",
    })
    repeated = isolated_db.upsert_strategy_evaluation_run({
        **run,
        "status": "completed",
        "strategy_count": 11,
        "completed_strategy_count": 11,
    })
    assert repeated["id"] == run["id"]
    assert repeated["status"] == "completed"

    picks = [{"stock_code": "600036", "stock_name": "招商银行", "rank": 1, "final_score": 80.0}]
    assert isolated_db.replace_strategy_evaluation_picks("eval-20260819", "balanced_alpha", picks) == 1
    assert isolated_db.replace_strategy_evaluation_picks("eval-20260819", "balanced_alpha", picks) == 1
    stored = isolated_db.list_strategy_evaluation_picks(run_id="eval-20260819")
    assert [(row["strategy"], row["stock_code"], row["rank"]) for row in stored] == [
        ("balanced_alpha", "600036", 1),
    ]


def test_terminal_outcome_is_not_overwritten(isolated_db):
    isolated_db.upsert_strategy_evaluation_run({
        "run_id": "eval-20260819", "market": "cn", "selection_date": date(2026, 8, 19),
        "status": "completed", "engine_version": "v1", "top_n": 5, "benchmark_code": "000300",
    })
    isolated_db.replace_strategy_evaluation_picks(
        "eval-20260819", "balanced_alpha", [{"stock_code": "600036", "rank": 1}]
    )
    pick_id = isolated_db.list_strategy_evaluation_picks(run_id="eval-20260819")[0]["id"]
    pending = isolated_db.upsert_strategy_evaluation_outcome({
        "pick_id": pick_id, "horizon": "1d", "engine_version": "v1", "status": "pending",
    })
    evaluated = isolated_db.upsert_strategy_evaluation_outcome({
        **pending, "status": "evaluated", "stock_return_pct": 2.0, "excess_return_pct": 1.2,
    })
    repeated = isolated_db.upsert_strategy_evaluation_outcome({
        **evaluated, "status": "evaluated", "stock_return_pct": 99.0,
    })
    assert repeated["stock_return_pct"] == 2.0
