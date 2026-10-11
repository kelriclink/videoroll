#!/usr/bin/env bash
set -euo pipefail

# Existing Compose -> prepared native runtime. Disaster recovery is separate.
if [[ "${1:-}" != "--execute" || $# -ne 1 ]]; then
  echo "usage: sudo $0 --execute (for lost data use restore_from_database.sh)" >&2
  exit 64
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NATIVE_ENV=/etc/videoroll/videoroll.env
old_stopped=0

rollback_on_error() {
  status=$?
  trap - ERR
  if [[ "$old_stopped" == "1" ]]; then
    echo "Cutover failed; stopping native services and restoring old Compose." >&2
    systemctl disable --now videoroll.target videoroll-ffplayout.service >/dev/null 2>&1 || true
    "$ROOT_DIR/scripts/prod_compose.sh" up -d --no-build --remove-orphans || true
  fi
  exit "$status"
}
trap rollback_on_error ERR

test -s "$NATIVE_ENV" || {
  echo "missing $NATIVE_ENV; run native install and configure database first" >&2
  exit 1
}
echo "[1/7] Verify the separately installed runtime"
if [[ ! -x /usr/local/bin/ffplayout && "${VIDEOROLL_SKIP_FFPLAYOUT:-0}" != "1" ]]; then
  echo "Install native ffplayout first or set VIDEOROLL_SKIP_FFPLAYOUT=1" >&2
  exit 1
fi

echo "[2/7] Validate native configuration"
systemctl daemon-reload
nginx -t -c /etc/videoroll/nginx.conf
for service in videoroll-redis videoroll-hatchet videoroll-social@api videoroll-social@worker; do
  systemctl cat "$service.service" >/dev/null
done
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python -m videoroll.deployment

python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/preflight.py" --connect

# Abort before the old stack is stopped if a reused env still points at the
# old Docker/native data tree or the project-local data is not prepared.
python3 "$ROOT_DIR/scripts/native/videorollctl.py" doctor
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/check_idle.py"

echo "[3/7] Stop old Docker Compose processes"
"$ROOT_DIR/scripts/prod_compose.sh" stop
old_stopped=1

echo "[4/7] Recheck idle state; preserve credentials, file indexes and leases"
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/check_idle.py"

echo "[5/7] Start native Redis/Hatchet and mint worker token"
bash "$ROOT_DIR/scripts/native/start_native_infra.sh"

echo "[6/7] Start all native services"
if [[ -x /usr/local/bin/ffplayout && "${VIDEOROLL_SKIP_FFPLAYOUT:-0}" != "1" ]]; then
  systemctl start videoroll-ffplayout.service
fi
systemctl start videoroll.target

echo "[7/7] Verify native health before enabling on boot"
ready=0
for attempt in $(seq 1 45); do
  if bash "$ROOT_DIR/scripts/native/health.sh"; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  echo "Native service health failed" >&2
  false
fi
systemctl enable videoroll.target
if systemctl is-active --quiet videoroll-ffplayout.service; then
  systemctl enable videoroll-ffplayout.service
fi
touch "$ROOT_DIR/data/native-cutover-complete"
trap - ERR
echo "VideoRoll native cutover completed. No Docker containers required at runtime."
