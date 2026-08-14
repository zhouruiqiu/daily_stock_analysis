# -*- coding: utf-8 -*-
"""
Unit tests for src.services.intraday_watch_worker.

Covers: enabled gate, off-hours skip, intraday happy path (analyze + notify),
empty STOCK_LIST skip, and graceful handling when realtime quote is None.
"""
import os
import sys
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import Config
from src.core.trading_calendar import MarketPhase
from src.services.intraday_watch_worker import (
    IntradayWatchWorker,
    run_intraday_watch_loop,
)
from src.stock_analyzer import TrendAnalysisResult


def _config(**overrides):
    overrides.setdefault("stock_list", [])
    return Config(**overrides)


def _fake_df(n=25):
    import pandas as pd

    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=n),
            "open": 10.0,
            "high": 11.0,
            "low": 9.0,
            "close": 10.5,
            "volume": 1000,
        }
    )


def _ok_dispatch():
    return mock.MagicMock(success=True)


class TestIntradayWatchWorker(unittest.TestCase):

    def test_disabled_skips_without_doing_anything(self):
        worker = IntradayWatchWorker()  # 默认 intraday_watch_enabled=False
        stats = worker.run_once()
        self.assertEqual(stats["analyzed"], 0)
        self.assertEqual(stats["notified"], 0)
        self.assertEqual(stats["skipped"], 0)

    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_off_hours_skipped_and_no_fetch(self, mock_phase):
        mock_phase.return_value = MarketPhase.POSTMARKET
        cfg = _config(intraday_watch_enabled=True, stock_list=["600519"])
        fetcher = mock.MagicMock()
        worker = IntradayWatchWorker(config_provider=lambda: cfg, fetcher_manager=fetcher)

        stats = worker.run_once()

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["analyzed"], 0)
        fetcher.get_daily_data.assert_not_called()

    @mock.patch("src.stock_analyzer.StockTrendAnalyzer.analyze")
    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_intraday_analyzes_and_notifies(self, mock_phase, mock_analyze):
        mock_phase.return_value = MarketPhase.INTRADAY
        mock_analyze.return_value = TrendAnalysisResult(code="600519")
        cfg = _config(intraday_watch_enabled=True, stock_list=["600519"])

        quote = mock.MagicMock(
            price=1689.5, change_pct=1.23, change_amount=20.5,
            name="贵州茅台", volume_ratio=1.8, turnover_rate=0.3,
        )
        fetcher = mock.MagicMock()
        fetcher.get_daily_data.return_value = (_fake_df(), "tencent")
        fetcher.get_realtime_quote.return_value = quote
        notifier = mock.MagicMock()
        notifier.send_with_results.return_value = _ok_dispatch()

        worker = IntradayWatchWorker(
            config_provider=lambda: cfg,
            fetcher_manager=fetcher,
            notifier=notifier,
            position_provider=lambda: {
                "600519": {"symbol": "600519.SH", "avg_cost": 1600, "quantity": 100}
            },
        )
        stats = worker.run_once()

        self.assertEqual(stats["analyzed"], 1)
        self.assertEqual(stats["notified"], 1)
        notifier.send_with_results.assert_called_once()
        content = notifier.send_with_results.call_args.args[0]
        self.assertIn("600519", content)
        self.assertIn("盯盘速报", content)
        self.assertIn("综合评分", content)
        self.assertIn("持仓", content)
        self.assertIn("成本", content)
        self.assertEqual(
            notifier.send_with_results.call_args.kwargs.get("route_type"), "alert"
        )

    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_empty_stock_list_skips(self, mock_phase):
        mock_phase.return_value = MarketPhase.INTRADAY
        cfg = _config(intraday_watch_enabled=True, stock_list=[])
        worker = IntradayWatchWorker(config_provider=lambda: cfg)

        stats = worker.run_once()

        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["analyzed"], 0)

    @mock.patch("src.stock_analyzer.StockTrendAnalyzer.analyze")
    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_cn_session_boundary_slots_are_allowed(self, mock_phase, mock_analyze):
        mock_analyze.return_value = TrendAnalysisResult(code="600519")
        cfg = _config(intraday_watch_enabled=True, stock_list=["600519"])
        fetcher = mock.MagicMock()
        fetcher.get_daily_data.return_value = (_fake_df(), "tushare")
        fetcher.get_realtime_quote.return_value = None
        notifier = mock.MagicMock()
        notifier.send_with_results.return_value = _ok_dispatch()
        worker = IntradayWatchWorker(
            config_provider=lambda: cfg,
            fetcher_manager=fetcher,
            notifier=notifier,
            position_provider=lambda: {},
        )

        for phase, current in (
            (MarketPhase.LUNCH_BREAK, datetime(2026, 8, 14, 11, 30)),
            (MarketPhase.POSTMARKET, datetime(2026, 8, 14, 15, 0)),
        ):
            with self.subTest(current=current):
                mock_phase.return_value = phase
                stats = worker.run_once(now=current)
                self.assertEqual(stats["analyzed"], 1)

        self.assertEqual(notifier.send_with_results.call_count, 2)

    @mock.patch("src.stock_analyzer.StockTrendAnalyzer.analyze")
    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_missing_realtime_quote_still_analyzes(self, mock_phase, mock_analyze):
        mock_phase.return_value = MarketPhase.INTRADAY
        mock_analyze.return_value = TrendAnalysisResult(code="000858")
        cfg = _config(intraday_watch_enabled=True, stock_list=["000858"])

        fetcher = mock.MagicMock()
        fetcher.get_daily_data.return_value = (_fake_df(), "efinance")
        fetcher.get_realtime_quote.return_value = None  # 实时行情拿不到
        notifier = mock.MagicMock()
        notifier.send_with_results.return_value = _ok_dispatch()

        worker = IntradayWatchWorker(
            config_provider=lambda: cfg, fetcher_manager=fetcher, notifier=notifier
        )
        stats = worker.run_once()

        self.assertEqual(stats["analyzed"], 1)
        self.assertEqual(stats["notified"], 1)

    @mock.patch("src.stock_analyzer.StockTrendAnalyzer.analyze")
    @mock.patch("src.core.trading_calendar.infer_market_phase")
    def test_portfolio_failure_degrades_to_watchlist_observation(
        self, mock_phase, mock_analyze
    ):
        mock_phase.return_value = MarketPhase.INTRADAY
        mock_analyze.return_value = TrendAnalysisResult(code="000858")
        cfg = _config(intraday_watch_enabled=True, stock_list=["000858"])
        fetcher = mock.MagicMock()
        fetcher.get_daily_data.return_value = (_fake_df(), "efinance")
        fetcher.get_realtime_quote.return_value = mock.MagicMock(
            price=88.0, change_pct=1.0, name="五粮液"
        )
        notifier = mock.MagicMock()
        notifier.send_with_results.return_value = _ok_dispatch()
        worker = IntradayWatchWorker(
            config_provider=lambda: cfg,
            fetcher_manager=fetcher,
            notifier=notifier,
            position_provider=mock.MagicMock(side_effect=RuntimeError("db unavailable")),
        )

        stats = worker.run_once()

        self.assertEqual(stats["notified"], 1)
        content = notifier.send_with_results.call_args.args[0]
        self.assertIn("自选观察", content)
        self.assertNotIn("继续持有", content)


class TestIntradayWatchLoop(unittest.TestCase):

    def test_disabled_returns_nonzero_without_creating_worker(self):
        cfg = _config(intraday_watch_enabled=False)
        worker_factory = mock.MagicMock()

        exit_code = run_intraday_watch_loop(
            config_provider=lambda: cfg,
            worker_factory=worker_factory,
        )

        self.assertEqual(exit_code, 2)
        worker_factory.assert_not_called()

    def test_polls_wall_clock_coordinator_every_30_seconds(self):
        cfg = _config(
            intraday_watch_enabled=True,
            intraday_watch_interval_minutes=30,
        )
        coordinator = mock.MagicMock()
        coordinator.tick.return_value = {"executed": []}
        coordinator_factory = mock.MagicMock(return_value=coordinator)
        stop_event = mock.MagicMock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True

        exit_code = run_intraday_watch_loop(
            config_provider=lambda: cfg,
            coordinator_factory=coordinator_factory,
            stop_event=stop_event,
        )

        self.assertEqual(exit_code, 0)
        coordinator_factory.assert_called_once()
        coordinator.tick.assert_called_once_with()
        stop_event.wait.assert_called_once_with(30)


if __name__ == "__main__":
    unittest.main()
