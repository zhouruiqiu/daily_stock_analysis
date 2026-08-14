# 强势题材策略优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `dragon_board` 收敛为只接受实时快照和真实题材证据、同时具备趋势确认与追高约束的强势题材观察策略。

**Architecture:** 在现有策略模型中增加两个通用但默认关闭的数据要求：`snapshot_mode=realtime` 和 `require_theme_evidence=true`。快照适配层提供实时性元数据，pipeline 在进入候选排序前执行策略数据门控；YAML 再负责该策略的阈值、权重和风险参数，其他内置策略保持原行为。

**Tech Stack:** Python 3.11、dataclasses、pandas、PyYAML、pytest、现有 Screening pipeline。

## Global Constraints

- 只优化 `dragon_board` 及其直接依赖的筛选契约。
- 不修改定时任务、通知推送、远程部署或 Web UI。
- 不执行 `git add`、`git commit`、`git push`。
- LLM 只能重排已经通过确定性门控的候选，不能覆盖硬过滤。
- 数据不足时允许返回少于五只或空结果，不使用默认中性分凑数。

---

### Task 1: 为策略增加快照实时性契约

**Files:**
- Modify: `src/services/screening/models.py`
- Modify: `src/services/screening/strategy.py`
- Modify: `src/services/screening/snapshot.py`
- Modify: `src/services/screening/pipeline.py`
- Test: `tests/test_builtin_screening_engine.py`
- Test: `tests/test_screening_strategy_contracts.py`

**Interfaces:**
- Produces: `ScreeningConfig.snapshot_requirements: dict[str, object]`
- Produces: snapshot attribute `snapshot_mode`, value `realtime` or `eod`
- Consumes: YAML `screening.snapshot_requirements.mode`

- [ ] **Step 1: 写失败测试，证明实时策略拒绝 EOD 快照**

```python
def test_dragon_board_rejects_eod_snapshot(monkeypatch):
    frame = valid_dragon_board_snapshot()
    frame.attrs["snapshot_source"] = "tushare"
    frame.attrs["snapshot_mode"] = "eod"
    monkeypatch.setattr(screening_pipeline, "fetch_snapshot_with_fallback", lambda *a, **k: frame)

    with pytest.raises(RuntimeError, match="requires realtime snapshot"):
        screening_pipeline.screen("dragon_board", use_llm=False)
```

能够让该测试失败的生产变更：pipeline 忽略 `snapshot_mode`，继续把 Tushare 上一交易日数据作为盘中强势快照。

- [ ] **Step 2: 运行测试并确认按预期失败**

Run: `venv/bin/python -m pytest -q tests/test_builtin_screening_engine.py -k dragon_board_rejects_eod_snapshot`

Expected: FAIL，因为模型、YAML loader 或 pipeline 尚未支持该契约。

- [ ] **Step 3: 实现最小数据要求模型与门控**

```python
@dataclass
class ScreeningConfig:
    # existing fields...
    snapshot_requirements: dict[str, object] = field(default_factory=dict)


def _validate_snapshot_requirements(frame: pd.DataFrame, requirements: dict[str, object]) -> None:
    required_mode = str(requirements.get("mode") or "").strip().lower()
    actual_mode = str(frame.attrs.get("snapshot_mode") or "unknown").strip().lower()
    if required_mode == "realtime" and actual_mode != "realtime":
        raise RuntimeError(
            f"Strategy requires realtime snapshot, got {actual_mode} "
            f"from {frame.attrs.get('snapshot_source', 'unknown')}"
        )
```

在 snapshot fetcher 中将 `sina`、`efinance`、`akshare_em`、`em_datacenter` 标记为 `realtime`，将 `tushare` 标记为 `eod`；last-good cache 必须持久化并恢复原始 `snapshot_mode`，不能仅凭缓存文件创建时间推断。

- [ ] **Step 4: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest -q tests/test_builtin_screening_engine.py tests/test_screening_strategy_contracts.py -k 'snapshot_mode or dragon_board'`

Expected: PASS。

### Task 2: 增加真实题材证据门控

**Files:**
- Modify: `src/services/screening/models.py`
- Modify: `src/services/screening/filter.py`
- Modify: `src/services/screening/strategy.py`
- Test: `tests/test_screening_strategy_contracts.py`

**Interfaces:**
- Produces: `HardFilterConfig.require_theme_evidence: bool`
- Produces: `_has_theme_evidence(df: pd.DataFrame) -> pd.Series`
- Consumes: `board_heat_score`、`industry_heat_score`、`concept_heat_score`、`board_heat_summary`

- [ ] **Step 1: 写失败测试，证明只有价格强度但没有题材证据的股票会被过滤**

```python
def test_theme_evidence_filter_rejects_price_only_candidate():
    filters = HardFilterConfig(require_theme_evidence=True)
    frame = pd.DataFrame([
        {"code": "price-only", "name": "Price Only", "change_pct": 7.2},
        {
            "code": "theme-backed",
            "name": "Theme Backed",
            "change_pct": 6.8,
            "board_heat_score": 78.0,
            "board_heat_summary": "机器人 热度78",
        },
    ])

    assert apply_hard_filters(frame, filters)["code"].tolist() == ["theme-backed"]
```

能够让该测试失败的生产变更：过滤器仍把缺失题材字段视为中性证据。

- [ ] **Step 2: 运行测试并确认按预期失败**

Run: `venv/bin/python -m pytest -q tests/test_screening_strategy_contracts.py -k theme_evidence_filter`

Expected: FAIL，因为 `HardFilterConfig` 尚无该字段。

- [ ] **Step 3: 实现题材证据过滤**

```python
def _has_theme_evidence(df: pd.DataFrame) -> pd.Series:
    evidence = pd.Series(False, index=df.index)
    for column in ("board_heat_score", "industry_heat_score", "concept_heat_score"):
        if column in df.columns:
            evidence |= pd.to_numeric(df[column], errors="coerce").notna()
    if "board_heat_summary" in df.columns:
        summary = df["board_heat_summary"].fillna("").astype(str).str.strip()
        evidence |= summary.ne("")
    return evidence
```

`apply_hard_filters()`、rejection summary、waterfall、active filters 和策略 metadata 必须共同识别该门控；默认值为 `False`，不改变其他策略。

- [ ] **Step 4: 运行针对性测试并确认通过**

Run: `venv/bin/python -m pytest -q tests/test_screening_strategy_contracts.py -k 'theme_evidence or dragon_board'`

Expected: PASS。

### Task 3: 收敛 dragon_board 的阈值、权重和文案

**Files:**
- Modify: `src/services/screening/strategies/dragon_board.yaml`
- Modify: `src/services/screening/strategy.py`
- Test: `tests/test_screening_strategy_contracts.py`
- Modify: `docs/screening-engine.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: Task 1 的 `snapshot_requirements.mode`
- Consumes: Task 2 的 `require_theme_evidence`
- Produces: `dragon_board` version `1.2`

- [ ] **Step 1: 写失败契约测试**

```python
def test_dragon_board_requires_realtime_theme_confirmation():
    strategy = load_strategy(STRATEGY_DIR / "dragon_board.yaml")
    filters = strategy.screening.hard_filters

    assert strategy.version == "1.2"
    assert strategy.screening.snapshot_requirements == {"mode": "realtime"}
    assert filters.require_theme_evidence is True
    assert filters.require_ma_bullish is True
    assert filters.change_60d_min == 5.0
    assert filters.change_60d_max == 30.0
    assert filters.max_drawdown_20d_pct_min == -12.0
    assert filters.atr_20_pct_max == 7.0
    assert filters.body_pct_min is None
    assert strategy.screening.scoring_profile["theme_heat_unknown_score"] == 15.0
    assert strategy.screening.scoring_profile["topic_alignment_unknown_score"] == 10.0
```

能够让该测试失败的生产变更：策略重新允许 EOD、无题材证据、无多周期趋势确认或把未知题材恢复到中性 50 分。

- [ ] **Step 2: 运行测试并确认按预期失败**

Run: `venv/bin/python -m pytest -q tests/test_screening_strategy_contracts.py -k dragon_board_requires_realtime_theme_confirmation`

Expected: FAIL，显示当前版本和契约仍为 1.1。

- [ ] **Step 3: 更新 YAML 与允许的 scoring profile keys**

将权重调整为：

```yaml
factor_weights:
  momentum: 0.25
  activity: 0.20
  theme_heat: 0.25
  topic_alignment: 0.20
  liquidity: 0.10
```

增加实时与题材门控；加入 `require_ma_bullish`、`change_60d_min=5`、`change_60d_max=30`、`max_drawdown_20d_pct_min=-12`、`atr_20_pct_max=7`；删除会被误读为当前盘中实体的 `body_pct_min`。在 scoring profile 中将未知题材分降到 15、未知匹配分降到 10，并在 YAML loader 的白名单中允许现有 scorer 支持的 `topic_alignment_*` 参数。

- [ ] **Step 4: 运行策略契约与过滤测试**

Run: `venv/bin/python -m pytest -q tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py`

Expected: PASS。

- [ ] **Step 5: 同步专题文档和 Changelog**

说明该策略现在要求实时快照和真实题材热度证据；数据不足时可以返回空结果，不等同于涨停、封板或连板识别。`docs/CHANGELOG.md` 的 `[Unreleased]` 继续使用扁平单行格式。

### Task 4: 完整回归验证

**Files:**
- Verify only: all files above

- [ ] **Step 1: 运行目标测试**

Run: `venv/bin/python -m pytest -q tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py tests/test_screening_api.py`

Expected: 0 failures。

- [ ] **Step 2: 运行 Python 静态验证**

Run: `PATH="$PWD/venv/bin:$PATH" ./scripts/ci_gate.sh syntax`

Expected: exit code 0。

- [ ] **Step 3: 检查 diff 质量和范围**

Run: `git diff --check -- src/services/screening/models.py src/services/screening/strategy.py src/services/screening/snapshot.py src/services/screening/pipeline.py src/services/screening/filter.py src/services/screening/strategies/dragon_board.yaml tests/test_screening_strategy_contracts.py tests/test_builtin_screening_engine.py docs/screening-engine.md docs/CHANGELOG.md docs/superpowers/specs/2026-08-13-dragon-board-strategy-design.md docs/superpowers/plans/2026-08-13-dragon-board-strategy.md`

Expected: exit code 0，且不包含定时任务、通知、部署相关改动。
