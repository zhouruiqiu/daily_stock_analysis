# -*- coding: utf-8 -*-
"""Forward-return evaluation and leaderboard aggregation for strategy picks."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from src.storage import DatabaseManager

OUTCOME_ENGINE_VERSION = "strategy-outcome-v1"


class StrategyOutcomeService:
    def __init__(self, *, db_manager: Optional[DatabaseManager] = None,
                 bar_provider: Optional[Callable[..., pd.DataFrame]] = None,
                 trade_days_provider: Optional[Callable[[date, int], list[date]]] = None) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.bar_provider = bar_provider or self._tushare_bars
        self.trade_days_provider = trade_days_provider or self._trade_days

    @staticmethod
    def _trade_days(start: date, count: int) -> list[date]:
        try:
            import exchange_calendars as xcals
            cal = xcals.get_calendar("XSHG")
            first = cal.date_to_session(start, direction="next")
            sessions = cal.sessions_window(first, count - 1)
            return [item.date() for item in sessions]
        except Exception:
            result=[]; current=start
            while len(result)<count:
                if current.weekday()<5: result.append(current)
                current += timedelta(days=1)
            return result

    @staticmethod
    def _tushare_bars(code: str, start: date, end: date, *, is_index: bool = False) -> pd.DataFrame:
        from src.config import setup_env
        setup_env()
        import tushare as ts
        token=os.getenv("TUSHARE_TOKEN", "").strip()
        if not token: return pd.DataFrame()
        pro=ts.pro_api(token)
        if is_index:
            ts_code = "000300.SH" if code == "000300" else code
            frame=pro.index_daily(ts_code=ts_code,start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"))
        else:
            suffix="SH" if str(code).startswith(("5","6","9")) else "SZ"
            frame=pro.daily(ts_code=f"{code}.{suffix}",start_date=start.strftime("%Y%m%d"),end_date=end.strftime("%Y%m%d"))
        if frame is None or frame.empty: return pd.DataFrame()
        frame=frame.rename(columns={"trade_date":"date"}).copy()
        frame["date"]=pd.to_datetime(frame["date"]).dt.date
        return frame.sort_values("date")

    def run_due(self, as_of_date: date, *, horizons: list[int] | None = None) -> dict[str, Any]:
        requested=sorted(set(horizons or [1,3,5]))
        evaluated=pending=unable=0
        for run in self.db.list_strategy_evaluation_runs(limit=365):
            selection=run["selection_date"]
            days=self.trade_days_provider(selection, max(requested)+1)
            if not days or days[0] != selection: continue
            benchmark=self.bar_provider(run.get("benchmark_code") or "000300",days[0],days[-1],is_index=True)
            for pick in self.db.list_strategy_evaluation_picks(run_id=run["run_id"]):
                stock=self.bar_provider(pick["stock_code"],days[0],days[-1],is_index=False)
                for horizon in requested:
                    payload={"pick_id":pick["id"],"horizon":f"{horizon}d","engine_version":OUTCOME_ENGINE_VERSION,"status":"pending"}
                    target=days[horizon]
                    if target > as_of_date or stock.empty or benchmark.empty:
                        self.db.upsert_strategy_evaluation_outcome(payload); pending+=1; continue
                    try:
                        entry=stock.loc[stock["date"]==days[0]].iloc[0]
                        target_row=stock.loc[stock["date"]==target].iloc[0]
                        b_entry=benchmark.loc[benchmark["date"]==days[0]].iloc[0]
                        b_target=benchmark.loc[benchmark["date"]==target].iloc[0]
                        window=stock[(stock["date"]>=days[0]) & (stock["date"]<=target)]
                        entry_open=float(entry["open"]); target_close=float(target_row["close"])
                        b_open=float(b_entry["open"]); b_close=float(b_target["close"])
                        stock_return=round((target_close/entry_open-1)*100,4)
                        benchmark_return=round((b_close/b_open-1)*100,4)
                        payload.update({"status":"evaluated","entry_trade_date":days[0],"target_trade_date":target,
                            "entry_open":entry_open,"target_close":target_close,"min_low":float(window["low"].min()),
                            "benchmark_entry_open":b_open,"benchmark_target_close":b_close,
                            "stock_return_pct":stock_return,"benchmark_return_pct":benchmark_return,
                            "excess_return_pct":round(stock_return-benchmark_return,4),
                            "max_adverse_excursion_pct":round(max(0,(entry_open-float(window["low"].min()))/entry_open*100),4),
                            "evaluated_at":datetime.utcnow()})
                        self.db.upsert_strategy_evaluation_outcome(payload); evaluated+=1
                    except (IndexError,KeyError,TypeError,ValueError,ZeroDivisionError):
                        self.db.upsert_strategy_evaluation_outcome(payload); pending+=1
        return {"evaluated":evaluated,"pending":pending,"unable":unable}

    def get_leaderboard(self, horizon: str = "5d", *, window: int = 20) -> dict[str, Any]:
        picks=self.db.list_strategy_evaluation_picks()
        by_id={item["id"]:item for item in picks}
        groups: dict[str,list[dict[str,Any]]]={}
        for row in self.db.list_strategy_evaluation_outcomes():
            pick=by_id.get(row["pick_id"])
            if pick and row["horizon"]==horizon and row["status"]=="evaluated":
                groups.setdefault(pick["strategy"],[]).append(row)
        items=[]
        for strategy,rows in groups.items():
            run_days=len({by_id[row["pick_id"]]["run_id"] for row in rows})
            count=len(rows)
            avg=lambda key: round(sum(float(row[key]) for row in rows if row.get(key) is not None)/count,4)
            sufficient=run_days>=5
            items.append({"strategy":strategy,"complete_days":run_days,"evaluated_count":count,
                "avg_return_pct":avg("stock_return_pct"),"avg_excess_return_pct":avg("excess_return_pct"),
                "positive_rate_pct":round(sum(float(r["stock_return_pct"])>0 for r in rows)/count*100,2),
                "beat_benchmark_rate_pct":round(sum(float(r["excess_return_pct"])>0 for r in rows)/count*100,2),
                "max_adverse_excursion_pct":max(float(r.get("max_adverse_excursion_pct") or 0) for r in rows),
                "sample_status":"ranked" if sufficient else "insufficient","rank":None})
        ranked=sorted([x for x in items if x["sample_status"]=="ranked"],key=lambda x:x["avg_excess_return_pct"],reverse=True)
        for index,item in enumerate(ranked,start=1): item["rank"]=index
        remainder=sorted([x for x in items if x["sample_status"]!="ranked"],key=lambda x:x["strategy"])
        return {"horizon":horizon,"window":window,"items":[*ranked,*remainder]}
