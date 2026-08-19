# Strategy Evaluation Leaderboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run every enabled A-share screening strategy at 09:25, evaluate Top-N picks at T+1/T+3/T+5, expose a Web leaderboard, and send daily/weekly WeCom summaries.

**Architecture:** Add append-only evaluation run/pick/outcome tables and a deterministic evaluation service that freezes one previous-close snapshot for all strategies. A separate outcome service evaluates same-day 09:30 entry prices and future closes, while scheduler, API, Web, and notification layers consume the persisted contracts.

**Tech Stack:** Python 3.11, SQLAlchemy, FastAPI, pytest, React/TypeScript/Vite, Vitest.

**Spec:** `docs/superpowers/specs/2026-08-19-strategy-evaluation-leaderboard-design.md`

## Global Constraints

- Work in the current user-approved checkout; do not create a worktree because current uncommitted changes are part of the requested deployment chain.
- Do not commit, push, switch branches, stash, reset, or overwrite unrelated files.
- Use TDD for every behavior change.
- Keep the feature disabled by default until remote smoke passes.
- Preserve existing manual and intraday screening contracts.

---

### Task 1: Persistence and configuration contracts

**Files:**
- Modify: `src/storage.py`
- Modify: `src/config.py`
- Modify: `src/config_registry.py`
- Modify: `.env.example`
- Test: `tests/test_strategy_evaluation_storage.py`
- Test: `tests/test_config_registry.py`

**Interfaces:**
- Produces: `StrategyEvaluationRun`, `StrategyEvaluationPick`, `StrategyEvaluationOutcome` SQLAlchemy models and `DatabaseManager` CRUD/query methods.
- Produces: validated `strategy_evaluation_*` config fields.

- [ ] Write failing storage tests for idempotent run creation, pick uniqueness, pending-only outcome updates, list/detail queries, and leaderboard inputs.
- [ ] Run `venv/bin/python -m pytest tests/test_strategy_evaluation_storage.py -q` and confirm failure due to missing models/methods.
- [ ] Add the three append-only tables, indexes, serialization helpers, and minimal CRUD methods.
- [ ] Add configuration parsing for enabled/time/top-N/horizons/benchmark/daily notify/weekly notify; horizons accept only a non-empty subset of `1,3,5`.
- [ ] Run storage/config tests and `venv/bin/python -m py_compile src/storage.py src/config.py`.

### Task 2: Deterministic daily evaluation and outcome engine

**Files:**
- Create: `src/services/strategy_evaluation_service.py`
- Create: `src/services/strategy_outcome_service.py`
- Modify: `src/services/screening/pipeline.py`
- Test: `tests/test_strategy_evaluation_service.py`
- Test: `tests/test_strategy_outcome_service.py`

**Interfaces:**
- Produces: `StrategyEvaluationService.run_daily(selection_date, notify=False) -> dict`.
- Produces: `StrategyOutcomeService.run_due(as_of_date) -> dict` and `get_leaderboard(horizon, window) -> dict`.
- Consumes: one shared previous-close `DataFrame`, enabled strategy definitions, storage CRUD, trading calendar, daily bars.

- [ ] Write failing tests proving all strategies receive the same snapshot object/version, LLM/random/remote analyzers are disabled, Top-N is stable, and one strategy failure produces a partial run.
- [ ] Add an explicit injected/frozen snapshot path to screening evaluation without changing normal `screen()` behavior.
- [ ] Implement daily run persistence and deterministic notification payload construction.
- [ ] Write failing outcome tests for same-day open entry, T+1/T+3/T+5 target dates, benchmark excess return, MAE, pending retry, terminal immutability, and sample-gated ranking.
- [ ] Implement the outcome engine using injected daily-bar/trading-day providers and persisted engine version.
- [ ] Run both service test files and Python compilation.

### Task 3: Scheduler and enterprise-WeChat reports

**Files:**
- Create: `src/services/strategy_evaluation_scheduler.py`
- Modify: `src/services/runtime_scheduler.py`
- Modify: `src/services/intraday_watch_worker.py`
- Test: `tests/test_strategy_evaluation_scheduler.py`
- Test: `tests/test_runtime_scheduler_service.py`

**Interfaces:**
- Produces: wall-clock 09:25 daily run, post-close outcome run, and last-trading-day weekly summary.
- Consumes: `StrategyEvaluationService`, `StrategyOutcomeService`, `NotificationService` alert route.

- [ ] Write failing tests for trading-day gates, exact wall-clock slots, duplicate claims, restart recovery, daily message, weekly message, and notification failure isolation.
- [ ] Implement scheduler coordination with independent locks and idempotent run keys.
- [ ] Register one 30-second background task when `STRATEGY_EVALUATION_ENABLED=true`.
- [ ] Run scheduler/runtime regression tests.

### Task 4: FastAPI contracts

**Files:**
- Create: `api/v1/schemas/strategy_evaluation.py`
- Modify: `api/v1/endpoints/screening.py`
- Test: `tests/test_strategy_evaluation_api.py`

**Interfaces:**
- Produces: `/api/v1/screening/evaluation/status`, `/runs`, `/runs/{run_id}`, `/leaderboard`, `/strategies/{strategy}/history`, and authenticated `/run-now`.

- [ ] Write failing API tests for success, validation, not-found, disabled state, and idempotent run-now.
- [ ] Add Pydantic response/request models and endpoint handlers backed only by the new services.
- [ ] Run API tests plus existing screening API regressions.

### Task 5: Web strategy leaderboard

**Files:**
- Create: `apps/dsa-web/src/api/strategyEvaluation.ts`
- Create: `apps/dsa-web/src/types/strategyEvaluation.ts`
- Create: `apps/dsa-web/src/pages/StrategyLeaderboardPage.tsx`
- Create: `apps/dsa-web/src/pages/__tests__/StrategyLeaderboardPage.test.tsx`
- Modify: `apps/dsa-web/src/App.tsx`
- Modify: relevant navigation component and locale files under `apps/dsa-web/src/`

**Interfaces:**
- Produces: `/strategy-leaderboard` page with status, horizon tabs, leaderboard, today results, and strategy history details.

- [ ] Write failing component tests for loading, empty, insufficient sample, ranked, partial/failed, horizon switching, and API error states.
- [ ] Implement typed API client and responsive page using existing cards/tables/badges/alerts.
- [ ] Add navigation and localized labels without changing unrelated routes.
- [ ] Run targeted Vitest, `npm run lint`, and `npm run build`; capture page screenshot for delivery evidence.

### Task 6: Documentation, full verification, and guarded deployment

**Files:**
- Modify: `docs/screening-engine.md`
- Modify: `docs/notifications.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `.env.example`

**Interfaces:**
- Produces: user/deployment documentation, verified disabled-first release, and rollback evidence.

- [ ] Document evaluation semantics, API, Web page, notification schedule, data-quality states, and rollback.
- [ ] Run `./scripts/ci_gate.sh`, targeted outcome/scheduler/API tests, Web lint/build, and `git diff --check`.
- [ ] Back up remote runtime and database; deploy with `STRATEGY_EVALUATION_ENABLED=false`.
- [ ] Run remote schema/API/shared-snapshot/outcome smoke without notifications.
- [ ] Enable the feature only after smoke succeeds, restart services, verify status/logs, then send one explicit daily test summary.
- [ ] Report deployment IDs, backup paths, verification gaps, and rollback commands without committing or pushing.
