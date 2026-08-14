# Remote Systemd Deployment Script Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe local script that builds and validates the checked-out workspace, deploys only runtime files to the existing remote systemd installation, and automatically rolls back a failed release without using Git.

**Architecture:** `scripts/deploy_remote.sh` builds the Web assets locally, creates an explicit runtime allowlist archive, uploads it to a remote staging directory, validates it, then briefly stops the systemd service to back up and install the release. Remote `.env`, secrets, virtualenv, database, reports, and logs remain outside the archive; a failed service or health check restores the code backup. Deployment settings come from environment variables, while SSH authentication remains delegated to `ssh`.

**Tech Stack:** Bash 3.2+, OpenSSH, tar, curl, systemd, Python/pytest, npm/Vite

## Global Constraints

- Do not run `git commit`, `git tag`, `git push`, branch switching, stash, reset, or checkout.
- Do not store the SSH password or application secrets in repository files or command-line arguments.
- Preserve remote `.env`, `venv/`, `data/`, `logs/`, `reports/`, secret files, and deployment backups.
- Build Web assets locally; the remote server does not need Node.js.
- Package only explicit runtime paths and reject macOS AppleDouble `._*` files.
- Validate all destructive remote paths before removing staging data or replacing runtime paths.
- Keep rollback archives under the configurable remote project directory.

---

### Task 1: Executable deployment contract

**Files:**
- Create: `tests/test_remote_deploy_script.py`
- Create: `scripts/deploy_remote.sh`

**Interfaces:**
- Consumes: `DSA_DEPLOY_TARGET`, `DSA_DEPLOY_ROOT`, `DSA_DEPLOY_SERVICE`, `DSA_DEPLOY_HEALTH_URL`, and standard `ssh`/`scp` authentication.
- Produces: `scripts/deploy_remote.sh [--dry-run] [--skip-tests] [--rollback DEPLOYMENT_ID]` with non-zero exit codes on invalid input or failed verification.

- [ ] **Step 1: Write failing subprocess tests**

  Add tests that execute the real Bash script and assert that `--help` documents configuration, missing target fails before any network call, dry-run prints a runtime manifest that omits persistent/sensitive paths, and malformed rollback IDs are rejected.

- [ ] **Step 2: Verify the tests fail because the script is missing**

  Run: `venv/bin/python -m pytest tests/test_remote_deploy_script.py -q`

- [ ] **Step 3: Implement argument parsing and local preflight**

  Implement strict Bash mode, repository-root discovery, configurable settings, required-command checks, deployment-ID validation, explicit runtime path arrays, and a side-effect-free dry run.

- [ ] **Step 4: Verify the contract tests pass**

  Run: `venv/bin/python -m pytest tests/test_remote_deploy_script.py -q`

### Task 2: Staged deploy and automatic rollback

**Files:**
- Modify: `tests/test_remote_deploy_script.py`
- Modify: `scripts/deploy_remote.sh`

**Interfaces:**
- Consumes: the Task 1 CLI and a controlled fake `ssh`/`scp` command boundary in tests.
- Produces: local validation/build, archive upload, remote staging validation, runtime backup/install, systemd restart, health check, and automatic code rollback.

- [ ] **Step 1: Add a failing end-to-end command-boundary test**

  Execute the script with temporary fake `ssh`, `scp`, `npm`, and `curl` executables; assert the observable command log includes upload, backup, service stop/start, health verification, and excludes `.env`, databases, virtualenv, logs, and reports.

- [ ] **Step 2: Verify the new test fails at the missing deploy behavior**

  Run: `venv/bin/python -m pytest tests/test_remote_deploy_script.py -q`

- [ ] **Step 3: Implement the minimal deployment workflow**

  Build/lint Web locally unless skipped, run relevant Python checks unless skipped, create and inspect a portable archive, upload to a unique remote staging path, validate Python/static/strategy content, back up existing runtime files, install the release, start `systemd`, poll health, and restore the backup when validation fails.

- [ ] **Step 4: Implement explicit manual rollback**

  Validate the deployment ID, confirm its backup archive exists under the configured backup directory, stop the service, restore the archive, start the service, and run the same health check.

- [ ] **Step 5: Verify all deployment tests pass**

  Run: `venv/bin/python -m pytest tests/test_remote_deploy_script.py -q`

### Task 3: Operator documentation and final verification

**Files:**
- Modify: `docs/DEPLOY.md`
- Modify: `docs/DEPLOY_EN.md`
- Modify: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: the final CLI and environment-variable names from Tasks 1-2.
- Produces: copy-paste setup, dry-run, deploy, rollback, SSH-key, backup, and failure-recovery guidance in both deployment guides.

- [ ] **Step 1: Document the direct systemd deployment workflow**

  Add Chinese and English instructions that export non-secret target settings, require SSH key or interactive authentication, show dry-run/deploy/rollback commands, and explain preserved paths and backup locations.

- [ ] **Step 2: Add flat `[Unreleased]` changelog entries**

  Add one `新功能` entry for the deployment script and one `测试` entry for its subprocess contract coverage without adding a subsection.

- [ ] **Step 3: Run focused and repository-adjacent verification**

  Run:

  ```bash
  venv/bin/python -m pytest tests/test_remote_deploy_script.py -q
  bash -n scripts/deploy_remote.sh
  scripts/deploy_remote.sh --help
  DSA_DEPLOY_TARGET=example.invalid scripts/deploy_remote.sh --dry-run --skip-tests
  ```

- [ ] **Step 4: Inspect the final diff without changing Git state**

  Run: `git diff --check -- scripts/deploy_remote.sh tests/test_remote_deploy_script.py docs/DEPLOY.md docs/DEPLOY_EN.md docs/CHANGELOG.md docs/superpowers/plans/2026-08-11-remote-systemd-deploy.md`

  Confirm no secret, password, API key, deployment archive, or generated static bundle was added.
