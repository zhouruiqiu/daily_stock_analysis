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
