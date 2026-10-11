#!/usr/bin/env bash
set -euo pipefail

# On a fresh Linux host, only the VideoRoll PostgreSQL database is retained.
# The separate Hatchet database may be empty/new.
if [[ "${1:-}" != "--execute" ]]; then
  echo "usage: sudo DATABASE_URL=... HATCHET_DATABASE_URL=... $0 --execute" >&2
  exit 64
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
NATIVE_ENV=/opt/videoroll/config/videoroll.env

stop_partial_restore() {
  status=$?
  trap - ERR
  echo "Restore failed; stopping native services. PostgreSQL changes are NOT rolled back." >&2
  systemctl disable --now videoroll.target videoroll-ffplayout.service >/dev/null 2>&1 || true
  exit "$status"
}
trap stop_partial_restore ERR

if [[ ! -f "$NATIVE_ENV" ]]; then
  : "${DATABASE_URL:?DATABASE_URL is required when no native environment exists}"
  : "${HATCHET_DATABASE_URL:?HATCHET_DATABASE_URL must point to a new/rebuildable Hatchet database}"
  install -d -m 0750 /opt/videoroll/config
  python3 "$ROOT_DIR/scripts/native/bootstrap_env.py" \
    --database-url "$DATABASE_URL" \
    --hatchet-database-url "$HATCHET_DATABASE_URL" \
    --output "$NATIVE_ENV"
fi

echo "[1/7] Install native runtime"
bash "$ROOT_DIR/scripts/native/install.sh"

echo "[2/7] Build/install native Web and Hatchet"
bash "$ROOT_DIR/scripts/native/build_web.sh"
if [[ ! -x /opt/videoroll/bin/hatchet-lite || ! -x /opt/videoroll/bin/hatchet-admin ]]; then
  bash "$ROOT_DIR/scripts/native/build_hatchet.sh"
fi

python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/preflight.py" --connect

python3 "$ROOT_DIR/scripts/native/videorollctl.py" doctor

echo "[3/7] Prepare PostgreSQL for a database-only recovery"
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python -m videoroll.db.migrate upgrade
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/db_only_recovery.py"
python3 "$ROOT_DIR/scripts/native/env_exec.py" "$NATIVE_ENV" \
  /opt/videoroll/venvs/core/bin/python "$ROOT_DIR/scripts/native/db_only_recovery.py" --execute

echo "[4/7] Start native Redis/Hatchet and generate worker token"
bash "$ROOT_DIR/scripts/native/start_native_infra.sh"

echo "[5/7] Start all native VideoRoll systemd units"
if [[ -x /opt/videoroll/bin/ffplayout ]]; then
  systemctl start videoroll-ffplayout.service
else
  echo "ffplayout not installed: playout UI will be unavailable until the binary is built." >&2
fi
systemctl start videoroll.target

echo "[6/7] Check HTTP and Redis services"
ready=0
for attempt in $(seq 1 45); do
  if bash "$ROOT_DIR/scripts/native/health.sh"; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" != "1" ]]; then
  echo "Native services did not pass health checks" >&2
  false
fi

echo "[7/7] Enable startup on boot"
systemctl enable videoroll.target
if systemctl is-active --quiet videoroll-ffplayout.service; then
  systemctl enable videoroll-ffplayout.service
fi
touch "${VIDEOROLL_DATA_ROOT:-/opt/videoroll/data}/db-only-restore-complete"
trap - ERR
echo "Database-only native restore complete. No Docker runtime is required."
echo "Old Fernet credentials, if present, require re-entry."
