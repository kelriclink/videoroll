#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
INSTALL_ROOT="${VIDEOROLL_INSTALL_ROOT:-/opt/videoroll}"
if [[ "$INSTALL_ROOT" != /opt/videoroll ]]; then
  echo "Native systemd units require /opt/videoroll; custom install roots are not supported." >&2
  exit 1
fi
if systemctl is-active --quiet videoroll.target || systemctl is-active --quiet videoroll.service; then
  echo "Stop the native stack before installing runtime dependencies." >&2
  exit 1
fi
test -f "$ROOT_DIR/social-auto-upload/sau_cli.py" || {
  echo "Initialize social-auto-upload first: git submodule update --init --recursive" >&2
  exit 1
}
VENV_ROOT="$INSTALL_ROOT/venvs"
# Remote ASR does not require local torch/OpenVINO/faster-whisper runtimes.
ASR_RUNTIME="${VIDEOROLL_NATIVE_ASR_RUNTIME:-local}"
if [[ "$ASR_RUNTIME" != "local" && "$ASR_RUNTIME" != "remote" ]]; then
  echo "VIDEOROLL_NATIVE_ASR_RUNTIME must be local or remote" >&2
  exit 64
fi
# An existing production data tree can be reused without moving any media.
# /opt/videoroll/current is only a stable symlink to this checkout.
DATA_ROOT="${VIDEOROLL_DATA_ROOT:-/opt/videoroll/data}"
if [[ "$DATA_ROOT" != /* || "$DATA_ROOT" == / ]]; then
  echo "VIDEOROLL_DATA_ROOT must be an absolute directory path other than /" >&2
  exit 1
fi
# Do not link persistent data into the Git checkout. Use configured root.
ENV_DIR=/opt/videoroll/config

env_value() {
  local key="$1"
  local file="${VIDEOROLL_SOURCE_ENV:-$ROOT_DIR/.env}"
  [[ -f "$file" ]] || return 0
  local value
  value="$(sed -n "s/^${key}=//p" "$file" | tail -n 1 | tr -d '\r')"
  if [[ ${#value} -ge 2 ]]; then
    if [[ "${value:0:1}" == '"' && "${value: -1}" == '"' ]]; then
      value="${value:1:${#value}-2}"
    elif [[ "${value:0:1}" == "'" && "${value: -1}" == "'" ]]; then
      value="${value:1:${#value}-2}"
    fi
  fi
  printf '%s' "$value"
}

APP_UID="${APP_UID:-$(env_value APP_UID)}"
APP_GID="${APP_GID:-$(env_value APP_GID)}"
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-10001}"
if [[ ! "$APP_UID" =~ ^[0-9]+$ || ! "$APP_GID" =~ ^[0-9]+$ ]]; then
  echo "APP_UID and APP_GID must be numeric" >&2
  exit 1
fi

policy_created=0
cleanup_policy() {
  if [[ "$policy_created" == "1" ]]; then
    rm -f /usr/sbin/policy-rc.d
  fi
}
trap cleanup_policy EXIT
if [[ ! -e /usr/sbin/policy-rc.d ]]; then
  printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
  chmod 0755 /usr/sbin/policy-rc.d
  policy_created=1
fi

apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip ffmpeg curl \
  xvfb fluxbox novnc websockify x11vnc xauth \
  fonts-noto-cjk ca-certificates pciutils ocl-icd-libopencl1

cleanup_policy
policy_created=0
trap - EXIT

# Do not disable Redis that may belong to other applications on a shared
# production host. If it already listens on port 6379, require explicit
# operator resolution during cutover rather than altering another service.
if systemctl is-active --quiet redis-server.service; then
  echo "Warning: redis-server.service is already running; check port 6379 before enabling VideoRoll." >&2
else
  systemctl disable redis-server.service >/dev/null 2>&1 || true
fi

# The repository and its data/ directory must never be chowned/chmodded
# implicitly; add the account without creating its home here.

if getent group videoroll >/dev/null; then
  existing_gid="$(getent group videoroll | cut -d: -f3)"
  if [[ "$existing_gid" != "$APP_GID" ]]; then
    echo "existing videoroll group gid=$existing_gid does not match APP_GID=$APP_GID" >&2
    exit 1
  fi
else
  groupadd --non-unique --gid "$APP_GID" videoroll
fi

if id videoroll >/dev/null 2>&1; then
  existing_uid="$(id -u videoroll)"
  existing_gid="$(id -g videoroll)"
  if [[ "$existing_uid" != "$APP_UID" || "$existing_gid" != "$APP_GID" ]]; then
    echo "existing videoroll user is $existing_uid:$existing_gid, expected $APP_UID:$APP_GID" >&2
    exit 1
  fi
else
  useradd --non-unique --uid "$APP_UID" --gid videoroll --no-create-home \
    --home-dir "$DATA_ROOT/home" --shell /usr/sbin/nologin videoroll
fi
for group in video render; do
  if getent group "$group" >/dev/null; then
    usermod -aG "$group" videoroll
  fi
done

# Do NOT chown/chmod an existing data directory: the Docker deployment may
# have different ownership and may contain hundreds of gigabytes of files.
# Only newly created directories receive videoroll ownership.
ensure_data_dir() {
  local path="$1"
  if [[ -e "$path" && ! -d "$path" ]]; then
    echo "Data path exists but is not a directory: $path" >&2
    exit 1
  fi
  if [[ ! -d "$path" ]]; then
    install -d -o videoroll -g videoroll -m 0750 "$path"
  fi
}
for data_dir in \
  "$DATA_ROOT" "$DATA_ROOT/home" "$DATA_ROOT/state" "$DATA_ROOT/state/render-worker" \
  "$DATA_ROOT/work" "$DATA_ROOT/work/videoroll" "$DATA_ROOT/work/render-worker" \
  "$DATA_ROOT/work/social-publisher" "$DATA_ROOT/nginx" \
  "$DATA_ROOT/nginx/client_temp" "$DATA_ROOT/nginx/proxy_temp" \
  "$DATA_ROOT/models" "$DATA_ROOT/models/whisper" "$DATA_ROOT/models/embeddings" \
  "$DATA_ROOT/dictionaries" "$DATA_ROOT/redis-native" "$DATA_ROOT/hatchet" \
  "$DATA_ROOT/browsers" "$DATA_ROOT/web" \
  "$DATA_ROOT/storage" "$DATA_ROOT/storage/objects" "$DATA_ROOT/storage/.partial" \
  "$DATA_ROOT/storage/playout-media" \
  "$DATA_ROOT/ffplayout" "$DATA_ROOT/ffplayout/db" "$DATA_ROOT/ffplayout/logs" \
  "$DATA_ROOT/ffplayout/playlists" "$DATA_ROOT/ffplayout/public" \
  "$DATA_ROOT/ffplayout/media" "$DATA_ROOT/ffplayout/tv-media" \
  "$DATA_ROOT/ffplayout/recordings"
do
  ensure_data_dir "$data_dir"
done
work_check="$DATA_ROOT/work/videoroll"
if [[ -d "$DATA_ROOT/storage/work/videoroll" ]]; then
  work_check="$DATA_ROOT/storage/work/videoroll"
fi
if ! runuser -u videoroll -- test -x "$DATA_ROOT" \
  || ! runuser -u videoroll -- test -w "$work_check"; then
  echo "The videoroll service user cannot traverse data or write the work directory." >&2
  echo "Check existing directory ownership and ACLs; the installer will not mutate them." >&2
  exit 1
fi

install -d -m 0755 "$INSTALL_ROOT" "$VENV_ROOT" "$INSTALL_ROOT/bin" "$INSTALL_ROOT/libexec" "$INSTALL_ROOT/share"
# DrvFs/NTFS venvs exhibit extremely slow pip installs on WSL; fail early.
if [[ -x /usr/bin/findmnt && "${VIDEOROLL_NATIVE_ALLOW_NONPOSIX_VENV:-0}" != "1" ]]; then
  venv_filesystem="$(findmnt -n -T "$VENV_ROOT" -o FSTYPE | head -n 1)"
  case "$venv_filesystem" in
    drvfs|9p|fuseblk|ntfs|ntfs3)
      echo "Refusing Python venv on $venv_filesystem ($VENV_ROOT). Use a Linux filesystem such as ext4." >&2
      exit 1
      ;;
  esac
fi
if [[ -e "$INSTALL_ROOT/current" && ! -L "$INSTALL_ROOT/current" ]]; then
  echo "$INSTALL_ROOT/current exists and is not a symlink; refusing to overwrite." >&2
  exit 1
fi
ln -sfn "$ROOT_DIR" "$INSTALL_ROOT/current"
if [[ ! -e "$ROOT_DIR/web" && ! -L "$ROOT_DIR/web" ]]; then
  ln -s "$INSTALL_ROOT/web" "$ROOT_DIR/web"
fi

# Service users need directory traversal for non-secret Redis/nginx config.
# The root-only 0600 environment file is read by systemd PID 1.
install -d -m 0711 "$ENV_DIR"
chmod 0711 "$ENV_DIR"
if [[ ! -f "$ENV_DIR/videoroll.env" ]]; then
  if [[ -f "${VIDEOROLL_SOURCE_ENV:-$ROOT_DIR/.env}" ]]; then
    VIDEOROLL_DATA_ROOT="$DATA_ROOT" python3 "$ROOT_DIR/scripts/native/migrate_env.py" \
      --source "${VIDEOROLL_SOURCE_ENV:-$ROOT_DIR/.env}" --output "$ENV_DIR/videoroll.env"
  else
    VIDEOROLL_DATA_ROOT="$DATA_ROOT" python3 "$ROOT_DIR/scripts/native/migrate_env.py" \
      --source "$ROOT_DIR/deploy/systemd/videoroll.env.example" --output "$ENV_DIR/videoroll.env"
    echo "No .env found; created template $ENV_DIR/videoroll.env." >&2
  fi
fi

# Refuse a different persistent data root when config already exists.
existing_root="$(sed -n 's/^VIDEOROLL_DATA_ROOT=//p' "$ENV_DIR/videoroll.env" | tail -n 1 | tr -d '"')"
if [[ -n "$existing_root" && "$existing_root" != "$DATA_ROOT" ]]; then
  echo "Configured data root differs from requested VIDEOROLL_DATA_ROOT" >&2
  exit 1
fi

# Use the same Python ABI for all five environments, including Debian 13.
# Never replace or upgrade the distribution's /usr/bin/python3.
PYTHON_BIN="${VIDEOROLL_PYTHON:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3.12)"
  else
    python3 -m venv "$VENV_ROOT/python-tooling"
    "$VENV_ROOT/python-tooling/bin/pip" install 'uv>=0.8,<1'
    export UV_PYTHON_INSTALL_DIR="$INSTALL_ROOT/python-runtimes"
    "$VENV_ROOT/python-tooling/bin/uv" python install 3.12
    PYTHON_BIN="$("$VENV_ROOT/python-tooling/bin/uv" python find 3.12)"
  fi
fi
"$PYTHON_BIN" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Python 3.12 is required"'

make_venv() {
  local name="$1"
  local dir="$VENV_ROOT/$name"
  "$PYTHON_BIN" -m venv "$dir"
  "$dir/bin/pip" install --upgrade pip
}

make_venv core
"$VENV_ROOT/core/bin/pip" install -c "$ROOT_DIR/requirements.lock" "$ROOT_DIR"

make_venv subtitle
if [[ "$ASR_RUNTIME" == "local" ]]; then
  "$VENV_ROOT/subtitle/bin/pip" install --index-url "${TORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}" "torch==2.14.0"
  "$VENV_ROOT/subtitle/bin/pip" install -c "$ROOT_DIR/requirements.lock" "${ROOT_DIR}[subtitle,asr]"
else
  "$VENV_ROOT/subtitle/bin/pip" install -c "$ROOT_DIR/requirements.lock" "$ROOT_DIR"
fi

make_venv workflow
"$VENV_ROOT/workflow/bin/pip" install -r "$ROOT_DIR/docker/workflow-requirements.txt"
"$VENV_ROOT/workflow/bin/pip" install --no-deps "$ROOT_DIR"

make_venv subtitle-workflow
if [[ "$ASR_RUNTIME" == "local" ]]; then
  "$VENV_ROOT/subtitle-workflow/bin/pip" install --index-url "${TORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}" "torch==2.14.0"
  "$VENV_ROOT/subtitle-workflow/bin/pip" install -c "$ROOT_DIR/requirements.lock" "${ROOT_DIR}[subtitle,asr]"
else
  "$VENV_ROOT/subtitle-workflow/bin/pip" install -c "$ROOT_DIR/requirements.lock" "$ROOT_DIR"
fi
"$VENV_ROOT/subtitle-workflow/bin/pip" install "hatchet-sdk==1.40.3" "protobuf==6.33.6"

make_venv social
"$VENV_ROOT/social/bin/pip" install --upgrade pip
"$VENV_ROOT/social/bin/pip" install -r "$ROOT_DIR/docker/social-publisher-requirements.txt"
"$VENV_ROOT/social/bin/pip" install --no-deps "$ROOT_DIR"
"$VENV_ROOT/social/bin/pip" install "$ROOT_DIR/social-auto-upload"

if [[ "${VIDEOROLL_SKIP_SOCIAL_BROWSER_INSTALL:-0}" != "1" ]]; then
  PLAYWRIGHT_BROWSERS_PATH="$DATA_ROOT/browsers" \
    "$VENV_ROOT/social/bin/patchright" install --with-deps chromium
  chromium_bin="$(find "$DATA_ROOT/browsers" -type f -path '*/chrome-linux64/chrome' -print -quit)"
  if [[ -z "$chromium_bin" || ! -x "$chromium_bin" ]]; then
    echo "Patchright Chromium installation is incomplete" >&2
    exit 1
  fi
  chown -R videoroll:videoroll "$DATA_ROOT/browsers"
fi

bash "$ROOT_DIR/scripts/native/install_system_binaries.sh"
install -d -m 0755 "$INSTALL_ROOT/libexec"
# A stable executable launcher also works with source archives lacking exec bits.
rm -f /opt/videoroll/bin/videorollctl
install -m 0755 "$ROOT_DIR/scripts/native/videorollctl" /opt/videoroll/bin/videorollctl
install -m 0755 "$ROOT_DIR/scripts/native/run-role.sh" /opt/videoroll/libexec/videoroll-run-role
install -m 0755 "$ROOT_DIR/scripts/native/run-hatchet.sh" /opt/videoroll/libexec/videoroll-run-hatchet
install -m 0755 "$ROOT_DIR/scripts/native/init_hatchet_token.sh" /opt/videoroll/libexec/videoroll-init-hatchet-token
install -m 0755 "$ROOT_DIR/scripts/native/run-social-role.sh" /opt/videoroll/libexec/videoroll-run-social-role
install -m 0755 "$ROOT_DIR/scripts/native/run-ffplayout.sh" /opt/videoroll/libexec/videoroll-run-ffplayout
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll@.service" /etc/systemd/system/videoroll@.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll.target" /etc/systemd/system/videoroll.target
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-web.service" /etc/systemd/system/videoroll-web.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-ffplayout.service" /etc/systemd/system/videoroll-ffplayout.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-redis.service" /etc/systemd/system/videoroll-redis.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-hatchet.service" /etc/systemd/system/videoroll-hatchet.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-hatchet-token.service" /etc/systemd/system/videoroll-hatchet-token.service
install -m 0644 "$ROOT_DIR/deploy/systemd/videoroll-social@.service" /etc/systemd/system/videoroll-social@.service
# Nginx and Redis do not expand EnvironmentFile path variables.
VIDEOROLL_DATA_ROOT="$DATA_ROOT" python3 - "$ROOT_DIR" "$ENV_DIR" <<'PYCONFIG'
import os, pathlib, sys
source, destination = map(pathlib.Path, sys.argv[1:3])
for template, filename in (
    ("deploy/systemd/redis.conf", "redis.conf"),
    ("deploy/systemd/nginx-videoroll.conf", "nginx.conf"),
):
    content = (source / template).read_text()
    if "@VIDEOROLL_DATA_ROOT@" not in content:
        raise SystemExit("Missing data-root placeholder: " + template)
    output = destination / filename
    output.write_text(content.replace("@VIDEOROLL_DATA_ROOT@", os.environ["VIDEOROLL_DATA_ROOT"]))
    output.chmod(0o644)
PYCONFIG
# This catches the real systemd Redis permission failure missed by mock tests.
for config_file in redis.conf nginx.conf; do
  if ! runuser -u videoroll -- test -r "$ENV_DIR/$config_file"; then
    echo "Native service user cannot read $ENV_DIR/$config_file" >&2
    exit 1
  fi
done
if runuser -u videoroll -- test -r "$ENV_DIR/videoroll.env"; then
  echo "Unsafe: unprivileged service user can read root-only environment file" >&2
  exit 1
fi

if [[ -n "${VIDEOROLL_WEB_DIST:-}" ]]; then
  bash "$ROOT_DIR/scripts/native/build_web.sh" --artifact-dir "$VIDEOROLL_WEB_DIST"
fi
if [[ -n "${HATCHET_ARTIFACT_DIR:-}" ]]; then
  bash "$ROOT_DIR/scripts/native/build_hatchet.sh"
fi
if [[ -n "${FFPLAYOUT_ARTIFACT_DIR:-}" ]]; then
  bash "$ROOT_DIR/scripts/native/build_ffplayout.sh" --artifact-dir "$FFPLAYOUT_ARTIFACT_DIR"
fi

systemctl daemon-reload

echo
echo "Native runtime installed but not enabled or started."
echo "Run videorollctl doctor, prepare remaining binaries, then review the native cutover."
