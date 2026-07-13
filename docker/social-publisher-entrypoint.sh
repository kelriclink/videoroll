#!/bin/sh
set -eu

python -m videoroll.deployment

display="${SOCIAL_LOGIN_DISPLAY:-:99}"
screen="${SOCIAL_LOGIN_SCREEN:-1440x900x24}"
display_number="${display#:}"

rm -f "/tmp/.X${display_number}-lock"
Xvfb "$display" -screen 0 "$screen" -nolisten tcp >/tmp/xvfb.log 2>&1 &

attempt=0
while [ ! -S "/tmp/.X11-unix/X${display_number}" ]; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 50 ]; then
    echo "Xvfb failed to create display $display" >&2
    exit 1
  fi
  sleep 0.1
done

DISPLAY="$display" fluxbox >/tmp/fluxbox.log 2>&1 &
# /dev/shm is a container tmpfs.  Keep the VNC credential in a private
# directory and supply x11vnc only its password-file hash, never a no-password
# flag or a browser-visible password.
umask 077
vnc_password_dir="${VNC_PASSWORD_DIR:-/dev/shm/videoroll-vnc}"
mkdir -p "$vnc_password_dir"
chmod 700 "$vnc_password_dir"
vnc_password_file="$(mktemp "$vnc_password_dir/password.XXXXXX")"
vnc_password="$(dd if=/dev/urandom bs=24 count=1 2>/dev/null | base64 | tr -d '\n' | cut -c1-24)"
printf '%s\n%s\n' "$vnc_password" "$vnc_password" | x11vnc -storepasswd "$vnc_password_file" >/tmp/x11vnc-password.log 2>&1
unset vnc_password
chmod 600 "$vnc_password_file"
x11vnc -display "$display" -forever -shared -rfbauth "$vnc_password_file" -localhost -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 >/tmp/websockify.log 2>&1 &

export DISPLAY="$display"

if [ "$#" -eq 0 ]; then
  set -- uvicorn videoroll.apps.social_publisher.main:app --host 0.0.0.0 --port 8010
fi

exec "$@"
