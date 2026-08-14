# Intraday Position Score Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add deterministic, explainable position-aware scores and hold recommendations to intraday watch notifications.

**Architecture:** A focused `IntradayPositionScorer` converts existing trend, quote, and optional position data into a structured result. The watch worker loads current portfolio positions once per cycle, matches normalized symbols, scores each item, and formats the result without invoking an LLM.

**Tech Stack:** Python dataclasses, existing `StockTrendAnalyzer`, existing `PortfolioService`, pytest/unittest, systemd.

## Global Constraints

- Do not invoke an LLM or fetch news.
- Held positions use 70 technical points plus 30 cost/PnL points.
- Non-held watchlist items use a normalized technical score and observation wording only.
- Missing portfolio data degrades to technical observation without failing the cycle.
- Do not commit, push, switch branches, or alter unrelated dirty-worktree changes.

---

### Task 1: Deterministic scorer

**Files:**
- Create: `src/services/intraday_position_scorer.py`
- Create: `tests/test_intraday_position_scorer.py`

**Interfaces:**
- Consumes: `TrendAnalysisResult`, quote-like objects, optional position dictionaries containing `avg_cost`, `quantity`, and `unrealized_pnl_pct`.
- Produces: `IntradayPositionScore` and `IntradayPositionScorer.score(trend, quote, position=None)`.

- [ ] Write failing tests for strong held, weak held, non-held observation, score bounds, and normalized A-share symbols.
- [ ] Run the scorer tests and confirm import/behavior failures.
- [ ] Implement the dataclass, technical/cost scoring, recommendation bands, risk bands, cost defense price, and explanatory reasons.
- [ ] Run the scorer tests and confirm they pass.

### Task 2: Worker portfolio integration and message format

**Files:**
- Modify: `src/services/intraday_watch_worker.py`
- Modify: `tests/test_intraday_watch_worker.py`

**Interfaces:**
- Consumes: `IntradayPositionScorer`, existing watch items, and current portfolio snapshot positions.
- Produces: watch notification blocks containing score, identity, recommendation, risk, cost/PnL when held, and reasons.

- [ ] Write failing tests proving held positions receive cost-aware output and portfolio failures degrade to observation output.
- [ ] Run the worker tests and confirm the new assertions fail.
- [ ] Load all active account positions once per cycle through the existing Portfolio Service, normalize symbols, and attach scores to items.
- [ ] Extend formatting while preserving the existing price/indicator/support lines.
- [ ] Run worker and scorer tests and confirm they pass.

### Task 3: Documentation and changelog

**Files:**
- Modify: `docs/notifications.md`
- Modify: `docs/CHANGELOG.md`

- [ ] Document the 70/30 held-position contract, observation fallback, output fields, and non-advisory boundary.
- [ ] Add a flat `[Unreleased]` entry.

### Task 4: Verification and deployment

**Files:**
- Verify: changed Python and tests
- Deploy: existing `scripts/deploy_remote.sh`

- [ ] Run focused pytest, Python compilation, flake8 critical checks, and `git diff --check`.
- [ ] Deploy without Git using an extended health-check window.
- [ ] Restart and verify `dsa` and `dsa-intraday-watch`.
- [ ] Confirm Web health, a seven-stock scoring cycle, and successful enterprise WeChat notification delivery.

