# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace

from src.services.strategy_evaluation_scheduler import StrategyEvaluationScheduler


class _Daily:
    def __init__(self): self.calls=[]
    def run_daily(self, day): self.calls.append(day); return {"status":"completed","strategy_count":11,"completed_strategy_count":11,"failed_strategy_count":0,"summary":{"strategies":{"alpha":[{"stock_code":"600036"}]}}}
class _Outcome:
    def __init__(self): self.calls=[]
    def run_due(self, day, horizons=None): self.calls.append(day); return {"evaluated":3,"pending":2,"unable":0}
    def get_leaderboard(self,*a,**k): return {"items":[]}
class _Notifier:
    def __init__(self): self.messages=[]
    def send_with_results(self,content,route_type): self.messages.append((content,route_type)); return SimpleNamespace(success=True)


def test_scheduler_claims_0925_once_and_sends_daily_summary():
    daily=_Daily(); notifier=_Notifier()
    scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=True,strategy_evaluation_weekly_notify=True,strategy_evaluation_horizons=[1,3,5]),daily_service=daily,outcome_service=_Outcome(),notifier=notifier,trading_day_provider=lambda day:True)
    scheduler.tick(datetime(2026,8,19,9,25,1)); scheduler.tick(datetime(2026,8,19,9,25,40))
    assert len(daily.calls)==1
    assert len(notifier.messages)==1
    assert "全策略盘前选股" in notifier.messages[0][0]


def test_scheduler_runs_outcomes_after_close_and_skips_holiday():
    outcome=_Outcome(); scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=False,strategy_evaluation_weekly_notify=False,strategy_evaluation_horizons=[1,3,5]),daily_service=_Daily(),outcome_service=outcome,notifier=_Notifier(),trading_day_provider=lambda day:day.day!=20)
    scheduler.tick(datetime(2026,8,19,15,10)); scheduler.tick(datetime(2026,8,20,15,10))
    assert outcome.calls==[datetime(2026,8,19).date()]


def test_outcome_failure_is_isolated_from_wall_clock_service():
    class _BrokenOutcome(_Outcome):
        def run_due(self, day, horizons=None):
            raise IndexError("calendar window too short")

    scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=False,strategy_evaluation_weekly_notify=False,strategy_evaluation_horizons=[1,3,5]),daily_service=_Daily(),outcome_service=_BrokenOutcome(),notifier=_Notifier(),trading_day_provider=lambda day:True)

    result = scheduler.tick(datetime(2026,8,19,15,10))

    assert result["executed"] == []
    assert result["errors"] == ["outcomes: calendar window too short"]
