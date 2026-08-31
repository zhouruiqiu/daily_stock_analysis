# -*- coding: utf-8 -*-
from datetime import date

import pandas as pd

from src.services.strategy_outcome_service import StrategyOutcomeService


class _Db:
    def __init__(self):
        self.outcomes = {}
    def list_strategy_evaluation_runs(self, limit=365):
        return [{"run_id":"r1","selection_date":date(2026,8,19),"benchmark_code":"000300"}]
    def list_strategy_evaluation_picks(self, run_id=None, strategy=None):
        return [{"id":1,"run_id":"r1","strategy":"alpha","stock_code":"600036"}]
    def upsert_strategy_evaluation_outcome(self, payload):
        key=(payload["pick_id"],payload["horizon"]); current=self.outcomes.get(key)
        if current and current["status"] != "pending": return current
        self.outcomes[key]=dict(payload); return self.outcomes[key]
    def list_strategy_evaluation_outcomes(self, status=None):
        values=list(self.outcomes.values())
        return [x for x in values if not status or x["status"]==status]


def test_outcome_uses_same_day_open_future_close_and_benchmark():
    days=[date(2026,8,19),date(2026,8,20),date(2026,8,21),date(2026,8,24),date(2026,8,25),date(2026,8,26)]
    def bars(code, start, end, is_index=False):
        base=100 if is_index else 10
        return pd.DataFrame([{"date":d,"open":base,"close":base+i,"low":base-1} for i,d in enumerate(days)])
    db=_Db()
    result=StrategyOutcomeService(db_manager=db,bar_provider=bars,trade_days_provider=lambda start,count:days[:count]).run_due(date(2026,8,26),horizons=[1,3,5])
    assert result["evaluated"]==3
    five=db.outcomes[(1,"5d")]
    assert five["entry_trade_date"]==date(2026,8,19)
    assert five["target_trade_date"]==date(2026,8,26)
    assert five["stock_return_pct"]==50.0
    assert five["benchmark_return_pct"]==5.0
    assert five["excess_return_pct"]==45.0


def test_leaderboard_waits_for_five_complete_days():
    db=_Db()
    db.outcomes[(1,"5d")]={"pick_id":1,"horizon":"5d","status":"evaluated","stock_return_pct":2.0,"excess_return_pct":1.0,"max_adverse_excursion_pct":3.0}
    service=StrategyOutcomeService(db_manager=db,bar_provider=lambda *a,**k:pd.DataFrame(),trade_days_provider=lambda *a:[])
    board=service.get_leaderboard("5d",window=20)
    assert board["items"][0]["rank"] is None
    assert board["items"][0]["sample_status"]=="insufficient"


def test_real_calendar_returns_entry_plus_requested_forward_sessions():
    days = StrategyOutcomeService._trade_days(date(2026, 8, 20), 6)

    assert len(days) == 6
    assert days[0] == date(2026, 8, 20)
    assert days[5] == date(2026, 8, 27)


def test_short_calendar_window_keeps_unavailable_horizon_pending():
    short_days = [
        date(2026, 8, 19), date(2026, 8, 20), date(2026, 8, 21),
        date(2026, 8, 24), date(2026, 8, 25),
    ]
    db = _Db()
    service = StrategyOutcomeService(
        db_manager=db,
        bar_provider=lambda *args, **kwargs: pd.DataFrame(),
        trade_days_provider=lambda start, count: short_days,
    )

    result = service.run_due(date(2026, 8, 27), horizons=[1, 3, 5])

    assert result["pending"] == 3
    assert db.outcomes[(1, "5d")]["status"] == "pending"


def test_leaderboard_surfaces_selected_strategy_while_outcomes_are_pending():
    db = _Db()
    service = StrategyOutcomeService(
        db_manager=db,
        bar_provider=lambda *args, **kwargs: pd.DataFrame(),
        trade_days_provider=lambda *args: [],
    )

    board = service.get_leaderboard("5d", window=20)

    assert board["items"] == [{
        "strategy": "alpha",
        "complete_days": 0,
        "evaluated_count": 0,
        "avg_return_pct": None,
        "avg_excess_return_pct": None,
        "positive_rate_pct": None,
        "beat_benchmark_rate_pct": None,
        "max_adverse_excursion_pct": None,
        "sample_status": "pending",
        "rank": None,
    }]


def test_leaderboard_includes_empty_and_failed_strategies_from_run_summary():
    db = _Db()
    db.list_strategy_evaluation_runs = lambda limit=365: [{
        "run_id": "r1",
        "selection_date": date(2026, 8, 19),
        "benchmark_code": "000300",
        "summary": {
            "strategies": {"alpha": [], "empty_strategy": []},
            "failures": {"broken_strategy": "provider failed"},
        },
    }]
    service = StrategyOutcomeService(
        db_manager=db,
        bar_provider=lambda *args, **kwargs: pd.DataFrame(),
        trade_days_provider=lambda *args: [],
    )

    board = service.get_leaderboard("5d", window=20)

    assert [item["strategy"] for item in board["items"]] == [
        "alpha", "broken_strategy", "empty_strategy",
    ]
    assert all(item["sample_status"] == "pending" for item in board["items"])


def test_intraday_cohort_uses_recorded_selection_price_as_entry():
    days = [date(2026, 8, 19), date(2026, 8, 20)]
    db = _Db()
    db.list_strategy_evaluation_runs = lambda limit=365: [{
        "run_id": "r1",
        "selection_date": days[0],
        "benchmark_code": "000300",
        "summary": {"cohort": "intraday_1000"},
    }]
    db.list_strategy_evaluation_picks = lambda run_id=None, strategy=None: [{
        "id": 1,
        "run_id": "r1",
        "strategy": "capital_heat",
        "stock_code": "600036",
        "auction_price": 12.0,
    }]

    def bars(code, start, end, is_index=False):
        base = 100 if is_index else 10
        return pd.DataFrame([
            {"date": days[0], "open": base, "close": base, "low": base - 1},
            {"date": days[1], "open": base, "close": base + 2, "low": base},
        ])

    service = StrategyOutcomeService(
        db_manager=db,
        bar_provider=bars,
        trade_days_provider=lambda start, count: days,
    )
    service.run_due(days[1], horizons=[1])

    assert db.outcomes[(1, "1d")]["entry_open"] == 12.0
    assert db.outcomes[(1, "1d")]["stock_return_pct"] == 0.0


def test_leaderboard_filters_strategies_by_cohort():
    db = _Db()
    db.list_strategy_evaluation_runs = lambda limit=365: [
        {
            "run_id": "preopen-run",
            "selection_date": date(2026, 8, 19),
            "summary": {"cohort": "preopen_previous_close", "strategies": {"alpha": []}},
        },
        {
            "run_id": "opening-run",
            "selection_date": date(2026, 8, 19),
            "summary": {"cohort": "opening_0945", "strategies": {"dragon_board": []}},
        },
    ]
    db.list_strategy_evaluation_picks = lambda run_id=None, strategy=None: [
        {"id": 1, "run_id": "preopen-run", "strategy": "alpha", "stock_code": "600036"},
        {"id": 2, "run_id": "opening-run", "strategy": "dragon_board", "stock_code": "000001"},
    ]
    service = StrategyOutcomeService(db_manager=db)

    board = service.get_leaderboard("5d", cohort="opening_0945")

    assert board["cohort"] == "opening_0945"
    assert [item["strategy"] for item in board["items"]] == ["dragon_board"]
