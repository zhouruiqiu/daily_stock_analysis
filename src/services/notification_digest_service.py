# -*- coding: utf-8 -*-
"""通知聚合服务：把分散的盘中推送缓冲为固定时点的综合简报。

设计要点（对应实施计划 Task 1/2）：
- 普通事件先落库（notification_digest_events），到 10:05 / 11:35 / 15:15 合并发送。
- 紧急事件（severity=critical，或内容含急跌/跌破支撑标记）不缓冲，由调用方立即发送。
- 按 dedup_key 去重：同 key 事件在未发送前只保留一条。
- 发送成功才标记 sent_at；服务重启后未发送事件进入下一次简报，已发送的不重复推。
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 内容级紧急标记：现有盘中组件未显式传 severity，命中即视为必须立即送达
URGENT_CONTENT_MARKERS: tuple[str, ...] = (
    "相对昨收大幅下跌",
    "跌破支撑",
    "跌破有效支撑",
    "数据源全部不可用",
)

_SLOT_LABELS: Dict[str, str] = {
    "10:05": "📈 开盘综合简报",
    "11:35": "🍽 午盘综合简报",
    "15:15": "📉 收盘综合简报",
}
_FALLBACK_SLOT_LABEL = "📬 通知简报"

_EVENT_TYPE_ORDER: List[str] = [
    "market_state",
    "strategy_evaluation",
    "portfolio_watch",
    "dynamic_screening",
    "general",
]
_EVENT_TYPE_LABELS: Dict[str, str] = {
    "market_state": "大盘状态",
    "market_crash": "大盘急跌",
    "support_breach": "支撑跌破",
    "portfolio_watch": "持仓盯盘",
    "dynamic_screening": "动态选股",
    "strategy_evaluation": "盘前策略",
    "strategy_outcome": "策略后验",
    "decision_signal": "决策信号",
    "system_failure": "系统故障",
    "general": "其他",
}

DEFAULT_MAX_EVENTS = 30


@dataclass(frozen=True)
class DigestRecordOutcome:
    """record_event 的结果，指导调用方是缓冲了还是要立即发。"""

    buffered: bool
    duplicate: bool


def classify_urgency(
    *,
    severity: Optional[str] = None,
    content: str = "",
) -> bool:
    """判断一条告警是否必须绕过聚合立即发送。"""
    if (severity or "").strip().lower() in {"critical", "urgent", "emergency"}:
        return True
    text = content or ""
    return any(marker in text for marker in URGENT_CONTENT_MARKERS)


def infer_event_type(content: str) -> str:
    """从内容推断事件类型（用于简报内分组标题）。"""
    text = content or ""
    if "盯盘" in text or "交易计划" in text:
        return "portfolio_watch"
    if "选股" in text or "题材" in text:
        return "dynamic_screening"
    if "盘前评测" in text or "策略周榜" in text or "全策略" in text:
        return "strategy_evaluation"
    if "A股短线趋势" in text or "大盘" in text:
        return "market_state"
    return "general"


def build_dedup_key(route_type: Optional[str], content: str) -> str:
    raw = f"{route_type or ''}|{content or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


class NotificationDigestService:
    """缓冲、去重、合并并发送通知事件。"""

    def __init__(self, db_manager: Any, *, max_events: int = DEFAULT_MAX_EVENTS):
        self.db = db_manager
        self.max_events = max(1, int(max_events))

    # ---- 记录 ----

    def record_event(
        self,
        *,
        content: str,
        route_type: Optional[str] = None,
        severity: Optional[str] = None,
        dedup_key: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
    ) -> DigestRecordOutcome:
        """记录一条待聚合事件。重复 key（未发送的）返回 duplicate=True。"""
        from src.storage import NotificationDigestEvent

        text = (content or "").strip()
        if not text:
            return DigestRecordOutcome(buffered=False, duplicate=False)

        key = (dedup_key or "").strip() or build_dedup_key(route_type, text)
        title = text.splitlines()[0][:255] if text else ""
        event_type = infer_event_type(text)
        sev = (severity or "").strip().lower() or "normal"
        occurred = occurred_at or datetime.now()

        with self.db.session_scope() as session:
            existing = (
                session.query(NotificationDigestEvent)
                .filter(
                    NotificationDigestEvent.dedup_key == key,
                    NotificationDigestEvent.sent_at.is_(None),
                )
                .one_or_none()
            )
            if existing is not None:
                return DigestRecordOutcome(buffered=False, duplicate=True)
            session.add(
                NotificationDigestEvent(
                    dedup_key=key,
                    event_type=event_type,
                    severity=sev,
                    title=title,
                    content=text,
                    occurred_at=occurred.replace(tzinfo=None),
                )
            )
        return DigestRecordOutcome(buffered=True, duplicate=False)

    # ---- 读取 ----

    def pending_count(self) -> int:
        from src.storage import NotificationDigestEvent

        with self.db.session_scope() as session:
            return (
                session.query(NotificationDigestEvent)
                .filter(NotificationDigestEvent.sent_at.is_(None))
                .count()
            )

    # ---- 发送 ----

    def flush(
        self,
        *,
        slot: str,
        now: Optional[datetime] = None,
        sender: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """把所有未发送事件合并成一份简报并立即发送。

        sender 需要提供 send_with_results(content, route_type=...) 接口
        （NotificationService 本身）。为避免递归聚合，digest 服务持有
        _digest_flush_in_progress 标记，发送入口据此放行。
        """
        from src.storage import NotificationDigestEvent

        flushed_at = now or datetime.now()
        with self.db.session_scope() as session:
            rows = (
                session.query(NotificationDigestEvent)
                .filter(NotificationDigestEvent.sent_at.is_(None))
                .order_by(NotificationDigestEvent.occurred_at.asc(), NotificationDigestEvent.id.asc())
                .all()
            )
            # session 关闭后实例属性不可再访问（expire_on_commit），此处提取纯数据
            events = [
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "title": row.title,
                    "content": row.content,
                    "occurred_at": row.occurred_at,
                }
                for row in rows
            ]
            event_ids = [event["id"] for event in events]

        stats: Dict[str, Any] = {
            "slot": slot,
            "event_count": len(event_ids),
            "sent": False,
            "truncated": 0,
        }
        if not events:
            return stats

        visible, truncated = events[: self.max_events], max(0, len(events) - self.max_events)
        stats["truncated"] = truncated
        content = self._format_digest(visible, truncated, slot, flushed_at)

        if sender is None:
            from src.notification import NotificationService

            sender = NotificationService()

        try:
            result = sender.send_with_results(
                content,
                route_type="alert",
                severity="critical",  # 简报本体直接送达，绕过聚合
            )
            sent_ok = bool(getattr(result, "success", False))
        except Exception as exc:  # noqa: BLE001 - 发送失败保留事件，下个时点重试
            logger.warning("[NotificationDigest] 简报发送失败（事件保留待重试）: %s", exc)
            sent_ok = False

        stats["sent"] = sent_ok
        if sent_ok:
            self._mark_sent(event_ids, slot, flushed_at)
        return stats

    # ---- 内部 ----

    def _mark_sent(self, event_ids: List[int], slot: str, sent_at: datetime) -> None:
        from src.storage import NotificationDigestEvent

        if not event_ids:
            return
        with self.db.session_scope() as session:
            (
                session.query(NotificationDigestEvent)
                .filter(
                    NotificationDigestEvent.id.in_(event_ids),
                    NotificationDigestEvent.sent_at.is_(None),
                )
                .update(
                    {
                        NotificationDigestEvent.sent_at: sent_at.replace(tzinfo=None),
                        NotificationDigestEvent.digest_slot: slot,
                    },
                    synchronize_session=False,
                )
            )

    def _format_digest(
        self,
        events: List[Any],
        truncated: int,
        slot: str,
        flushed_at: datetime,
    ) -> str:
        now_str = flushed_at.strftime("%Y-%m-%d %H:%M")
        header = _SLOT_LABELS.get(slot, f"{_FALLBACK_SLOT_LABEL} · {slot}")
        lines: List[str] = [
            header,
            f"⏰ {now_str} · 合并 {len(events)} 条推送",
            "━" * 16,
            "",
        ]

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for event in events:
            grouped.setdefault(event.get("event_type") or "general", []).append(event)

        def _type_sort_key(event_type: str) -> tuple:
            try:
                return (_EVENT_TYPE_ORDER.index(event_type), event_type)
            except ValueError:
                return (len(_EVENT_TYPE_ORDER), event_type)

        for event_type in sorted(grouped, key=_type_sort_key):
            label = _EVENT_TYPE_LABELS.get(event_type, event_type)
            lines.append(f"【{label}】")
            for event in grouped[event_type]:
                occurred = event.get("occurred_at")
                time_tag = occurred.strftime("%H:%M") if occurred else "--:--"
                body = (event.get("content") or "").strip()
                from src.services.notification_privacy import contains_portfolio_details

                if event_type == "portfolio_watch" and contains_portfolio_details(body):
                    # Old persisted events must not leak details after upgrading the renderer.
                    body = "历史账户明细已隐藏，请登录 Web 查看最新计划。"
                lines.append(f"· {time_tag} {body}")
                lines.append("")
        if truncated:
            lines.append(f"… 另有 {truncated} 条较早事件未展示")
        return "\n".join(lines).rstrip()
