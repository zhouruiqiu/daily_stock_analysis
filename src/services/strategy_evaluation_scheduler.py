# -*- coding: utf-8 -*-
"""Wall-clock scheduler for daily strategy evaluation and outcomes."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from src.core.trading_calendar import is_market_open
from src.services.strategy_evaluation_service import StrategyEvaluationService
from src.services.strategy_outcome_service import StrategyOutcomeService

logger = logging.getLogger(__name__)


class StrategyEvaluationScheduler:
    def __init__(self, *, config_provider: Callable[[], Any],
                 daily_service: Optional[Any] = None, outcome_service: Optional[Any] = None,
                 notifier: Optional[Any] = None,
                 trading_day_provider: Callable[[Any], bool] = lambda day: is_market_open("cn", day)) -> None:
        self.config_provider=config_provider
        self.daily_service=daily_service or StrategyEvaluationService()
        self.outcome_service=outcome_service or StrategyOutcomeService()
        self.notifier=notifier
        self.trading_day_provider=trading_day_provider
        self._claims:set[str]=set()

    def _notify(self, content: str) -> None:
        notifier=self.notifier
        if notifier is None:
            from src.notification import NotificationService
            notifier=NotificationService()
        notifier.send_with_results(content,route_type="alert")

    @staticmethod
    def _daily_message(result: dict[str,Any]) -> str:
        lines=["🧪 全策略盘前选股",f"状态：{result.get('status')}",f"完成：{result.get('completed_strategy_count',0)}/{result.get('strategy_count',0)}"]
        for strategy,picks in (result.get("summary",{}).get("strategies",{}) or {}).items():
            codes="、".join(item.get("stock_code","") for item in picks)
            lines.append(f"{strategy}：{codes or '-'}")
        return "\n".join(lines)

    def tick(self, now: Optional[datetime]=None) -> dict[str,Any]:
        current=now or datetime.now(ZoneInfo("Asia/Shanghai"))
        config=self.config_provider(); executed=[]; errors=[]
        if not getattr(config,"strategy_evaluation_enabled",False) or not self.trading_day_provider(current.date()):
            return {"executed":executed,"errors":errors}
        time_text=current.strftime("%H:%M")
        key=f"{current.date()}:{time_text}"
        if time_text==getattr(config,"strategy_evaluation_time","09:25") and key not in self._claims:
            self._claims.add(key); result=self.daily_service.run_daily(current.date()); executed.append("daily")
            if getattr(config,"strategy_evaluation_daily_notify",True): self._notify(self._daily_message(result))
        if time_text=="15:10" and key not in self._claims:
            self._claims.add(key)
            try:
                result=self.outcome_service.run_due(current.date(),horizons=list(getattr(config,"strategy_evaluation_horizons",[1,3,5]))); executed.append("outcomes")
                if getattr(config,"strategy_evaluation_weekly_notify",True) and current.weekday()==4:
                    board=self.outcome_service.get_leaderboard("5d",window=20)
                    ranked=[item for item in board.get("items",[]) if item.get("rank")]
                    lines=["🏆 全策略周榜",*[f"{item['rank']}. {item['strategy']} 超额{item['avg_excess_return_pct']:+.2f}%" for item in ranked[:5]]]
                    self._notify("\n".join(lines))
            except Exception as exc:  # noqa: BLE001 - outcome failures must not stop intraday monitoring.
                logger.exception("[StrategyEvaluation] outcome run failed: %s", exc)
                errors.append(f"outcomes: {exc}")
        return {"executed":executed,"errors":errors}
