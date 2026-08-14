# Intraday Market Scheduling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add wall-clock-aligned A-share monitoring, dynamic screening at 10:00/14:00, and afternoon watch notifications beginning at 13:30.

**Architecture:** A new in-process session coordinator polls every 30 seconds and claims deterministic Beijing-time slots. It composes a lightweight market-trend monitor, the existing intraday stock watcher, and a dynamic screening worker, so both standalone and runtime-scheduler entry points share the same timing semantics.

**Tech Stack:** Python 3.11, existing `schedule` runtime, `DataFetcherManager`, `ScreeningService`, `NotificationService`, pytest/unittest.

## Global Constraints

- Do not run `git add`, `git commit`, `git push`, or alter the current branch.
- Reuse the existing `alert` notification route and screening service.
- New capabilities are default-off and configured through `.env`.
- Use `Asia/Shanghai` wall-clock slots and skip lunch, off-hours, weekends, and holidays through the existing market-phase guard.
- Keep ordinary analysis, Web screening, and existing report behavior unchanged when new switches are disabled.

---

### Task 1: Runtime configuration contract

**Files:**
- Modify: `src/config.py`
- Modify: `.env.example`
- Test: `tests/test_intraday_session_scheduler.py`

**Interfaces:**
- Produces config fields `intraday_market_monitor_enabled: bool`, `intraday_market_monitor_interval_minutes: int`, `intraday_market_trend_threshold_pct: float`, `intraday_screening_enabled: bool`, `intraday_screening_times: list[str]`, and `intraday_screening_max_results: int`.

- [ ] Write a failing test constructing `Config.from_env()` with literal environment values and asserting the normalized fields.
- [ ] Run `venv/bin/python -m pytest -q tests/test_intraday_session_scheduler.py -k config` and verify failure because fields are absent.
- [ ] Add dataclass fields and parse the six environment variables with safe defaults and bounds.
- [ ] Run the focused test and verify it passes.

### Task 2: Persistent in-process market trend monitor

**Files:**
- Create: `src/services/intraday_market_monitor.py`
- Test: `tests/test_intraday_market_monitor.py`

**Interfaces:**
- Produces `MarketTrendState` values `neutral`, `sustained_up`, and `sustained_down`.
- Produces `IntradayMarketMonitor.run_once(now: datetime) -> MarketTrendSnapshot` and `current_state`.
- Consumes `DataFetcherManager.get_main_indices(region="cn")` and `NotificationService.send_with_results(..., route_type="alert")`.

- [ ] Write failing tests using complete index rows for three samples that prove sustained-up, sustained-down, below-threshold neutral, insufficient-data neutral, lunch-session reset, transition-only notification, and recovery notification.
- [ ] Run `venv/bin/python -m pytest -q tests/test_intraday_market_monitor.py` and verify the missing module failure.
- [ ] Implement sample normalization, three-point direction/median threshold classification, session reset, state transitions, and compact WeCom text formatting.
- [ ] Run the focused test file and verify it passes.

### Task 3: Dynamic screening worker

**Files:**
- Create: `src/services/intraday_screening_worker.py`
- Test: `tests/test_intraday_screening_worker.py`

**Interfaces:**
- Produces `select_strategy(state: MarketTrendState) -> str` with mappings `sustained_up -> dragon_board`, `sustained_down -> low_volatility_quality`, and `neutral -> shrink_pullback`.
- Produces `IntradayScreeningWorker.run_once(state, now) -> dict[str, int | str]`.
- Consumes `ScreeningService.screen(strategy=..., market="cn", max_results=5)` and the existing alert notification route.

- [ ] Write failing tests for all three mappings, five-candidate formatting, zero-candidate notification, and exception isolation.
- [ ] Run `venv/bin/python -m pytest -q tests/test_intraday_screening_worker.py` and verify the missing module failure.
- [ ] Implement the strategy mapping, screening invocation, result/degradation formatting, and success/empty/failure notification paths.
- [ ] Run the focused test file and verify it passes.

### Task 4: Wall-clock session coordinator

**Files:**
- Create: `src/services/intraday_session_scheduler.py`
- Modify: `src/services/intraday_watch_worker.py`
- Modify: `src/services/runtime_scheduler.py`
- Modify: `main.py` only if the existing standalone entry cannot consume the coordinator without changing its CLI contract.
- Test: `tests/test_intraday_session_scheduler.py`
- Test: `tests/test_intraday_watch_worker.py`
- Test: `tests/test_main_schedule_mode.py`

**Interfaces:**
- Produces `IntradaySessionCoordinator.tick(now: datetime | None = None) -> dict[str, object]`.
- Produces deterministic due-slot helpers for 10-minute market samples, configured screening times, and 30-minute watch slots restricted to `09:30..11:30` and `13:30..15:00`.
- Consumes the monitor, screening worker, and existing `IntradayWatchWorker.run_once()`.

- [ ] Write failing tests proving exact watch slots, no 13:00 execution, first afternoon run at 13:30, restart at 10:07 waiting until 10:30, one execution per slot, 10:00/14:00 screening, and ordering market -> screening -> watch.
- [ ] Run the focused scheduler/watch tests and verify failures against interval-based behavior.
- [ ] Implement the coordinator with per-day slot claims and market-session guards.
- [ ] Change standalone `run_intraday_watch_loop` to poll the coordinator every 30 seconds instead of sleeping for the configured interval.
- [ ] Register the same coordinator in `RuntimeSchedulerService` at a 30-second polling interval whenever any intraday capability is enabled.
- [ ] Run the focused scheduler/watch/main tests and verify they pass.

### Task 5: User-facing documentation and changelog

**Files:**
- Modify: `docs/notifications.md`
- Modify: `docs/DEPLOY.md`
- Modify: `docs/DEPLOY_EN.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Documents exact time slots, strategy mapping, threshold, new environment variables, transition-only notifications, and rollback switches.

- [ ] Update the Chinese notification/deployment documentation with the exact runtime contract.
- [ ] Synchronize the English deployment document because deployment behavior and variables change.
- [ ] Add flat `[Unreleased]` entries without new category headings.
- [ ] Cross-check every documented variable and command against the implementation.

### Task 6: Verification and remote deployment

**Files:**
- No Git operations.
- Deploy only files changed by Tasks 1-5 plus their required new modules.

**Interfaces:**
- Uses existing remote path `/opt/daily_stock_analysis` and systemd unit `dsa-intraday-watch`.

- [ ] Run `venv/bin/python -m py_compile` for every changed Python module.
- [ ] Run all new tests plus `tests/test_intraday_watch_worker.py`, `tests/test_main_schedule_mode.py`, and relevant runtime scheduler tests.
- [ ] Run `./scripts/ci_gate.sh` if the focused suite is green; report unrelated pre-existing failures separately.
- [ ] Back up remote changed files and `.env`, transfer only the verified scope, and add the six enabled runtime values without exposing credentials.
- [ ] Restart `dsa-intraday-watch`, inspect `systemctl status` and recent logs, and confirm the next wall-clock tasks without sending an out-of-session test alert.
- [ ] Provide exact rollback commands and note any verification gap.
