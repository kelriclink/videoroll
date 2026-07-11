#!/bin/sh
set -eu

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
x11vnc -display "$display" -forever -shared -nopw -localhost -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 0.0.0.0:6080 localhost:5900 >/tmp/websockify.log 2>&1 &

export DISPLAY="$display"

if [ "$#" -eq 0 ]; then
  set -- uvicorn videoroll.apps.social_publisher.main:app --host 0.0.0.0 --port 8010
fi

exec "$@"
