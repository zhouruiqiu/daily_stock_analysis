# -*- coding: utf-8 -*-
from datetime import datetime
from types import SimpleNamespace

from src.services.strategy_evaluation_scheduler import StrategyEvaluationScheduler


class _Daily:
    def __init__(self): self.calls=[]
    def run_daily(self, day): self.calls.append(day); return self.run_cohort(day, "preopen_previous_close")
    def run_cohort(self, day, cohort): self.calls.append((day, cohort)); return {"run_id":f"run-{cohort}","selection_date":day,"status":"completed","strategy_count":1,"completed_strategy_count":1,"failed_strategy_count":0,"summary":{"cohort":cohort,"strategies":{"alpha":[{"stock_code":"600036"}]}}}
class _Outcome:
    def __init__(self): self.calls=[]; self.board_calls=[]
    def run_due(self, day, horizons=None): self.calls.append(day); return {"evaluated":3,"pending":2,"unable":0}
    def get_leaderboard(self,*a,**k): self.board_calls.append((a, k)); return {"items":[]}
class _Notifier:
    def __init__(self): self.messages=[]
    def send_with_results(self,content,route_type): self.messages.append((content,route_type)); return SimpleNamespace(success=True)


def test_scheduler_claims_0925_once_and_sends_daily_summary():
    daily=_Daily(); notifier=_Notifier()
    scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=True,strategy_evaluation_weekly_notify=True,strategy_evaluation_horizons=[1,3,5]),daily_service=daily,outcome_service=_Outcome(),notifier=notifier,trading_day_provider=lambda day:True,task_launcher=lambda task: task())
    scheduler.tick(datetime(2026,8,19,9,25,1)); scheduler.tick(datetime(2026,8,19,9,25,40))
    assert daily.calls==[(datetime(2026,8,19).date(), "preopen_previous_close")]
    assert len(notifier.messages)==1
    assert "全策略盘前选股" in notifier.messages[0][0]


def test_scheduler_runs_outcomes_after_close_and_skips_holiday():
    outcome=_Outcome(); scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=False,strategy_evaluation_weekly_notify=False,strategy_evaluation_horizons=[1,3,5]),daily_service=_Daily(),outcome_service=outcome,notifier=_Notifier(),trading_day_provider=lambda day:day.day!=20,task_launcher=lambda task: task())
    scheduler.tick(datetime(2026,8,19,15,10)); scheduler.tick(datetime(2026,8,20,15,10))
    assert outcome.calls==[datetime(2026,8,19).date()]


def test_outcome_failure_is_isolated_from_wall_clock_service():
    class _BrokenOutcome(_Outcome):
        def run_due(self, day, horizons=None):
            raise IndexError("calendar window too short")

    scheduler=StrategyEvaluationScheduler(config_provider=lambda:SimpleNamespace(strategy_evaluation_enabled=True,strategy_evaluation_time="09:25",strategy_evaluation_daily_notify=False,strategy_evaluation_weekly_notify=False,strategy_evaluation_horizons=[1,3,5]),daily_service=_Daily(),outcome_service=_BrokenOutcome(),notifier=_Notifier(),trading_day_provider=lambda day:True,task_launcher=lambda task: task())

    result = scheduler.tick(datetime(2026,8,19,15,10))

    assert result["executed"] == ["outcomes"]
    assert result["errors"] == ["outcomes: calendar window too short"]


def test_scheduler_launches_each_cohort_at_its_market_time_without_blocking_tick():
    daily = _Daily()
    queued = []
    scheduler = StrategyEvaluationScheduler(
        config_provider=lambda: SimpleNamespace(
            strategy_evaluation_enabled=True,
            strategy_evaluation_time="09:25",
            strategy_evaluation_daily_notify=False,
            strategy_evaluation_weekly_notify=False,
            strategy_evaluation_horizons=[1, 3, 5],
        ),
        daily_service=daily,
        outcome_service=_Outcome(),
        notifier=_Notifier(),
        trading_day_provider=lambda day: True,
        task_launcher=queued.append,
    )

    first = scheduler.tick(datetime(2026, 8, 19, 9, 25))
    scheduler.tick(datetime(2026, 8, 19, 9, 45))
    scheduler.tick(datetime(2026, 8, 19, 10, 0))

    assert first["executed"] == ["preopen_previous_close_queued"]
    assert daily.calls == []
    assert len(queued) == 3
    for task in queued:
        task()
    assert daily.calls == [
        (datetime(2026, 8, 19).date(), "preopen_previous_close"),
        (datetime(2026, 8, 19).date(), "opening_0945"),
        (datetime(2026, 8, 19).date(), "intraday_1000"),
    ]


def test_weekly_board_runs_on_last_open_day_before_friday_holiday():
    outcome = _Outcome()
    notifier = _Notifier()
    open_days = {datetime(2026, 10, 8).date()}
    scheduler = StrategyEvaluationScheduler(
        config_provider=lambda: SimpleNamespace(
            strategy_evaluation_enabled=True,
            strategy_evaluation_time="09:25",
            strategy_evaluation_daily_notify=False,
            strategy_evaluation_weekly_notify=True,
            strategy_evaluation_horizons=[1, 3, 5],
        ),
        daily_service=_Daily(),
        outcome_service=outcome,
        notifier=notifier,
        trading_day_provider=lambda day: day in open_days,
        task_launcher=lambda task: task(),
    )

    scheduler.tick(datetime(2026, 10, 8, 15, 10))

    assert any("全策略周榜" in content for content, _ in notifier.messages)
    assert [kwargs["cohort"] for _, kwargs in outcome.board_calls] == [
        "preopen_previous_close", "opening_0945", "intraday_1000",
    ]


def test_daily_notification_exception_is_recorded_as_failed_batch_status():
    class _RecordingDaily(_Daily):
        def __init__(self):
            super().__init__()
            self.notifications = []
        def record_notification_status(self, result, *, success, error=""):
            self.notifications.append((success, error))

    class _BrokenNotifier:
        def send_with_results(self, content, route_type):
            raise RuntimeError("wechat unavailable")

    daily = _RecordingDaily()
    scheduler = StrategyEvaluationScheduler(
        config_provider=lambda: SimpleNamespace(
            strategy_evaluation_enabled=True, strategy_evaluation_time="09:25",
            strategy_evaluation_daily_notify=True, strategy_evaluation_weekly_notify=False,
            strategy_evaluation_horizons=[1, 3, 5],
        ),
        daily_service=daily, outcome_service=_Outcome(), notifier=_BrokenNotifier(),
        trading_day_provider=lambda day: True, task_launcher=lambda task: task(),
    )

    scheduler.tick(datetime(2026, 8, 19, 9, 25))

    assert daily.notifications == [(False, "wechat unavailable")]
