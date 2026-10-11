#!/usr/bin/env bash
set -euo pipefail

ROOT=/opt/videoroll
ARTIFACT_ROOT="${VIDEOROLL_SYSTEM_BINARIES_ARTIFACT_DIR:-}"
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run as root to provision self-contained nginx and Redis." >&2
  exit 1
fi
if systemctl is-active --quiet videoroll-web.service || systemctl is-active --quiet videoroll-redis.service; then
  echo "Stop native nginx/Redis units before replacing binaries" >&2
  exit 1
fi
scratch=""
cleanup() { if [[ -n "$scratch" ]]; then rm -rf "$scratch"; fi; }
trap cleanup EXIT
if [[ -n "$ARTIFACT_ROOT" ]]; then
  tree="$ARTIFACT_ROOT"
  if [[ -d "$tree/extracted" ]]; then tree="$tree/extracted"; fi
else
  command -v dpkg-deb >/dev/null || { echo "Debian packages must be extracted on build host" >&2; exit 1; }
  scratch="$(mktemp -d)"
  mkdir -p "$scratch/packages" "$scratch/extracted"
  (cd "$scratch/packages"; apt-get download nginx nginx-common redis-server redis-tools liblzf1)
  for deb in "$scratch"/packages/*.deb; do dpkg-deb -x "$deb" "$scratch/extracted"; done
  tree="$scratch/extracted"
fi

nginx_bin="$tree/usr/sbin/nginx"
redis_bin="$tree/usr/bin/redis-server"
redis_cli="$tree/usr/bin/redis-cli"
mime="$tree/etc/nginx/mime.types"
lzf="$(find "$tree" -type f -name 'liblzf.so.1.*' -print -quit)"
for file in "$nginx_bin" "$redis_bin" "$redis_cli" "$mime" "$lzf"; do
  test -s "$file" || { echo "Missing bundled binary/resource $file" >&2; exit 1; }
done
install -d -m 0755 "$ROOT/bin" "$ROOT/lib" "$ROOT/share/nginx"
install -m 0755 "$nginx_bin" "$ROOT/bin/nginx"
install -m 0755 "$redis_bin" "$ROOT/bin/redis-server"
install -m 0755 "$redis_cli" "$ROOT/bin/redis-cli-real"
# CLI also links liblzf. Wrap it so operators need no global linker changes.
cat > "$ROOT/bin/redis-cli" <<'SH'
#!/usr/bin/env bash
export LD_LIBRARY_PATH="/opt/videoroll/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec /opt/videoroll/bin/redis-cli-real "$@"
SH
chmod 0755 "$ROOT/bin/redis-cli"
install -m 0644 "$mime" "$ROOT/share/nginx/mime.types"
install -m 0644 "$lzf" "$ROOT/lib/liblzf.so.1.5"
ln -sfn liblzf.so.1.5 "$ROOT/lib/liblzf.so.1"

if ldd "$ROOT/bin/nginx" | grep -q 'not found'; then
  echo "Nginx has missing system-library dependencies" >&2
  exit 1
fi
if LD_LIBRARY_PATH="$ROOT/lib" ldd "$ROOT/bin/redis-server" | grep -q 'not found'; then
  echo "Redis has missing system-library dependencies" >&2
  exit 1
fi
"$ROOT/bin/nginx" -v
LD_LIBRARY_PATH="$ROOT/lib" "$ROOT/bin/redis-server" --version
echo "Self-contained Nginx/Redis runtime installed under $ROOT."
