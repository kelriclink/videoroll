#!/usr/bin/env bash
# Run once as root before starting VideoRoll's unified dubbing service.
set -euo pipefail
path="${DUBBING_SERVICE_TOKEN_FILE:-/opt/videoroll/config/dubbing-service.token}"
if [ "$(id -u)" -ne 0 ];then echo 'Run as root' >&2; exit 77;fi
install -d -m 750 -o root -g videoroll "$(dirname "$path")"
if [ ! -f "$path" ];then
  umask 077
  tmp="$(mktemp "$(dirname "$path")/.dubbing-token.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT
  openssl rand -hex 32 > "$tmp"
  chown videoroll:videoroll "$tmp"
  chmod 600 "$tmp"
  mv -n "$tmp" "$path"
fi
chown videoroll:videoroll "$path"
chmod 600 "$path"
printf '%s\n' 'Unified dubbing service secret is configured (value hidden).'
