#!/usr/bin/env bash
set -euo pipefail

role="${1:?usage: videoroll-run-social-role {api|worker|scheduler}}"
VENV="${VIDEOROLL_SOCIAL_VENV:-/opt/videoroll/venvs/social}"
RUNTIME_DIR="${RUNTIME_DIRECTORY:-/run/videoroll-social-$role}"

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export ORCHESTRATOR_URL="${ORCHESTRATOR_URL:-http://127.0.0.1:8000}"
export SOCIAL_PUBLISH_WORK_DIR="${SOCIAL_PUBLISH_WORK_DIR:-/opt/videoroll/current/data/work/social-publisher}"
export SAU_RUNTIME_DIR="${SAU_RUNTIME_DIR:-/opt/videoroll/current/social-auto-upload}"
export SAU_EXECUTABLE="${SAU_EXECUTABLE:-$VENV/bin/sau}"
export SAU_COOKIES_DIR="$RUNTIME_DIR/cookies"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/opt/videoroll/current/data/browsers}"
export HOME="${HOME:-/opt/videoroll/current/data/home}"

mkdir -p "$SAU_COOKIES_DIR"
chmod 700 "$RUNTIME_DIR" "$SAU_COOKIES_DIR"

start_display_stack() {
  local display="$1"
  local rfb_port="$2"
  local bridge_port="$3"
  local websockify_port="$4"
  local screen="${SOCIAL_LOGIN_SCREEN:-1440x900x24}"
  local display_number="${display#:}"
  local password_dir="$RUNTIME_DIR/vnc"

  export DISPLAY="$display"
  export SOCIAL_LOGIN_DISPLAY="$display"

  rm -f "/tmp/.X${display_number}-lock" "/tmp/.X11-unix/X${display_number}" 2>/dev/null || true
  Xvfb "$display" -screen 0 "$screen" -nolisten tcp >"$RUNTIME_DIR/xvfb.log" 2>&1 &

  for _ in $(seq 1 50); do
    [[ -S "/tmp/.X11-unix/X${display_number}" ]] && break
    sleep 0.1
  done
  [[ -S "/tmp/.X11-unix/X${display_number}" ]] || {
    echo "Xvfb failed to create display $display" >&2
    exit 1
  }

  DISPLAY="$display" fluxbox >"$RUNTIME_DIR/fluxbox.log" 2>&1 &

  mkdir -p "$password_dir"
  chmod 700 "$password_dir"
  local vnc_password_file="$password_dir/password"
  local bridge_password_file="$password_dir/bridge-password"
  local vnc_password
  vnc_password="$(dd if=/dev/urandom bs=12 count=1 2>/dev/null | base64 | tr -d '\n' | cut -c1-8)"
  printf '%s' "$vnc_password" >"$bridge_password_file"
  chmod 600 "$bridge_password_file"
  x11vnc -storepasswd "$vnc_password" "$vnc_password_file" >"$RUNTIME_DIR/x11vnc-password.log" 2>&1
  unset vnc_password
  chmod 600 "$vnc_password_file"

  x11vnc -display "$display" -forever -shared -rfbauth "$vnc_password_file"     -localhost -rfbport "$rfb_port" >"$RUNTIME_DIR/x11vnc.log" 2>&1 &

  "$VENV/bin/python" -m videoroll.apps.social_publisher.vnc_bridge     --listen-port "$bridge_port"     --target-port "$rfb_port"     --password-file "$bridge_password_file" >"$RUNTIME_DIR/vnc-bridge.log" 2>&1 &

  websockify --web=/usr/share/novnc "127.0.0.1:$websockify_port"     "127.0.0.1:$bridge_port" >"$RUNTIME_DIR/websockify.log" 2>&1 &
}

case "$role" in
  api)
    export DEPLOYMENT_ROLE=social-publisher-api
    "$VENV/bin/python" -m videoroll.deployment
    start_display_stack :99 5900 5901 6080
    exec "$VENV/bin/uvicorn" videoroll.apps.social_publisher.main:app --host 127.0.0.1 --port 8010
    ;;
  worker)
    export DEPLOYMENT_ROLE=social-publisher-worker
    "$VENV/bin/python" -m videoroll.deployment
    start_display_stack :100 5910 5911 6081
    exec "$VENV/bin/celery" -A videoroll.apps.social_publisher.worker:celery_app       worker -Q social_publish -l INFO --concurrency "${SOCIAL_PUBLISH_CONCURRENCY:-1}"
    ;;
  scheduler)
    export DEPLOYMENT_ROLE=social-publisher-scheduler
    exec "$VENV/bin/celery" -A videoroll.apps.social_publisher.worker:celery_app       beat -l INFO --schedule /opt/videoroll/current/data/state/social-celerybeat-schedule
    ;;
  *)
    echo "unknown social role: $role" >&2
    exit 64
    ;;
esac
