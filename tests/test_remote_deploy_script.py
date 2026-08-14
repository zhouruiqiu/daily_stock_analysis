from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "deploy_remote.sh"


def run_deploy(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    command_env.pop("DSA_DEPLOY_TARGET", None)
    if env:
        command_env.update(env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=command_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_help_documents_configuration_without_requiring_target() -> None:
    result = run_deploy("--help")

    assert result.returncode == 0
    assert "DSA_DEPLOY_TARGET" in result.stdout
    assert "--dry-run" in result.stdout
    assert "--rollback" in result.stdout


def test_deploy_requires_target_before_network_activity() -> None:
    result = run_deploy("--skip-tests")

    assert result.returncode == 2
    assert "DSA_DEPLOY_TARGET" in result.stderr


def test_dry_run_prints_runtime_manifest_without_persistent_paths() -> None:
    result = run_deploy(
        "--dry-run",
        "--skip-tests",
        env={"DSA_DEPLOY_TARGET": "deploy@example.invalid"},
    )

    assert result.returncode == 0, result.stderr
    assert "main.py" in result.stdout
    assert "api/" in result.stdout
    assert "static/" in result.stdout
    assert ".env" not in result.stdout
    assert "venv/" not in result.stdout
    assert "data/" not in result.stdout
    assert "logs/" not in result.stdout
    assert "reports/" not in result.stdout


def test_rollback_rejects_path_traversal_deployment_id() -> None:
    result = run_deploy(
        "--rollback",
        "../latest",
        env={"DSA_DEPLOY_TARGET": "deploy@example.invalid"},
    )

    assert result.returncode == 2
    assert "deployment ID" in result.stderr


def write_fake_command(directory: Path, name: str, body: str) -> None:
    command = directory / name
    command.write_text(f"#!/bin/sh\nset -eu\n{body}", encoding="utf-8")
    command.chmod(0o755)


def test_deploy_uploads_allowlisted_release_and_runs_remote_safety_flow(
    tmp_path: Path,
) -> None:
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    command_log = tmp_path / "commands.log"
    archive_log = tmp_path / "archive.log"

    write_fake_command(
        fakebin,
        "ssh",
        'printf "ssh %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'stdin_file="${TMPDIR:-/tmp}/fake-ssh-stdin.$$"\n'
        'cat > "$stdin_file"\n'
        'cat "$stdin_file" >> "$COMMAND_LOG"\n'
        'rm -f "$stdin_file"\n',
    )
    write_fake_command(
        fakebin,
        "scp",
        'printf "scp %s\\n" "$*" >> "$COMMAND_LOG"\n'
        'archive=""\n'
        'for argument in "$@"; do\n'
        '  if [ -f "$argument" ]; then archive="$argument"; fi\n'
        'done\n'
        '[ -n "$archive" ]\n'
        '/usr/bin/tar -tzf "$archive" > "$ARCHIVE_LOG"\n',
    )

    env = {
        "PATH": f"{fakebin}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "ARCHIVE_LOG": str(archive_log),
        "DSA_DEPLOY_TARGET": "deploy@example.invalid",
        "DSA_DEPLOY_ROOT": "/srv/dsa",
        "DSA_DEPLOY_SERVICE": "dsa-test",
        "DSA_DEPLOY_HEALTH_URL": "http://127.0.0.1:18000/api/health?probe=a&full=1",
    }
    result = run_deploy("--skip-tests", env=env)

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text(encoding="utf-8")
    assert "systemctl stop" in commands
    assert "systemctl start" in commands
    assert "runtime-before-" in commands
    assert "curl" in commands
    assert "automatic rollback" in commands
    assert "probe=a\\&full=1" in commands

    archive_entries = archive_log.read_text(encoding="utf-8").splitlines()
    assert "main.py" in archive_entries
    assert "static/" in archive_entries
    assert not any(entry == ".env" or entry.startswith(".env/") for entry in archive_entries)
    assert not any(entry.startswith("venv/") for entry in archive_entries)
    assert not any(entry.startswith("data/") for entry in archive_entries)
    assert not any(entry.startswith("logs/") for entry in archive_entries)
    assert not any(entry.startswith("reports/") for entry in archive_entries)
    assert not any("/._" in entry or entry.startswith("._") for entry in archive_entries)
