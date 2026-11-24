#!/usr/bin/env bash
set -euo pipefail
# Assemble only; no systemd installation, database changes or service start.
if (( $# != 4 )); then
  echo 'usage: stage-release.sh NEW_ABSOLUTE_DIRECTORY CARGO_BIN_DIRECTORY NGINX_BINARY MIME_TYPES' >&2
  exit 2
fi
out=$1
bins=$2
nginx=$3
mime=$4
repo=$(cd "$(dirname "$0")/../.." && pwd)
[[ "$out" == /* && ! -e "$out" && ! -L "$out" ]]
for bin in videoroll-v2 videoroll-egress videoroll-sau; do test -x "$bins/$bin"; done
test -x "$nginx"
test -f "$mime"
test -f "$repo/src/web/dist/index.html"
mkdir -m 0755 -- "$out"
mkdir -p "$out/bin" "$out/share/nginx" "$out/deploy/rust-native" "$out/scripts/native" "$out/vendor"
for bin in videoroll-v2 videoroll-egress videoroll-sau; do
  install -m 0755 "$bins/$bin" "$out/bin/$bin"
done
install -m 0755 "$nginx" "$out/bin/nginx"
install -m 0644 "$mime" "$out/share/nginx/mime.types"
cp -a "$repo/src/web/dist" "$out/web"
cp -a "$repo/deploy/rust-native/." "$out/deploy/rust-native/"
install -m 0755 "$repo/scripts/native/local_inference.py" "$out/scripts/native/local_inference.py"
install -m 0755 "$repo/scripts/native/youdub_bridge.py" "$out/scripts/native/youdub_bridge.py"
cp -a "$repo/vendor/youdub_webui" "$out/vendor/"
git -C "$repo" rev-parse HEAD > "$out/SOURCE_REVISION"
(cd "$out"; find bin web deploy scripts vendor share -type f -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo "staged release: $out (not installed or started)"
