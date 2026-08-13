#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$ROOT_DIR/extensions/videoroll-youtube-submit"
OUTPUT_FILE="${1:-$ROOT_DIR/dist/videoroll-youtube-submit.zip}"
TEMP_DIR="$(mktemp -d)"
TEMP_ARCHIVE="$TEMP_DIR/videoroll-youtube-submit.zip"

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

if ! command -v zip >/dev/null 2>&1; then
  echo "zip command is required" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_FILE")"
(
  cd "$SOURCE_DIR"
  zip -q "$TEMP_ARCHIVE" \
    manifest.json \
    background.js \
    content.js \
    shared.js \
    options.html \
    options.css \
    options.js \
    README.md
)
mv "$TEMP_ARCHIVE" "$OUTPUT_FILE"

echo "Browser extension package: $OUTPUT_FILE"
if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$OUTPUT_FILE"
fi
