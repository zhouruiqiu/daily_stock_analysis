# Screening Strategy Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make built-in screening strategy names, executable filters, YAML validation, and documented capabilities agree without claiming unmeasured performance gains.

**Architecture:** Keep the existing snapshot → daily enrichment → factor ranking pipeline. Add strict validation at the YAML loader boundary, add one reusable daily-quality hard filter, and correct built-in strategy YAML so every advertised deterministic condition maps to a real field. Preserve custom strategy compatibility and existing API contracts.

**Tech Stack:** Python 3.10+, PyYAML, dataclasses, pandas, pytest, YAML strategy resources.

## Global Constraints

- Do not add a data source or change an API response/request contract.
- Do not modify `strategies/*.yaml`, portfolio files, or `apps/dsa-web/src/pages/StockScreeningPage.tsx`.
- Do not execute `git commit`, `git push`, branch changes, or history rewrites.
- Preserve the legacy custom-strategy path where `tech_weight` is used only when `factor_weights` is absent.
- Do not claim win-rate or return improvement; this phase corrects deterministic contracts only.
- Keep `[Unreleased]` changelog entries flat using `- [类型] 描述`.

---

### Task 1: Reject malformed strategy YAML at the loader boundary

**Files:**
- Create: `tests/test_screening_strategy_contracts.py`
- Modify: `src/services/screening/strategy.py`

**Interfaces:**
- Consumes: `load_strategy(filepath: Path) -> Strategy`.
- Produces: strict duplicate-key detection and validation helpers called only by `load_strategy`.

- [x] **Step 1: Write failing loader contract tests**

Add tests that write temporary YAML files and assert:

```python
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
    tmp_path: Path, filters: dict[str, float], field: str
) -> None:
    path = _write_strategy(tmp_path, hard_filters=filters)
    with pytest.raises(ValueError, match=rf"{field}.*min.*max"):
        load_strategy(path)


def test_load_strategy_rejects_invalid_factor_weights(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, factor_weights={"value": -0.1})
    with pytest.raises(ValueError, match="factor_weights.*non-negative"):
        load_strategy(path)


def test_load_strategy_keeps_legacy_tech_weight_only_config(tmp_path: Path) -> None:
    path = _write_strategy(tmp_path, tech_weight=0.6, factor_weights=None)
    strategy = load_strategy(path)
    assert strategy.screening.tech_weight == 0.6
    assert strategy.screening.factor_weights == {}
```

- [x] **Step 2: Run tests and verify the new contracts fail**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py -v
```

Expected: duplicate keys load silently and invalid bounds/weights are accepted.

- [x] **Step 3: Implement strict loading and validation**

In `strategy.py`:

```python
class _UniqueKeySafeLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping
```

Register the constructor only on `_UniqueKeySafeLoader`, load through a focused `_load_strategy_yaml(filepath)` helper, then validate:

- every configured `*_min` with a matching `*_max` has `min <= max`;
- `factor_weights` is a mapping;
- every weight is finite and non-negative;
- at least one configured weight is positive.

Wrap validation errors with the strategy filename while preserving the offending key.

- [x] **Step 4: Run the loader contract tests**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py -v
```

Expected: all Task 1 tests pass.

---

### Task 2: Add a deterministic daily-data quality hard filter

**Files:**
- Modify: `src/services/screening/models.py`
- Modify: `src/services/screening/filter.py`
- Modify: `src/services/screening/strategy.py`
- Modify: `tests/test_screening_strategy_contracts.py`

**Interfaces:**
- Produces: `HardFilterConfig.daily_quality_score_min: float | None`.
- Consumers: `apply_hard_filters`, `hard_filter_rejection_summary`, `hard_filter_waterfall`, `requires_daily_features`, `without_daily_filters`, and strategy catalog metadata.

- [x] **Step 1: Write failing filter tests**

```python
def test_daily_quality_filter_requires_daily_features_and_rejects_low_quality() -> None:
    filters = HardFilterConfig(daily_quality_score_min=80.0)
    assert requires_daily_features(filters) is True

    frame = pd.DataFrame(
        [
            {"code": "good", "daily_quality_score": 95.0},
            {"code": "bad", "daily_quality_score": 60.0},
        ]
    )
    assert apply_hard_filters(frame, filters)["code"].tolist() == ["good"]


def test_strategy_metadata_reports_daily_quality_requirement(tmp_path: Path) -> None:
    path = _write_strategy(
        tmp_path,
        hard_filters={"daily_quality_score_min": 80.0},
        factor_weights={"stability": 1.0},
    )
    info = list_strategies(tmp_path)[0]
    assert info.requires_daily_features is True
    assert "daily_quality_score" in info.required_daily_fields
```

- [x] **Step 2: Run the focused tests and verify failure**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py -k daily_quality -v
```

Expected: `HardFilterConfig` does not accept `daily_quality_score_min`.

- [x] **Step 3: Wire the new field through every filter surface**

Add `daily_quality_score_min` to the dataclass, `_DAILY_FILTER_DEFAULTS`, the direct filter, rejection summary, waterfall, `requires_daily_features`, and `_required_daily_fields`. Use the same `_filter_min(..., ["daily_quality_score"], value)` behavior as other numeric filters so missing required data fails explicitly.

- [x] **Step 4: Run the focused tests**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py -k daily_quality -v
```

Expected: both daily-quality tests pass.

---

### Task 3: Correct built-in strategy semantics

**Files:**
- Modify: `src/services/screening/strategies/dragon_board.yaml`
- Modify: `src/services/screening/strategies/oversold_reversal.yaml`
- Modify: `src/services/screening/strategies/quality_value.yaml`
- Modify: `src/services/screening/strategies/blue_chip_income.yaml`
- Modify: the remaining `src/services/screening/strategies/*.yaml` files only to remove ineffective built-in `tech_weight` keys
- Modify: `tests/test_screening_strategy_contracts.py`
- Modify: `tests/test_builtin_screening_engine.py`

**Interfaces:**
- Consumes: strict loader and `daily_quality_score_min` from Tasks 1-2.
- Produces: 11 valid built-in strategies with honest descriptions and executable filters.

- [x] **Step 1: Write failing built-in strategy contract tests**

Assert the following exact contracts:

```python
def test_builtin_strategies_do_not_publish_ineffective_tech_weight() -> None:
    for path in STRATEGY_DIR.glob("*.yaml"):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "tech_weight" not in raw["screening"], path.name


def test_oversold_reversal_is_a_real_daily_oversold_setup() -> None:
    strategy = load_strategy(STRATEGY_DIR / "oversold_reversal.yaml")
    filters = strategy.screening.hard_filters
    assert filters.change_60d_max is not None and filters.change_60d_max < 0
    assert filters.rsi_status_whitelist == ["oversold"]
    assert filters.daily_quality_score_min is not None
    assert requires_daily_features(filters) is True


def test_dragon_board_only_claims_executable_theme_momentum_fields() -> None:
    strategy = load_strategy(STRATEGY_DIR / "dragon_board.yaml")
    filters = strategy.screening.hard_filters
    assert "experimental" in strategy.tags
    assert filters.change_60d_max is not None
    assert filters.breakout_20d_pct_min is not None
    assert filters.volume_ratio_20d_min is not None
    assert "dragon_head" not in strategy.analysis_skills
    forbidden = ("首板", "二板", "封板时间", "炸板", "梯队")
    assert not any(word in strategy.screening.ranking_hints for word in forbidden)


def test_value_strategy_names_match_available_factors() -> None:
    strategies = load_all_strategies(STRATEGY_DIR)
    assert strategies["quality_value"].display_name == "低估值稳健"
    assert strategies["blue_chip_income"].display_name == "大盘低估值防守"
```

Update the expected inventory in `test_builtin_screening_engine.py` to include `dragon_board`.

- [x] **Step 2: Run strategy tests and verify failure**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py::test_bundled_strategies_are_loaded_from_the_internal_package -v
```

Expected: current names, filters, inventory, duplicate key, and `tech_weight` declarations violate the new contracts.

- [x] **Step 3: Apply the minimum YAML corrections**

For `dragon_board`:

- version `1.1`, display name `强势题材（实验）`, add tag `experimental`;
- analysis skills `[hot_theme, bull_trend]`;
- one `range_20d_pct_max` only;
- `change_60d_max: 35.0`, `breakout_20d_pct_min: -1.0`, `volume_ratio_20d_min: 1.3`, `daily_quality_score_min: 80.0`;
- remove deterministic claims about board count, seal time, limit-break, ladder, and leader rank;
- explain that the 9.95% ceiling is a conservative main-board momentum window, not universal limit-up detection.

For `oversold_reversal`:

- version `1.2`;
- `change_60d_min: -35.0`, `change_60d_max: -8.0`;
- `rsi_status_whitelist: [oversold]`;
- `volatility_20d_pct_max: 55.0`, `max_drawdown_20d_pct_min: -30.0`, `atr_20_pct_max: 8.0`;
- `daily_quality_score_min: 80.0`;
- describe the result as a reversal watchlist requiring subsequent stabilization, not a buy signal.

Rename/reword the two value strategies so the deterministic contract mentions only valuation, liquidity, size, and trading stability. Remove unsupported dividend/ROE/cash-flow claims from ranking hints.

Remove `tech_weight` from all bundled screening YAML files. Do not remove the dataclass field or loader key.

- [x] **Step 4: Run all strategy contract tests**

Run:

```bash
python -m pytest tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py::test_bundled_strategies_are_loaded_from_the_internal_package -v
```

Expected: all pass and all 11 strategies load.

---

### Task 4: Document the corrected boundary and verify the change

**Files:**
- Modify: `docs/screening-engine.md`
- Modify: `docs/CHANGELOG.md`
- Verify: all Python/YAML files changed in Tasks 1-3

**Interfaces:**
- Produces: user-facing documentation consistent with runtime behavior.

- [x] **Step 1: Update the screening strategy boundary documentation**

Add a concise section to `docs/screening-engine.md` stating:

- built-in factor weights are authoritative; `tech_weight` remains legacy fallback only when custom YAML omits factor weights;
- current structured value score uses PE/PB and must not be interpreted as ROE/cash-flow/dividend quality;
- experimental theme momentum does not have seal-time, limit-break, or board-ladder data;
- oversold reversal requires daily RSI, negative 60-day return, bounded volatility/drawdown, and daily data quality;
- screening run history is persistence, not yet a strategy T+N performance report.

- [x] **Step 2: Add flat changelog entries**

Under `[Unreleased]`, add independent lines:

```markdown
- [修复] 选股策略加载拒绝重复 YAML 键、非法阈值区间和非法因子权重，避免配置被静默覆盖。
- [改进] 收敛强势题材、超跌反转和低估值策略的可执行条件与展示语义，使策略描述与真实筛选字段一致。
- [测试] 补充内置选股策略配置、日 K 数据质量和兼容旧式自定义策略的回归测试。
```

- [x] **Step 3: Run focused verification**

Run:

```bash
python -m py_compile src/services/screening/models.py src/services/screening/filter.py src/services/screening/strategy.py
python -m pytest tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py -v
```

Expected: syntax check succeeds and focused tests pass.

- [x] **Step 4: Run repository-level deterministic checks**

Run:

```bash
./scripts/ci_gate.sh syntax
./scripts/ci_gate.sh flake8
```

Expected: both commands exit 0. If a failure is pre-existing or outside touched files, preserve the exact output and report it without unrelated edits.

- [x] **Step 5: Review the final diff scope**

Run:

```bash
git diff -- src/services/screening/models.py src/services/screening/filter.py src/services/screening/strategy.py src/services/screening/strategies tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py docs/screening-engine.md docs/CHANGELOG.md docs/superpowers
git status --short
```

Expected: no portfolio, Web history, Agent strategy, or Git-history mutation appears in the task diff.
