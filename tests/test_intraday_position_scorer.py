# -*- coding: utf-8 -*-
from types import SimpleNamespace

from src.services.intraday_position_scorer import (
    IntradayPositionScorer,
    normalize_position_symbol,
)
from src.stock_analyzer import (
    MACDStatus,
    RSIStatus,
    TrendAnalysisResult,
    TrendStatus,
    VolumeStatus,
)


def _trend(**overrides):
    values = {
        "code": "600036",
        "trend_status": TrendStatus.BULL,
        "ma_alignment": "多头排列 MA5>MA10>MA20",
        "current_price": 42.0,
        "ma5": 41.0,
        "ma10": 40.0,
        "ma20": 39.0,
        "bias_ma5": 2.4,
        "macd_status": MACDStatus.BULLISH,
        "rsi_6": 58.0,
        "rsi_status": RSIStatus.STRONG_BUY,
        "volume_status": VolumeStatus.HEAVY_VOLUME_UP,
        "support_levels": [41.0, 40.0],
        "resistance_levels": [45.0],
    }
    values.update(overrides)
    return TrendAnalysisResult(**values)


def test_held_strong_stock_includes_cost_and_hold_recommendation():
    result = IntradayPositionScorer().score(
        _trend(),
        SimpleNamespace(price=42.0, change_pct=2.0, volume_ratio=1.5),
        {"symbol": "600036.SH", "avg_cost": 40.0, "quantity": 600},
    )

    assert result.is_held is True
    assert result.score >= 65
    assert "持有" in result.recommendation
    assert result.pnl_pct == 5.0
    assert result.defense_price is not None
    assert result.positive_reasons


def test_held_weak_stock_recommends_reducing_risk():
    result = IntradayPositionScorer().score(
        _trend(
            trend_status=TrendStatus.STRONG_BEAR,
            ma_alignment="强势空头排列，均线发散下行",
            current_price=32.0,
            ma5=34.0,
            ma10=36.0,
            ma20=38.0,
            bias_ma5=-5.9,
            macd_status=MACDStatus.DEATH_CROSS,
            rsi_6=28.0,
            rsi_status=RSIStatus.OVERSOLD,
            volume_status=VolumeStatus.HEAVY_VOLUME_DOWN,
        ),
        SimpleNamespace(price=32.0, change_pct=-5.0, volume_ratio=2.0),
        {"symbol": "600036", "avg_cost": 40.0, "quantity": 600},
    )

    assert result.score < 50
    assert result.recommendation in {"考虑减仓", "高风险"}
    assert result.risk_level == "高"
    assert result.negative_reasons


def test_non_held_uses_observation_wording_and_no_cost_fields():
    result = IntradayPositionScorer().score(
        _trend(),
        SimpleNamespace(price=42.0, change_pct=1.0, volume_ratio=1.2),
    )

    assert result.is_held is False
    assert "观察" in result.recommendation
    assert "持有" not in result.recommendation
    assert result.avg_cost is None
    assert result.pnl_pct is None


def test_score_is_always_bounded():
    scorer = IntradayPositionScorer()
    high = scorer.score(_trend(), SimpleNamespace(price=42.0))
    low = scorer.score(
        _trend(trend_status=TrendStatus.STRONG_BEAR, macd_status=MACDStatus.DEATH_CROSS),
        SimpleNamespace(price=1.0),
        {"avg_cost": 100.0, "quantity": 1},
    )

    assert 0 <= high.score <= 100
    assert 0 <= low.score <= 100


def test_normalize_position_symbol_matches_a_share_suffixes():
    assert normalize_position_symbol("600036.SH") == "600036"
    assert normalize_position_symbol("sz301047") == "301047"
    assert normalize_position_symbol("300136") == "300136"


def test_held_stock_below_nearest_support_exposes_breach_fields():
    result = IntradayPositionScorer().score(
        _trend(support_levels=[40.2, 39.0], current_price=39.8),
        SimpleNamespace(price=39.8, change_pct=-2.0),
        {"symbol": "600036", "avg_cost": 40.0, "quantity": 600},
    )

    assert result.nearest_support == 40.2
    assert result.support_breached is True
    assert result.support_break_pct == 1.0


def test_support_breach_requires_held_position_and_realtime_price():
    trend = _trend(support_levels=[40.2, 39.0], current_price=39.0)

    watchlist = IntradayPositionScorer().score(
        trend,
        SimpleNamespace(price=39.8, change_pct=-2.0),
    )
    missing_realtime = IntradayPositionScorer().score(
        trend,
        None,
        {"symbol": "600036", "avg_cost": 40.0, "quantity": 600},
    )

    assert watchlist.support_breached is False
    assert missing_realtime.support_breached is False
