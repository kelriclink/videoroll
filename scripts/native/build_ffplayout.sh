#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT_DIR/services/ffplayout"
OUTPUT_DIR=""
ARTIFACT_DIR="${FFPLAYOUT_ARTIFACT_DIR:-}"
case "${1:-}" in
  --output-dir) OUTPUT_DIR="${2:?output directory required}" ;;
  --artifact-dir) ARTIFACT_DIR="${2:?artifact directory required}" ;;
  "") ;;
  *) echo "usage: $0 [--output-dir DIR | --artifact-dir DIR]" >&2; exit 64 ;;
esac
if [[ -n "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$(realpath -m "$OUTPUT_DIR")"
fi
if [[ -n "$ARTIFACT_DIR" ]]; then
  ARTIFACT_DIR="$(realpath "$ARTIFACT_DIR")"
fi
if [[ -z "$OUTPUT_DIR" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Use --output-dir on a build host, or sudo to install artifacts." >&2
  exit 1
fi
if [[ -z "$OUTPUT_DIR" ]] && systemctl is-active --quiet videoroll-ffplayout.service; then
  echo "Stop ffplayout before replacing its binary." >&2
  exit 1
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ -z "$ARTIFACT_DIR" ]]; then
command -v cargo >/dev/null || { echo "cargo is required" >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is required" >&2; exit 1; }
node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "$node_major" != "24" ]]; then
  echo "ffplayout build requires Node.js 24; found Node.js $node_major" >&2
  exit 1
fi

mkdir -p "$TMP/source" "$TMP/artifacts/bin" "$TMP/artifacts/assets"
cp -a "$SRC/." "$TMP/source/"
cd "$TMP/source"
npm ci --no-fund --no-audit
npm pkg set 'scripts.build-only=vite build --base=/playout/'
cargo build --locked --release --package ffplayout --no-default-features --features embed_frontend
install -m 0755 target/release/ffplayout "$TMP/artifacts/bin/ffplayout"
install -m 0644 assets/dummy.vtt assets/logo.png "$TMP/artifacts/assets/"
ARTIFACT_DIR="$TMP/artifacts"
fi

test -s "$ARTIFACT_DIR/bin/ffplayout"
test -s "$ARTIFACT_DIR/assets/dummy.vtt"
test -s "$ARTIFACT_DIR/assets/logo.png"
if [[ -n "$OUTPUT_DIR" ]]; then
  install -d "$OUTPUT_DIR/bin" "$OUTPUT_DIR/assets"
  install -m 0755 "$ARTIFACT_DIR/bin/ffplayout" "$OUTPUT_DIR/bin/ffplayout"
  install -m 0644 "$ARTIFACT_DIR/assets/"{dummy.vtt,logo.png} "$OUTPUT_DIR/assets/"
  echo "Native ffplayout artifacts exported to $OUTPUT_DIR"
  exit 0
fi
install -m 0755 "$ARTIFACT_DIR/bin/ffplayout" /usr/local/bin/ffplayout
install -d -m 0755 /usr/share/ffplayout
install -m 0644 "$ARTIFACT_DIR/assets/"{dummy.vtt,logo.png} /usr/share/ffplayout/

echo "Installed /usr/local/bin/ffplayout"
