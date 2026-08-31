# -*- coding: utf-8 -*-
"""Deterministic daily all-strategy screening experiment."""
from __future__ import annotations

from datetime import date, datetime
import inspect
from typing import Any, Callable, Optional

from src.storage import DatabaseManager

STRATEGY_EVALUATION_ENGINE_VERSION = "strategy-eval-v1"
_COHORT_ENGINE_SUFFIX = {
    "preopen_previous_close": "pc",
    "opening_0945": "o945",
    "intraday_1000": "i1000",
}


class StrategyEvaluationService:
    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        strategy_loader: Optional[Callable[[], Any]] = None,
        snapshot_provider: Optional[Callable[[date], Any]] = None,
        screen_runner: Optional[Callable[..., Any]] = None,
        top_n: int = 5,
        benchmark_code: str = "000300",
    ) -> None:
        self.db = db_manager or DatabaseManager.get_instance()
        self.strategy_loader = strategy_loader or self._load_strategies
        self.snapshot_provider = snapshot_provider or self._load_snapshot
        self.screen_runner = screen_runner or self._screen
        self.top_n = max(1, min(int(top_n), 20))
        self.benchmark_code = benchmark_code

    @staticmethod
    def _load_strategies() -> dict[str, str]:
        from src.services.screening.config import Config
        from src.services.screening.strategy import load_all_strategies

        strategies = load_all_strategies(Config.from_env().strategies_dir)
        return {
            name: item.evaluation_profile.cohort
            for name, item in strategies.items()
            if item.screening.enabled
        }

    @staticmethod
    def _load_snapshot(_selection_date: date, *, cohort: str = "preopen_previous_close"):
        from src.services.screening.config import Config
        from src.services.screening.snapshot import fetch_snapshot_with_fallback

        config = Config.from_env()
        if cohort == "preopen_previous_close":
            priority = ["tushare", *[item for item in config.snapshot_source_priority if item != "tushare"]]
            required_mode = "eod"
        else:
            priority = [item for item in config.snapshot_source_priority if item != "tushare"]
            required_mode = "realtime"
        frame = fetch_snapshot_with_fallback(
            priority,
            fallback_snapshot_path=config.fallback_snapshot_path,
            fallback_max_age_hours=config.snapshot_fallback_max_age_hours,
            cache_ttl_seconds=0,
            market="cn",
            required_snapshot_mode=required_mode,
        )
        if cohort == "preopen_previous_close":
            frame.attrs["snapshot_mode"] = "previous_close"
        return frame

    def _snapshot_for(self, selection_date: date, cohort: str) -> Any:
        parameters = inspect.signature(self.snapshot_provider).parameters.values()
        accepts_cohort = any(
            item.name == "cohort" or item.kind == inspect.Parameter.VAR_KEYWORD
            for item in parameters
        )
        if accepts_cohort:
            return self.snapshot_provider(selection_date, cohort=cohort)
        return self.snapshot_provider(selection_date)

    @staticmethod
    def _screen(strategy: str, **kwargs):
        from src.services.screening.config import Config
        from src.services.screening.pipeline import screen

        return screen(strategy, config=Config.from_env(), **kwargs)

    @staticmethod
    def _pick_payload(item: Any, rank: int, strategy_version: str) -> dict[str, Any]:
        return {
            "stock_code": str(getattr(item, "code", "")),
            "stock_name": str(getattr(item, "name", "") or ""),
            "rank": rank,
            "screen_score": getattr(item, "screen_score", None),
            "final_score": getattr(item, "final_score", None),
            "strategy_version": strategy_version,
            "auction_price": getattr(item, "price", None),
        }

    def run_daily(self, selection_date: date) -> dict[str, Any]:
        return self.run_cohort(selection_date, "preopen_previous_close")

    def record_notification_status(
        self,
        result: dict[str, Any],
        *,
        success: bool,
        error: str = "",
    ) -> dict[str, Any]:
        updated = dict(result)
        summary = dict(updated.get("summary") or {})
        notification = {"status": "sent" if success else "failed"}
        if error:
            notification["error"] = str(error)
        summary["notification"] = notification
        updated["summary"] = summary
        self.db.upsert_strategy_evaluation_run(updated)
        return updated

    def run_cohort(self, selection_date: date, cohort: str) -> dict[str, Any]:
        loaded = self.strategy_loader()
        if isinstance(loaded, dict):
            strategies = [name for name, item_cohort in loaded.items() if item_cohort == cohort]
        else:
            strategies = list(loaded) if cohort == "preopen_previous_close" else []
        engine_version = (
            f"{STRATEGY_EVALUATION_ENGINE_VERSION}-"
            f"{_COHORT_ENGINE_SUFFIX.get(cohort, 'custom')}"
        )
        run_id = f"strategy-eval-{selection_date.isoformat()}-{engine_version}"
        self.db.upsert_strategy_evaluation_run({
            "run_id": run_id, "market": "cn", "selection_date": selection_date,
            "status": "running", "engine_version": engine_version,
            "top_n": self.top_n, "benchmark_code": self.benchmark_code,
            "strategy_count": len(strategies),
            "snapshot_mode": "previous_close" if cohort == "preopen_previous_close" else "realtime",
            "summary": {"cohort": cohort},
        })
        snapshot = self._snapshot_for(selection_date, cohort)
        completed = 0
        failures: dict[str, str] = {}
        results: dict[str, list[dict[str, Any]]] = {}
        for strategy in strategies:
            try:
                raw = self.screen_runner(
                    strategy,
                    market="cn",
                    max_output=self.top_n,
                    use_llm=False,
                    post_analyzers=["scorecard"],
                    selection_seed="",
                    snapshot_df=snapshot,
                    allow_snapshot_mode_override=True,
                )
                version = str(getattr(raw, "strategy_version", "") or "")
                picks = [
                    self._pick_payload(item, rank, version)
                    for rank, item in enumerate(list(getattr(raw, "picks", []))[:self.top_n], start=1)
                ]
                self.db.replace_strategy_evaluation_picks(run_id, strategy, picks)
                results[strategy] = picks
                completed += 1
            except Exception as exc:  # noqa: BLE001 - one strategy must not stop the batch.
                failures[strategy] = f"{exc.__class__.__name__}: {exc}"
        failed = len(failures)
        status = "completed" if failed == 0 else ("partial" if completed else "failed")
        payload = {
            "run_id": run_id, "market": "cn", "selection_date": selection_date,
            "status": status, "engine_version": engine_version,
            "top_n": self.top_n, "benchmark_code": self.benchmark_code,
            "strategy_count": len(strategies), "completed_strategy_count": completed,
            "failed_strategy_count": failed, "completed_at": datetime.utcnow(),
            "cohort": cohort,
            "snapshot_mode": "previous_close" if cohort == "preopen_previous_close" else "realtime",
            "summary": {"cohort": cohort, "strategies": results, "failures": failures},
        }
        self.db.upsert_strategy_evaluation_run(payload)
        return payload
