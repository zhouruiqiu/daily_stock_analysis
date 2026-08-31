# -*- coding: utf-8 -*-
"""通知聚合（digest）服务测试：缓冲、去重、紧急旁路、重启不重发、调度时点。"""

import contextlib
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.services.notification_digest_service import (
    NotificationDigestService,
    classify_urgency,
)
from src.storage import Base, NotificationDigestEvent


def _fake_db():
    """独立于 DatabaseManager 单例的内存库，只建 digest 表。"""

    class _Db:
        def __init__(self):
            self._engine = create_engine("sqlite://")
            Base.metadata.create_all(self._engine, tables=[NotificationDigestEvent.__table__])
            self._Session = sessionmaker(bind=self._engine)

        @contextlib.contextmanager
        def session_scope(self):
            session = self._Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    return _Db()


class _Sender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send_with_results(self, content, **kwargs):
        if self.fail:
            raise RuntimeError("channel down")
        self.sent.append((content, kwargs))
        return SimpleNamespace(success=True)


class ClassifyUrgencyTest(TestCase):
    def test_critical_severity_bypasses(self) -> None:
        self.assertTrue(classify_urgency(severity="critical", content="普通内容"))

    def test_urgent_content_markers_bypass(self) -> None:
        self.assertTrue(classify_urgency(content="📉 A股出现明显持续下跌\n主要指数相对昨收大幅下跌"))
        self.assertTrue(classify_urgency(content="⚠️ 胜宏科技 实时价跌破支撑 250.00"))

    def test_normal_content_is_buffered(self) -> None:
        self.assertFalse(classify_urgency(content="📊 A股盯盘速报\n共 7 只"))


class DigestServiceTest(TestCase):
    def setUp(self) -> None:
        self.db = _fake_db()
        self.service = NotificationDigestService(self.db)

    def test_record_buffers_normal_event(self) -> None:
        outcome = self.service.record_event(content="📊 盯盘速报\n内容", route_type="alert")
        self.assertTrue(outcome.buffered)
        self.assertFalse(outcome.duplicate)
        self.assertEqual(self.service.pending_count(), 1)

    def test_duplicate_key_is_ignored_until_sent(self) -> None:
        self.service.record_event(content="同一条推送", route_type="alert", dedup_key="k1")
        outcome = self.service.record_event(content="同一条推送", route_type="alert", dedup_key="k1")
        self.assertFalse(outcome.buffered)
        self.assertTrue(outcome.duplicate)
        self.assertEqual(self.service.pending_count(), 1)

    def test_flush_sends_and_marks_sent(self) -> None:
        self.service.record_event(content="盯盘速报 A", route_type="alert")
        self.service.record_event(content="选股结果 B", route_type="alert")
        sender = _Sender()
        stats = self.service.flush(slot="10:05", sender=sender)

        self.assertTrue(stats["sent"])
        self.assertEqual(stats["event_count"], 2)
        self.assertEqual(len(sender.sent), 1)  # 合并为一条简报
        self.assertIn("盯盘速报 A", sender.sent[0][0])
        self.assertIn("选股结果 B", sender.sent[0][0])
        self.assertEqual(self.service.pending_count(), 0)

    def test_flush_marks_urgent_routing_on_digest_itself(self) -> None:
        """简报本体以 severity=critical 发送（绕过聚合拦截）。"""
        self.service.record_event(content="盯盘速报 A", route_type="alert")
        sender = _Sender()
        self.service.flush(slot="11:35", sender=sender)
        self.assertEqual(sender.sent[0][1].get("severity"), "critical")
        self.assertEqual(sender.sent[0][1].get("route_type"), "alert")

    def test_failed_flush_keeps_events_for_retry(self) -> None:
        self.service.record_event(content="盯盘速报 A", route_type="alert")
        stats = self.service.flush(slot="10:05", sender=_Sender(fail=True))
        self.assertFalse(stats["sent"])
        self.assertEqual(self.service.pending_count(), 1)

        # 下一个时点重试成功
        stats2 = self.service.flush(slot="11:35", sender=_Sender())
        self.assertTrue(stats2["sent"])
        self.assertEqual(self.service.pending_count(), 0)

    def test_restart_does_not_resent_confirmed_events(self) -> None:
        """服务重启（新实例，同库）：已发送事件不再进简报。"""
        self.service.record_event(content="盯盘速报 A", route_type="alert")
        self.service.flush(slot="10:05", sender=_Sender())

        restarted = NotificationDigestService(self.db)
        stats = restarted.flush(slot="11:35", sender=_Sender())
        self.assertEqual(stats["event_count"], 0)

    def test_unsent_events_survive_restart_into_next_digest(self) -> None:
        self.service.record_event(content="盯盘速报 A", route_type="alert")
        # 重启前未 flush
        restarted = NotificationDigestService(self.db)
        self.assertEqual(restarted.pending_count(), 1)
        sender = _Sender()
        stats = restarted.flush(slot="11:35", sender=sender)
        self.assertEqual(stats["event_count"], 1)
        self.assertTrue(stats["sent"])

    def test_flush_truncates_overflow(self) -> None:
        service = NotificationDigestService(self.db, max_events=2)
        for i in range(4):
            service.record_event(content=f"事件 {i}", route_type="alert")
        sender = _Sender()
        stats = service.flush(slot="15:15", sender=sender)
        self.assertEqual(stats["truncated"], 2)
        self.assertIn("另有 2 条", sender.sent[0][0])

    def test_empty_flush_sends_nothing(self) -> None:
        sender = _Sender()
        stats = self.service.flush(slot="10:05", sender=sender)
        self.assertEqual(stats["event_count"], 0)
        self.assertEqual(len(sender.sent), 0)


class NotificationInterceptionTest(TestCase):
    """NotificationService 拦截层：不初始化完整服务，只测 digest 分支。"""

    def _service(self, db):
        from src.notification import NotificationService

        fake = SimpleNamespace(
            _config=SimpleNamespace(
                notification_digest_enabled=True,
                notification_digest_max_events=30,
            ),
            _digest_db_manager=db,
        )
        fake._get_digest_db_manager = NotificationService._get_digest_db_manager.__get__(fake)
        return fake, NotificationService

    def test_normal_alert_is_buffered(self) -> None:
        db = _fake_db()
        fake, cls = self._service(db)
        status = cls._maybe_buffer_notification_digest(
            fake, "📊 盯盘速报\n普通内容", route_type="alert"
        )
        self.assertEqual(status, "digest_buffered")
        self.assertEqual(NotificationDigestService(db).pending_count(), 1)

    def test_urgent_content_passes_through(self) -> None:
        db = _fake_db()
        fake, cls = self._service(db)
        status = cls._maybe_buffer_notification_digest(
            fake, "⚠️ 跌破支撑 250.00", route_type="alert"
        )
        self.assertIsNone(status)
        self.assertEqual(NotificationDigestService(db).pending_count(), 0)

    def test_disabled_config_passes_through(self) -> None:
        from src.notification import NotificationService

        fake = SimpleNamespace(
            _config=SimpleNamespace(notification_digest_enabled=False),
            _digest_db_manager=None,
        )
        status = NotificationService._maybe_buffer_notification_digest(
            fake, "普通内容", route_type="alert"
        )
        self.assertIsNone(status)

    def test_non_alert_route_passes_through(self) -> None:
        db = _fake_db()
        fake, cls = self._service(db)
        status = cls._maybe_buffer_notification_digest(
            fake, "普通内容", route_type="report"
        )
        self.assertIsNone(status)

    def test_digest_error_fails_open(self) -> None:
        class _BrokenDb:
            @contextlib.contextmanager
            def session_scope(self):
                raise RuntimeError("db broken")
                yield  # pragma: no cover

        fake, cls = self._service(_BrokenDb())
        status = cls._maybe_buffer_notification_digest(
            fake, "普通内容", route_type="alert"
        )
        self.assertIsNone(status)  # 出错放行立即发送


class SchedulerDigestSlotTest(TestCase):
    def test_digest_flush_runs_once_at_slot(self) -> None:
        from src.core.trading_calendar import MarketPhase
        from src.services.intraday_session_scheduler import IntradaySessionCoordinator

        events = []

        class _Noop:
            def __init__(self, name):
                self.name = name
                self.current_state = None

            def run_once(self, now=None):
                events.append(self.name)

        flushes = []

        def _fake_flush(self, *, slot, now=None, sender=None):
            flushes.append(slot)
            return {"slot": slot, "event_count": 1, "sent": True}

        config = SimpleNamespace(
            intraday_watch_enabled=False,
            intraday_market_monitor_enabled=False,
            intraday_screening_enabled=False,
            strategy_evaluation_enabled=False,
            notification_digest_enabled=True,
            notification_digest_times=["10:05"],
            notification_digest_max_events=30,
        )
        coordinator = IntradaySessionCoordinator(
            config_provider=lambda: config,
            market_monitor=_Noop("market"),
            screening_worker=_Noop("screening"),
            watch_worker=_Noop("watch"),
            phase_provider=lambda _now: MarketPhase.INTRADAY,
            task_launcher=lambda task: task(),
        )
        clock = [datetime(2026, 8, 31, 10, 5)]
        with patch(
            "src.services.notification_digest_service.NotificationDigestService.flush",
            _fake_flush,
        ), patch("src.storage.DatabaseManager", return_value=None):
            for _ in range(3):
                coordinator.tick(clock[0])
        self.assertEqual(flushes, ["10:05"])  # 同一时点只执行一次

    def test_digest_not_in_capabilities_when_disabled(self) -> None:
        from src.services.intraday_session_scheduler import intraday_capabilities_enabled

        config = SimpleNamespace(
            intraday_watch_enabled=False,
            intraday_market_monitor_enabled=False,
            intraday_screening_enabled=False,
            strategy_evaluation_enabled=False,
            notification_digest_enabled=True,
        )
        self.assertTrue(intraday_capabilities_enabled(config))
        config.notification_digest_enabled = False
        self.assertFalse(intraday_capabilities_enabled(config))
