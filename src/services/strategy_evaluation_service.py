# -*- coding: utf-8 -*-
"""Deterministic daily all-strategy screening experiment."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Optional

from src.storage import DatabaseManager

STRATEGY_EVALUATION_ENGINE_VERSION = "strategy-eval-v1"


class StrategyEvaluationService:
    def __init__(
        self,
        *,
        db_manager: Optional[DatabaseManager] = None,
        strategy_loader: Optional[Callable[[], list[str]]] = None,
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
    def _load_strategies() -> list[str]:
        from src.services.screening.config import Config
        from src.services.screening.strategy import load_all_strategies

        strategies = load_all_strategies(Config.from_env().strategies_dir)
        return [name for name, item in strategies.items() if item.screening.enabled]

    @staticmethod
    def _load_snapshot(_selection_date: date):
        from src.services.screening.config import Config
        from src.services.screening.snapshot import fetch_snapshot_with_fallback

        config = Config.from_env()
        priority = ["tushare", *[item for item in config.snapshot_source_priority if item != "tushare"]]
        frame = fetch_snapshot_with_fallback(
            priority,
            fallback_snapshot_path=config.fallback_snapshot_path,
            fallback_max_age_hours=config.snapshot_fallback_max_age_hours,
            cache_ttl_seconds=0,
            market="cn",
        )
        frame.attrs["snapshot_mode"] = "previous_close"
        return frame

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
        }

    def run_daily(self, selection_date: date) -> dict[str, Any]:
        run_id = f"strategy-eval-{selection_date.isoformat()}-{STRATEGY_EVALUATION_ENGINE_VERSION}"
        strategies = list(self.strategy_loader())
        self.db.upsert_strategy_evaluation_run({
            "run_id": run_id, "market": "cn", "selection_date": selection_date,
            "status": "running", "engine_version": STRATEGY_EVALUATION_ENGINE_VERSION,
            "top_n": self.top_n, "benchmark_code": self.benchmark_code,
            "strategy_count": len(strategies),
        })
        snapshot = self.snapshot_provider(selection_date)
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
            "status": status, "engine_version": STRATEGY_EVALUATION_ENGINE_VERSION,
            "top_n": self.top_n, "benchmark_code": self.benchmark_code,
            "strategy_count": len(strategies), "completed_strategy_count": completed,
            "failed_strategy_count": failed, "completed_at": datetime.utcnow(),
            "summary": {"strategies": results, "failures": failures},
        }
        self.db.upsert_strategy_evaluation_run(payload)
        return payload
