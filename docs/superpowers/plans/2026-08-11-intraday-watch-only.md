# Intraday Watch Only Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an independent, lightweight intraday watch process that reloads the Web watchlist and pushes a technical snapshot every 30 minutes only during configured market trading phases.

**Architecture:** Add a dedicated `--intraday-watch-only` CLI branch that starts only `IntradayWatchWorker` in a stoppable interval loop. Keep the existing Web and daily scheduler processes unchanged, then deploy the new mode as a separate systemd service.

**Tech Stack:** Python, unittest/pytest, systemd, existing notification routing and market calendar services.

---

### Task 1: Specify the standalone loop with tests

**Files:**
- Modify: `tests/test_intraday_watch_worker.py`
- Test: `tests/test_intraday_watch_worker.py`

- [ ] Add tests proving the loop exits with a non-zero code when disabled.
- [ ] Add a test proving it runs immediately, waits for the configured interval, and stops cleanly.
- [ ] Run the focused tests and confirm the new tests fail for the missing behavior.

### Task 2: Implement the standalone CLI mode

**Files:**
- Modify: `src/services/intraday_watch_worker.py`
- Modify: `main.py`
- Test: `tests/test_intraday_watch_worker.py`

- [ ] Implement the minimal stoppable loop using `threading.Event.wait`.
- [ ] Add `--intraday-watch-only` and route it before Web, schedule, and full-analysis startup.
- [ ] Handle `KeyboardInterrupt` as a clean service shutdown.
- [ ] Run focused worker and CLI regression tests.

### Task 3: Document the operational contract

**Files:**
- Modify: `.env.example`
- Modify: `docs/notifications.md`
- Modify: `docs/DEPLOY.md`
- Modify: `docs/DEPLOY_EN.md`
- Modify: `docs/CHANGELOG.md`

- [ ] Document the dedicated CLI mode and independent systemd service.
- [ ] Clarify that `STOCK_LIST` is reloaded for each watch cycle and that off-market cycles do not push.
- [ ] Record the user-visible behavior under `[Unreleased]` using the required flat format.

### Task 4: Verify locally

**Files:**
- Test: `tests/test_intraday_watch_worker.py`
- Test: affected main/scheduler tests

- [ ] Run focused pytest coverage.
- [ ] Run Python compilation checks for changed Python files.
- [ ] Inspect the final diff without staging or committing anything.

### Task 5: Deploy and enable remotely

**Files:**
- Use: `scripts/deploy_remote.sh`
- Create remotely: `/etc/systemd/system/dsa-intraday-watch.service`
- Modify remotely: `/opt/daily_stock_analysis/.env`

- [ ] Inspect available notification channels without exposing credentials.
- [ ] Deploy the verified local working tree using the existing backup/rollback script.
- [ ] Back up the remote `.env`, enable 30-minute CN intraday watching, and preserve the seven-stock `STOCK_LIST`.
- [ ] Install and enable the independent systemd service.
- [ ] Verify both `dsa` and `dsa-intraday-watch`, check logs, and confirm the Web health endpoint remains available.

