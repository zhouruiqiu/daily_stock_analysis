# -*- coding: utf-8 -*-
"""Behavioral contracts for built-in and custom screening strategies."""

from pathlib import Path

import pytest
import pandas as pd
import yaml

from src.services.screening.filter import apply_hard_filters, requires_daily_features
from src.services.screening.models import HardFilterConfig
from src.services.screening.strategy import list_strategies, load_all_strategies, load_strategy


REPO_ROOT = Path(__file__).resolve().parents[1]
STRATEGY_DIR = REPO_ROOT / "src" / "services" / "screening" / "strategies"


def _write_strategy(
    tmp_path: Path,
    *,
    hard_filters: dict[str, object] | None = None,
    factor_weights: dict[str, float] | None = None,
    tech_weight: float | None = None,
) -> Path:
    screening: dict[str, object] = {
        "enabled": True,
        "market_scope": ["cn"],
        "hard_filters": hard_filters or {},
    }
    if factor_weights is not None:
        screening["factor_weights"] = factor_weights
    if tech_weight is not None:
        screening["tech_weight"] = tech_weight
    path = tmp_path / "custom.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": "custom",
                "display_name": "Custom",
                "description": "custom strategy",
                "screening": screening,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def test_load_strategy_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.yaml"
    path.write_text(
        """
name: duplicate
screening:
  enabled: true
  hard_filters:
    range_20d_pct_max: 40
    range_20d_pct_max: 45
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML key.*range_20d_pct_max"):
        load_strategy(path)


@pytest.mark.parametrize(
    ("filters", "field"),
    [
        ({"price_min": 20, "price_max": 10}, "price"),
        ({"change_60d_min": 5, "change_60d_max": -5}, "change_60d"),
    ],
)
def test_load_strategy_rejects_inverted_filter_ranges(
    tmp_path: Path,
    filters: dict[str, float],
    field: str,
) -> None:
    path = _write_strategy(tmp_path, hard_filters=filters, factor_weights={"value": 1.0})

    with pytest.raises(ValueError, match=rf"{field}.*min.*max"):
        load_strategy(path)


def test_load_strategy_rejects_negative_factor_weights(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, factor_weights={"value": -0.1})

    with pytest.raises(ValueError, match="factor_weights.*non-negative"):
        load_strategy(path)


def test_load_strategy_keeps_legacy_tech_weight_only_config(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, tech_weight=0.6)

    strategy = load_strategy(path)

    assert strategy.screening.tech_weight == 0.6
    assert strategy.screening.factor_weights == {}


def test_load_strategy_exposes_evaluation_profile(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, factor_weights={"value": 1.0})
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace(
            "screening:\n",
            "evaluation_profile:\n"
            "  cohort: opening_0945\n"
            "  risk_tier: high\n"
            "  max_position_pct: 0.08\n\n"
            "screening:\n",
        ),
        encoding="utf-8",
    )

    strategy = load_strategy(path)

    assert strategy.evaluation_profile.cohort == "opening_0945"
    assert strategy.evaluation_profile.risk_tier == "high"
    assert strategy.evaluation_profile.max_position_pct == 0.08


def test_load_strategy_rejects_unknown_evaluation_cohort(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, factor_weights={"value": 1.0})
    content = path.read_text(encoding="utf-8")
    path.write_text(
        content.replace(
            "screening:\n",
            "evaluation_profile:\n  cohort: lunchtime\n  risk_tier: medium\n"
            "  max_position_pct: 0.10\n\nscreening:\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="evaluation_profile.cohort"):
        load_strategy(path)


def test_builtin_strategies_use_explicit_factor_weights_only() -> None:
    for path in STRATEGY_DIR.glob("*.yaml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        screening = raw["screening"]

        assert "tech_weight" not in screening, path.name
        assert screening.get("factor_weights"), path.name


def test_daily_quality_filter_requires_daily_features_and_rejects_low_quality() -> None:
    filters = HardFilterConfig(daily_quality_score_min=80.0)
    frame = pd.DataFrame(
        [
            {"code": "good", "name": "Good", "daily_quality_score": 95.0},
            {"code": "bad", "name": "Bad", "daily_quality_score": 60.0},
        ]
    )

    assert requires_daily_features(filters) is True
    assert apply_hard_filters(frame, filters)["code"].tolist() == ["good"]


def test_theme_evidence_filter_rejects_price_only_candidate() -> None:
    filters = HardFilterConfig(require_theme_evidence=True)
    frame = pd.DataFrame(
        [
            {"code": "price-only", "name": "Price Only", "change_pct": 7.2},
            {
                "code": "theme-backed",
                "name": "Theme Backed",
                "change_pct": 6.8,
                "board_heat_score": 78.0,
                "board_heat_summary": "机器人 热度78",
            },
        ]
    )

    assert apply_hard_filters(frame, filters)["code"].tolist() == ["theme-backed"]


def test_strategy_metadata_reports_daily_quality_requirement(tmp_path: Path) -> None:
    _write_strategy(
        tmp_path,
        hard_filters={"daily_quality_score_min": 80.0},
        factor_weights={"stability": 1.0},
    )

    info = list_strategies(tmp_path)[0]

    assert info.requires_daily_features is True
    assert "daily_quality_score" in info.required_daily_fields


def test_oversold_reversal_requires_confirmed_daily_oversold_conditions() -> None:
    strategy = load_strategy(STRATEGY_DIR / "oversold_reversal.yaml")
    filters = strategy.screening.hard_filters

    assert filters.change_60d_min == -35.0
    assert filters.change_60d_max == -8.0
    assert filters.rsi_status_whitelist == ["oversold"]
    assert filters.volatility_20d_pct_max == 55.0
    assert filters.max_drawdown_20d_pct_min == -30.0
    assert filters.atr_20_pct_max == 8.0
    assert filters.daily_quality_score_min == 80.0
    assert requires_daily_features(filters) is True


def test_dragon_board_exposes_executable_theme_momentum_contract() -> None:
    strategy = load_strategy(STRATEGY_DIR / "dragon_board.yaml")
    filters = strategy.screening.hard_filters

    assert strategy.display_name == "强势题材（实验）"
    assert "experimental" in strategy.tags
    assert strategy.analysis_skills == ["hot_theme", "bull_trend"]
    assert filters.change_60d_max == 30.0
    assert filters.breakout_20d_pct_min == -1.0
    assert filters.volume_ratio_20d_min == 1.3
    assert filters.daily_quality_score_min == 80.0


def test_dragon_board_requires_realtime_theme_confirmation() -> None:
    strategy = load_strategy(STRATEGY_DIR / "dragon_board.yaml")
    screening = strategy.screening
    filters = screening.hard_filters

    assert strategy.version == "1.2"
    assert screening.snapshot_requirements == {"mode": "realtime"}
    assert filters.require_theme_evidence is True
    assert filters.require_ma_bullish is True
    assert filters.change_60d_min == 5.0
    assert filters.change_60d_max == 30.0
    assert filters.max_drawdown_20d_pct_min == -12.0
    assert filters.atr_20_pct_max == 7.0
    assert filters.body_pct_min is None
    assert screening.factor_weights == {
        "momentum": 0.25,
        "activity": 0.20,
        "theme_heat": 0.25,
        "topic_alignment": 0.20,
        "liquidity": 0.10,
    }
    assert screening.scoring_profile["theme_heat_unknown_score"] == 15.0
    assert screening.scoring_profile["topic_alignment_unknown_score"] == 10.0


def test_value_strategy_names_match_structured_factor_capabilities() -> None:
    strategies = load_all_strategies(STRATEGY_DIR)

    assert strategies["quality_value"].display_name == "低估值稳健"
    assert strategies["blue_chip_income"].display_name == "大盘低估值防守"
