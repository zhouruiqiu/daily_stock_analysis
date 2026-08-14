#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd -P)"

DRY_RUN=false
SKIP_TESTS=false
ROLLBACK_ID=""

RUNTIME_PATHS=(
  main.py
  server.py
  webui.py
  requirements.txt
  api
  bot
  data_provider
  src
  strategies
  static
)

usage() {
  cat <<'EOF'
Usage:
  scripts/deploy_remote.sh [--dry-run] [--skip-tests]
  scripts/deploy_remote.sh --rollback DEPLOYMENT_ID

Options:
  --dry-run       Print the runtime manifest and exit without network activity.
  --skip-tests    Skip local Python checks and Web lint/build.
  --rollback ID   Restore a previous deployment backup.
  -h, --help      Show this help.

Required environment for deployment/rollback:
  DSA_DEPLOY_TARGET       OpenSSH target, for example deploy@example.com
  DSA_DEPLOY_ROOT         Absolute remote project directory
  DSA_DEPLOY_SERVICE      systemd service name
  DSA_DEPLOY_HEALTH_URL   Health URL reachable from the remote host

Optional environment:
  DSA_DEPLOY_SSH_OPTS     Extra ssh/scp options, separated by spaces
  DSA_DEPLOY_HEALTH_TRIES Health attempts (default: 20)

Authentication is handled by OpenSSH. Use an SSH key or enter the password
interactively; never place passwords in this script or its environment.
EOF
}

die_usage() {
  echo "deploy: $*" >&2
  exit 2
}

validate_deployment_id() {
  local deployment_id="$1"
  if [[ ! "${deployment_id}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
    die_usage "invalid deployment ID: ${deployment_id}"
  fi
}

print_manifest() {
  echo "Runtime manifest:"
  local path
  for path in "${RUNTIME_PATHS[@]}"; do
    if [[ -d "${REPO_ROOT}/${path}" ]]; then
      echo "  ${path}/"
    elif [[ -f "${REPO_ROOT}/${path}" ]]; then
      echo "  ${path}"
    else
      die_usage "required runtime path is missing: ${path}"
    fi
  done
}

while (($# > 0)); do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --skip-tests)
      SKIP_TESTS=true
      shift
      ;;
    --rollback)
      (($# >= 2)) || die_usage "--rollback requires a deployment ID"
      ROLLBACK_ID="$2"
      validate_deployment_id "${ROLLBACK_ID}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die_usage "unknown argument: $1"
      ;;
  esac
done

if [[ "${DRY_RUN}" == true ]]; then
  [[ -n "${DSA_DEPLOY_TARGET:-}" ]] || die_usage "DSA_DEPLOY_TARGET is required"
  print_manifest
  echo "Dry run only; no network activity performed."
  exit 0
fi

[[ -n "${DSA_DEPLOY_TARGET:-}" ]] || die_usage "DSA_DEPLOY_TARGET is required"
[[ -n "${DSA_DEPLOY_ROOT:-}" ]] || die_usage "DSA_DEPLOY_ROOT is required"
[[ -n "${DSA_DEPLOY_SERVICE:-}" ]] || die_usage "DSA_DEPLOY_SERVICE is required"
[[ -n "${DSA_DEPLOY_HEALTH_URL:-}" ]] || die_usage "DSA_DEPLOY_HEALTH_URL is required"

if [[ ! "${DSA_DEPLOY_TARGET}" =~ ^[A-Za-z0-9._-]+@[A-Za-z0-9._:-]+$ ]]; then
  die_usage "DSA_DEPLOY_TARGET must use the user@host form"
fi
if [[ ! "${DSA_DEPLOY_ROOT}" =~ ^/[A-Za-z0-9._/-]+$ ]] || [[ "${DSA_DEPLOY_ROOT}" == "/" ]]; then
  die_usage "DSA_DEPLOY_ROOT must be a safe absolute path other than /"
fi
if [[ ! "${DSA_DEPLOY_SERVICE}" =~ ^[A-Za-z0-9@._-]+$ ]]; then
  die_usage "DSA_DEPLOY_SERVICE contains unsupported characters"
fi
if [[ ! "${DSA_DEPLOY_HEALTH_URL}" =~ ^https?://[^[:space:]]+$ ]]; then
  die_usage "DSA_DEPLOY_HEALTH_URL must be an HTTP(S) URL without spaces"
fi

HEALTH_TRIES="${DSA_DEPLOY_HEALTH_TRIES:-20}"
if [[ ! "${HEALTH_TRIES}" =~ ^[1-9][0-9]*$ ]] || ((HEALTH_TRIES > 120)); then
  die_usage "DSA_DEPLOY_HEALTH_TRIES must be between 1 and 120"
fi

SSH_OPTIONS=()
if [[ -n "${DSA_DEPLOY_SSH_OPTS:-}" ]]; then
  # shellcheck disable=SC2206 -- documented as whitespace-separated OpenSSH options.
  SSH_OPTIONS=(${DSA_DEPLOY_SSH_OPTS})
fi

CONTROL_PATH="${TMPDIR:-/tmp}/dsa-ssh-$$"
SSH_OPTIONS+=(
  -o ControlMaster=auto
  -o ControlPersist=60
  -o "ControlPath=${CONTROL_PATH}"
)

cleanup_control_connection() {
  ssh "${SSH_OPTIONS[@]}" -O exit "${DSA_DEPLOY_TARGET}" >/dev/null 2>&1 || true
}
trap cleanup_control_connection EXIT

remote_bash_command() {
  local command="bash -s --"
  local argument quoted
  for argument in "$@"; do
    printf -v quoted '%q' "${argument}"
    command="${command} ${quoted}"
  done
  printf '%s' "${command}"
}

run_remote_rollback() {
  local remote_command
  remote_command="$(remote_bash_command \
    "${DSA_DEPLOY_ROOT}" "${DSA_DEPLOY_SERVICE}" "${DSA_DEPLOY_HEALTH_URL}" \
    "${ROLLBACK_ID}" "${HEALTH_TRIES}")"
  ssh "${SSH_OPTIONS[@]}" "${DSA_DEPLOY_TARGET}" "${remote_command}" <<'REMOTE_ROLLBACK'
set -euo pipefail

root="$1"
service="$2"
health_url="$3"
deployment_id="$4"
health_tries="$5"
backup="${root}/deploy-backups/runtime-before-${deployment_id}.tar.gz"
runtime_paths=(main.py server.py webui.py requirements.txt api bot data_provider src strategies static)

[[ "${root}" == /* && "${root}" != "/" ]] || { echo "rollback: unsafe root" >&2; exit 2; }
[[ -f "${backup}" ]] || { echo "rollback: backup not found: ${backup}" >&2; exit 1; }

systemctl stop "${service}"
for path in "${runtime_paths[@]}"; do
  rm -rf -- "${root:?}/${path}"
done
tar -xzf "${backup}" -C "${root}"
systemctl start "${service}"

for ((attempt = 1; attempt <= health_tries; attempt++)); do
  if curl --fail --silent --show-error --max-time 5 "${health_url}" >/dev/null; then
    echo "Rollback ${deployment_id} completed and health check passed."
    exit 0
  fi
  sleep 2
done

echo "rollback: service did not become healthy" >&2
systemctl status "${service}" --no-pager >&2 || true
exit 1
REMOTE_ROLLBACK
}

if [[ -n "${ROLLBACK_ID}" ]]; then
  echo "==> Rolling back deployment ${ROLLBACK_ID}"
  run_remote_rollback
  exit 0
fi

if [[ "${SKIP_TESTS}" != true ]]; then
  echo "==> Running local backend checks"
  (cd "${REPO_ROOT}" && venv/bin/python -m py_compile main.py server.py webui.py)
  (cd "${REPO_ROOT}" && PATH="${REPO_ROOT}/venv/bin:${PATH}" ./scripts/ci_gate.sh syntax)
  (cd "${REPO_ROOT}" && PATH="${REPO_ROOT}/venv/bin:${PATH}" ./scripts/ci_gate.sh flake8)

  echo "==> Linting and building Web assets locally"
  if [[ ! -d "${REPO_ROOT}/apps/dsa-web/node_modules" ]]; then
    (cd "${REPO_ROOT}/apps/dsa-web" && npm ci)
  fi
  (cd "${REPO_ROOT}/apps/dsa-web" && npm run lint && npm run build)
fi

"${REPO_ROOT}/venv/bin/python" "${REPO_ROOT}/scripts/check_static_assets.py" \
  "${REPO_ROOT}/static"

DEPLOYMENT_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
ARCHIVE_PATH="$(mktemp "${TMPDIR:-/tmp}/dsa-release.XXXXXX.tar.gz")"
REMOTE_ARCHIVE="/tmp/dsa-release-${DEPLOYMENT_ID}.tar.gz"

cleanup_archive() {
  rm -f -- "${ARCHIVE_PATH}"
}
trap 'cleanup_archive; cleanup_control_connection' EXIT

echo "==> Packaging deployment ${DEPLOYMENT_ID}"
(
  cd "${REPO_ROOT}"
  COPYFILE_DISABLE=1 tar -czf "${ARCHIVE_PATH}" "${RUNTIME_PATHS[@]}"
)

if tar -tzf "${ARCHIVE_PATH}" | grep -E '(^|/)\._' >/dev/null; then
  echo "deploy: archive contains forbidden macOS AppleDouble files" >&2
  exit 1
fi

echo "==> Checking remote installation"
ssh "${SSH_OPTIONS[@]}" "${DSA_DEPLOY_TARGET}" \
  "test -d '${DSA_DEPLOY_ROOT}' && test -x '${DSA_DEPLOY_ROOT}/venv/bin/python'"

echo "==> Uploading release archive"
scp "${SSH_OPTIONS[@]}" "${ARCHIVE_PATH}" \
  "${DSA_DEPLOY_TARGET}:${REMOTE_ARCHIVE}"

echo "==> Installing release with backup and automatic rollback"
REMOTE_COMMAND="$(remote_bash_command \
  "${DSA_DEPLOY_ROOT}" "${DSA_DEPLOY_SERVICE}" "${DSA_DEPLOY_HEALTH_URL}" \
  "${DEPLOYMENT_ID}" "${HEALTH_TRIES}" "${REMOTE_ARCHIVE}")"
ssh "${SSH_OPTIONS[@]}" "${DSA_DEPLOY_TARGET}" "${REMOTE_COMMAND}" <<'REMOTE_DEPLOY'
set -euo pipefail

root="$1"
service="$2"
health_url="$3"
deployment_id="$4"
health_tries="$5"
archive="$6"
stage="${root}/.deploy-stage-${deployment_id}"
backup_dir="${root}/deploy-backups"
backup="${backup_dir}/runtime-before-${deployment_id}.tar.gz"
runtime_paths=(main.py server.py webui.py requirements.txt api bot data_provider src strategies static)
deployment_started=false

[[ "${root}" == /* && "${root}" != "/" ]] || { echo "deploy: unsafe root" >&2; exit 2; }
[[ "${stage}" == "${root}/.deploy-stage-"* ]] || { echo "deploy: unsafe stage" >&2; exit 2; }
[[ "${archive}" == /tmp/dsa-release-* ]] || { echo "deploy: unsafe archive" >&2; exit 2; }

cleanup() {
  rm -rf -- "${stage}"
  rm -f -- "${archive}"
}

automatic_rollback() {
  local exit_code=$?
  trap - ERR
  set +e
  if [[ "${deployment_started}" == true && -f "${backup}" ]]; then
    echo "deploy: failure detected; starting automatic rollback" >&2
    systemctl stop "${service}"
    for path in "${runtime_paths[@]}"; do
      rm -rf -- "${root:?}/${path}"
    done
    tar -xzf "${backup}" -C "${root}"
    systemctl start "${service}"
  fi
  cleanup
  exit "${exit_code}"
}
trap automatic_rollback ERR
trap cleanup EXIT

mkdir -p "${stage}" "${backup_dir}"
chmod 700 "${stage}" "${backup_dir}"
tar -xzf "${archive}" -C "${stage}"

if [[ -n "$(find "${stage}" -name '._*' -print -quit)" ]]; then
  echo "deploy: staged release contains AppleDouble files" >&2
  false
fi

"${root}/venv/bin/python" -m compileall -q \
  "${stage}/main.py" "${stage}/server.py" "${stage}/webui.py" \
  "${stage}/api" "${stage}/bot" "${stage}/data_provider" "${stage}/src"

"${root}/venv/bin/python" - "${stage}/static" <<'PY_STATIC'
import re
import sys
from pathlib import Path

static_dir = Path(sys.argv[1])
index = static_dir / "index.html"
if not index.is_file():
    raise SystemExit("static/index.html is missing")
refs = re.findall(r'(?:src|href)=["\'](/assets/[^"\']+)["\']', index.read_text(errors="replace"))
missing = [ref for ref in refs if not (static_dir / ref.lstrip("/")).is_file()]
if missing:
    raise SystemExit(f"missing static assets: {missing}")
PY_STATIC

"${root}/venv/bin/python" - "${stage}" <<'PY_STRATEGIES'
import sys
from pathlib import Path

import yaml

stage = Path(sys.argv[1])
files = list((stage / "strategies").glob("*.yaml"))
files += list((stage / "src/services/screening/strategies").glob("*.yaml"))
if not files:
    raise SystemExit("no strategy YAML files found")
for path in files:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    if not isinstance(value, dict):
        raise SystemExit(f"strategy is not a mapping: {path}")
PY_STRATEGIES

if [[ -f "${root}/requirements.txt" ]] && ! cmp -s \
  "${root}/requirements.txt" "${stage}/requirements.txt"; then
  echo "deploy: requirements.txt changed; update the remote venv explicitly before deploying" >&2
  false
fi

existing_paths=()
for path in "${runtime_paths[@]}"; do
  [[ -e "${root}/${path}" ]] && existing_paths+=("${path}")
done
(( ${#existing_paths[@]} > 0 )) || { echo "deploy: no existing runtime files to back up" >&2; false; }
tar -czf "${backup}" -C "${root}" "${existing_paths[@]}"
chmod 600 "${backup}"

deployment_started=true
systemctl stop "${service}"
for path in "${runtime_paths[@]}"; do
  rm -rf -- "${root:?}/${path}"
  cp -a "${stage}/${path}" "${root}/${path}"
done
systemctl start "${service}"

for ((attempt = 1; attempt <= health_tries; attempt++)); do
  if curl --fail --silent --show-error --max-time 5 "${health_url}" >/dev/null; then
    deployment_started=false
    trap - ERR
    echo "Deployment ${deployment_id} completed and health check passed."
    exit 0
  fi
  sleep 2
done

echo "deploy: service did not become healthy" >&2
systemctl status "${service}" --no-pager >&2 || true
false
REMOTE_DEPLOY

echo "==> Deployment ${DEPLOYMENT_ID} succeeded"
echo "Rollback command: scripts/deploy_remote.sh --rollback ${DEPLOYMENT_ID}"
