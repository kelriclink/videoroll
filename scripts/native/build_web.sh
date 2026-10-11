#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WEB_DIR="$ROOT_DIR/src/web"
DEST="${VIDEOROLL_WEB_ROOT:-$ROOT_DIR/data/web}"
ARTIFACT_DIR=""
case "${1:-}" in
  --artifact-dir) ARTIFACT_DIR="${2:?artifact directory required}" ;;
  --output-dir) DEST="${2:?output directory required}" ;;
  "") ;;
  *) echo "usage: $0 [--artifact-dir DIST | --output-dir DIR]" >&2; exit 64 ;;
esac
DEST="$(realpath -m -s "$DEST")"
if [[ -n "$ARTIFACT_DIR" ]]; then
  ARTIFACT_DIR="$(realpath "$ARTIFACT_DIR")"
fi

if [[ -z "$ARTIFACT_DIR" ]]; then
command -v node >/dev/null || { echo "Node.js 22 is required" >&2; exit 1; }
command -v npm >/dev/null || { echo "npm is required" >&2; exit 1; }

node_major="$(node -p 'process.versions.node.split(".")[0]')"
if [[ "$node_major" != "22" ]]; then
  echo "VideoRoll Web build requires Node.js 22; found Node.js $node_major" >&2
  exit 1
fi

cd "$WEB_DIR"
npm ci --no-fund --no-audit
npm run build
  ARTIFACT_DIR="$WEB_DIR/dist"
fi

test -s "$ARTIFACT_DIR/index.html"
# Never overwrite live assets. Install a versioned directory, then switch a link.
mkdir -p "$(dirname "$DEST")"
STAGED="$(mktemp -d "${DEST}.release-XXXXXX")"
cp -a "$ARTIFACT_DIR/." "$STAGED/"
find "$STAGED" -type d -exec chmod 0755 {} +
find "$STAGED" -type f -exec chmod 0644 {} +
if [[ -d "$DEST" && ! -L "$DEST" ]]; then
  if [[ -z "$(ls -A "$DEST")" ]]; then
    rmdir "$DEST"
  else
    echo "Existing Web files retained. Prepared artifacts: $STAGED" >&2
    echo "Use videorollctl update --bundle for application upgrades." >&2
    exit 1
  fi
fi
ln -s "$STAGED" "${DEST}.next-$$"
mv -Tf "${DEST}.next-$$" "$DEST"

echo "Installed VideoRoll Web to $DEST"
