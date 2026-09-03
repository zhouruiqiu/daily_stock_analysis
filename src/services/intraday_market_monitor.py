# -*- coding: utf-8 -*-
"""Lightweight A-share index trend monitor for intraday alerts."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from statistics import median
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

_INDEX_NAMES = {"上证指数", "深证成指", "创业板指"}


class MarketTrendState(str, Enum):
    NEUTRAL = "neutral"
    SUSTAINED_UP = "sustained_up"
    SUSTAINED_DOWN = "sustained_down"


@dataclass(frozen=True)
class MarketTrendSnapshot:
    state: MarketTrendState
    sample_count: int
    valid_index_count: int
    cumulative_changes: Dict[str, float]
    daily_changes: Dict[str, float]
    sampled_at: datetime


@dataclass(frozen=True)
class _MarketSample:
    sampled_at: datetime
    values: Dict[str, float]


class IntradayMarketMonitor:
    """Keep three index samples and notify only when the trend state changes."""

    def __init__(
        self,
        *,
        index_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
        notifier: Optional[Any] = None,
        threshold_pct: float = 0.35,
        drop_alert_pct: float = 1.5,
        notifications_enabled: bool = True,
    ) -> None:
        self._index_provider = index_provider
        self._notifier = notifier
        self.threshold_pct = max(float(threshold_pct), 0.0)
        self.drop_alert_pct = max(float(drop_alert_pct), 0.0)
        self.notifications_enabled = bool(notifications_enabled)
        self._samples: List[_MarketSample] = []
        self._session_key: Optional[str] = None
        self.current_state = MarketTrendState.NEUTRAL

    def _fetch_indices(self) -> List[Dict[str, Any]]:
        if self._index_provider is not None:
            return list(self._index_provider() or [])
        from data_provider.base import DataFetcherManager

        return list(
            DataFetcherManager().get_main_indices(
                region="cn",
                require_realtime=True,
            )
            or []
        )

    @staticmethod
    def _session_for(now: datetime) -> str:
        period = "am" if now.hour < 12 else "pm"
        return f"{now.date().isoformat()}:{period}"

    @staticmethod
    def _normalize_indices(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        values: Dict[str, float] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name not in _INDEX_NAMES:
                continue
            try:
                current = float(row.get("current"))
            except (TypeError, ValueError):
                continue
            if current > 0:
                values[name] = current
        return values

    @staticmethod
    def _normalize_daily_changes(rows: List[Dict[str, Any]]) -> Dict[str, float]:
        changes: Dict[str, float] = {}
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name not in _INDEX_NAMES:
                continue
            try:
                change_pct = float(row.get("change_pct"))
            except (TypeError, ValueError):
                continue
            changes[name] = change_pct
        return changes

    def run_once(self, now: Optional[datetime] = None) -> MarketTrendSnapshot:
        sampled_at = now or datetime.now()
        session_key = self._session_for(sampled_at)
        if session_key != self._session_key:
            self._samples = []
            self.current_state = MarketTrendState.NEUTRAL
            self._session_key = session_key

        rows = self._fetch_indices()
        values = self._normalize_indices(rows)
        daily_changes = self._normalize_daily_changes(rows)
        self._samples.append(_MarketSample(sampled_at=sampled_at, values=values))
        self._samples = self._samples[-3:]

        previous = self.current_state
        state, changes = self._classify(daily_changes)
        self.current_state = state
        snapshot = MarketTrendSnapshot(
            state=state,
            sample_count=len(self._samples),
            valid_index_count=len(values),
            cumulative_changes=changes,
            daily_changes=daily_changes,
            sampled_at=sampled_at,
        )
        if self.notifications_enabled and state is not previous:
            self._notify_transition(previous, snapshot)
        return snapshot

    def _classify(
        self,
        daily_changes: Dict[str, float],
    ) -> tuple[MarketTrendState, Dict[str, float]]:
        daily_falling = sum(
            value <= -self.drop_alert_pct
            for value in daily_changes.values()
        )
        if daily_falling >= 2:
            return MarketTrendState.SUSTAINED_DOWN, daily_changes
        if len(self._samples) < 3:
            return MarketTrendState.NEUTRAL, {}
        first, middle, last = self._samples
        common = set(first.values) & set(middle.values) & set(last.values)
        if len(common) < 2:
            return MarketTrendState.NEUTRAL, {}

        changes = {
            name: (last.values[name] / first.values[name] - 1.0) * 100.0
            for name in common
            if first.values[name] > 0
        }
        if len(changes) < 2:
            return MarketTrendState.NEUTRAL, changes

        rising = sum(
            first.values[name] < middle.values[name] < last.values[name]
            for name in changes
        )
        falling = sum(
            first.values[name] > middle.values[name] > last.values[name]
            for name in changes
        )
        middle_change = median(changes.values())
        if rising >= 2 and middle_change >= self.threshold_pct:
            return MarketTrendState.SUSTAINED_UP, changes
        if falling >= 2 and middle_change <= -self.threshold_pct:
            return MarketTrendState.SUSTAINED_DOWN, changes
        return MarketTrendState.NEUTRAL, changes

    def _notify_transition(
        self,
        previous: MarketTrendState,
        snapshot: MarketTrendSnapshot,
    ) -> None:
        daily_falling = sum(
            value <= -self.drop_alert_pct
            for value in snapshot.daily_changes.values()
        )
        if snapshot.sample_count < 3 and daily_falling < 2:
            return
        if snapshot.state is MarketTrendState.NEUTRAL:
            if previous is MarketTrendState.NEUTRAL:
                return
            title = "🟡 A股短线趋势解除"
            summary = "此前的持续单边信号已恢复为震荡/中性。"
        elif snapshot.state is MarketTrendState.SUSTAINED_UP:
            title = "📈 A股出现明显持续上涨"
            summary = "主要指数连续约20分钟同向走强。"
        else:
            title = "📉 A股出现明显持续下跌"
            summary = (
                "主要指数相对昨收大幅下跌。"
                if daily_falling >= 2
                else "主要指数连续约20分钟同向走弱。"
            )

        changes = "  ".join(
            f"{name}{value:+.2f}%"
            for name, value in sorted(snapshot.cumulative_changes.items())
        )
        content = (
            f"{title}\n"
            f"⏰ {snapshot.sampled_at.strftime('%Y-%m-%d %H:%M')}\n"
            f"{summary}\n"
            f"区间变化：{changes or '-'}"
        )
        try:
            notifier = self._notifier
            if notifier is None:
                from src.notification import NotificationService

                notifier = NotificationService()
            notifier.send_with_results(content, route_type="alert", bypass_digest=True)
        except Exception as exc:  # noqa: BLE001 - monitoring must keep running.
            logger.warning("[IntradayMarket] trend notification failed: %s", exc)
