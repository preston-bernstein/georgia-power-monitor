#!/usr/bin/env bash
# Deploy Georgia Power Monitor to a Linux host under the gp-monitor nologin service user.
#
# Mirrors algo-macro-monitor/scripts/deploy.sh's structure and service-user convention, adapted
# for gp-monitor: this script provisions the service user itself (algo-macro's assumes the user is
# already provisioned) and installs into a dedicated venv dir rather than an in-app .venv. Intended
# to be run ON the target deploy host (desktop or xps-agent) from a checked-out clone of this repo
# -- REPO_ROOT below is resolved from this script's own location, so "the host" and "the local
# repo" are the same machine; there is no over-the-network rsync to a separate box. Run as a user
# with passwordless sudo (see docs/DECISIONS.md / CLAUDE.md's Home lab SSH access table).
#
# Usage: scripts/deploy.sh [--dry-run]
set -euo pipefail

DRY_RUN=0
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1

SERVICE_USER=gp-monitor
APP_DIR="/home/${SERVICE_USER}/app"
VENV_DIR="/home/${SERVICE_USER}/venv"
ENV_FILE="${APP_DIR}/.env"
CONFIG_FILE="${APP_DIR}/config.yaml"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        printf 'DRY-RUN:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

# run_sudo: same as run(), but always goes through sudo (steps 1, 5, 6, 8 below require root even
# when the rest of the script is executed as an unprivileged user with sudo rights).
run_sudo() {
    run sudo "$@"
}

echo "==> Deploying gp-monitor to ${APP_DIR} (service user: ${SERVICE_USER})"

# 1. Service user: verify, or create it if missing (unlike algo-macro-monitor, gp-monitor's service
#    user is provisioned by this script, not out-of-band).
if id "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "==> Service user ${SERVICE_USER} already exists"
else
    echo "==> Creating service user ${SERVICE_USER}"
    run_sudo useradd -r -s /sbin/nologin "${SERVICE_USER}"
fi

# 2. App + venv directories, owned by the service user.
run_sudo install -d -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}" "${APP_DIR}/data" "${VENV_DIR}"

# 3. Sync code (src, pyproject, ops/) from the local repo checkout into the service user's app dir.
run_sudo rsync -a --delete \
    "${REPO_ROOT}/src" \
    "${REPO_ROOT}/pyproject.toml" \
    "${REPO_ROOT}/ops" \
    "${APP_DIR}/"
if [[ $DRY_RUN -eq 1 ]]; then
    echo "DRY-RUN: install config.example.yaml -> ${CONFIG_FILE} (only if it doesn't already exist)"
elif sudo test -f "${CONFIG_FILE}"; then
    echo "==> ${CONFIG_FILE} already exists, leaving it in place"
else
    sudo install -o "${SERVICE_USER}" -g "${SERVICE_USER}" -m 640 \
        "${REPO_ROOT}/config.example.yaml" "${CONFIG_FILE}"
fi
run_sudo chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

# 4. Create/refresh the service-user venv and install the package (editable -- app dir is the
#    source of truth after step 3's rsync, so `-e` avoids a second copy inside the venv).
run_sudo -u "${SERVICE_USER}" python3 -m venv "${VENV_DIR}"
run_sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install --upgrade pip
run_sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/pip" install -e "${APP_DIR}"

# 5. Install systemd units.
run_sudo install -m 644 "${REPO_ROOT}/systemd/gp-monitor-poll.service" /etc/systemd/system/
run_sudo install -m 644 "${REPO_ROOT}/systemd/gp-monitor-poll.timer" /etc/systemd/system/

# 6. Reload systemd so it picks up the units installed in step 5.
run_sudo systemctl daemon-reload

# 7. Smoke test: ops/preflight.py against the configured Home Assistant target. Requires
#    Preston-provided real credentials at ${ENV_FILE} (EnvironmentFile= for the systemd service --
#    see systemd/gp-monitor-poll.service) -- this script never creates or edits that file. If it's
#    missing, preflight still runs and reports the HA-reachability check; only the token-validity
#    check is expected to fail, and that failure is reported but does not abort this deploy (a
#    fresh host legitimately won't have real credentials yet until Preston drops them in).
if [[ $DRY_RUN -eq 0 ]] && ! sudo -u "${SERVICE_USER}" test -f "${ENV_FILE}"; then
    echo "WARN: ${ENV_FILE} does not exist -- preflight's HA-token-validity check will fail until Preston provides one." >&2
fi
if ! run_sudo -u "${SERVICE_USER}" "${VENV_DIR}/bin/python" "${APP_DIR}/ops/preflight.py" \
    --config "${CONFIG_FILE}" --env-file "${ENV_FILE}"; then
    echo "WARN: preflight reported a failure (see PASS/FAIL lines above) -- continuing deploy; re-run scripts/deploy.sh after fixing." >&2
fi

# 8. Enable + (re)start the timer. `restart`, not `enable --now`: this script is meant to be
#    re-run on redeploys (see header comment), and an already-active timer does NOT reload its
#    unit file on its own even after `daemon-reload` (step 6) -- only a restart reparses it. Using
#    `enable --now` here was a real bug: a systemd/gp-monitor-poll.timer edit landed on disk and
#    got daemon-reloaded, but the live timer instance kept running on its stale, previously-loaded
#    config until manually restarted.
run_sudo systemctl enable gp-monitor-poll.timer
run_sudo systemctl restart gp-monitor-poll.timer

echo "==> Done. Check: systemctl status gp-monitor-poll.timer"
