# -*- coding: utf-8 -*-
"""指数行情数据新鲜度回归测试。

背景（2026-08-31 事故）：大盘复盘调用 get_main_indices() 未带
require_realtime=True，命中 Tushare 的 index_daily 日线型实现，
收盘后当日 bar 未更新时把上一交易日行情当作"今日"复盘。
本文件锁定两处"当日行情"调用点必须跳过日线型数据源。
"""

import sys
from unittest import TestCase
from unittest.mock import MagicMock, patch

for _mod in ("litellm", "google.generativeai", "google.genai", "anthropic"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _sample_index_row():
    return {
        "code": "sh000001",
        "name": "上证指数",
        "current": 3986.30,
        "change": 34.12,
        "change_pct": 0.86,
        "open": 3950.24,
        "high": 3988.00,
        "low": 3947.80,
        "prev_close": 3952.18,
        "volume": 1.0,
        "amount": 9704.0,
        "amplitude": 1.02,
    }


class MarketAnalyzerRealtimeIndexTest(TestCase):
    def test_review_requires_realtime_index_source(self) -> None:
        """复盘的 _get_main_indices 必须带 require_realtime=True。"""
        import src.market_analyzer as market_analyzer_module
        from src.market_analyzer import MarketAnalyzer

        with patch.object(
            market_analyzer_module, "get_config", return_value=MagicMock()
        ), patch.object(
            market_analyzer_module,
            "DataFetcherManager",
        ) as manager_cls:
            manager = MagicMock()
            manager.get_main_indices.return_value = [_sample_index_row()]
            manager_cls.return_value = manager

            analyzer = MarketAnalyzer(search_service=None, analyzer=None)
            indices = analyzer._get_main_indices()

        manager.get_main_indices.assert_called_once_with(
            region="cn",
            require_realtime=True,
        )
        self.assertEqual(len(indices), 1)
        self.assertEqual(indices[0].current, 3986.30)


class AgentMarketToolsRealtimeIndexTest(TestCase):
    def test_agent_tool_requires_realtime_index_source(self) -> None:
        """agent 的行情工具必须带 require_realtime=True。"""
        from src.agent.tools import market_tools

        manager = MagicMock()
        manager.get_main_indices.return_value = [_sample_index_row()]
        with patch.object(market_tools, "_get_fetcher_manager", return_value=manager):
            result = market_tools._handle_get_market_indices(region="cn")

        manager.get_main_indices.assert_called_once_with(
            region="cn",
            require_realtime=True,
        )
        self.assertEqual(result["indices_count"], 1)
