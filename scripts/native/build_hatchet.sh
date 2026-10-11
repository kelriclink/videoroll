#!/usr/bin/env bash
set -euo pipefail

# Use the vendored, reproducible Hatchet build (OpenAPI + Protobuf generation).
# Docker is only a BUILD TOOL; the resulting binaries run under systemd.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT_DIR/services/hatchet"
VERSION="${HATCHET_VERSION:-v0.107.0}"
OUTPUT_DIR=""
if [[ "${1:-}" == "--output-dir" ]]; then
  OUTPUT_DIR="${2:?--output-dir requires a path}"
  shift 2
fi
if [[ $# -ne 0 ]]; then
  echo "usage: $0 [--output-dir DIR]" >&2
  exit 64
fi
if [[ -z "$OUTPUT_DIR" && "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root to install, or supply --output-dir" >&2
  exit 1
fi
if [[ -z "$OUTPUT_DIR" ]] && systemctl is-active --quiet videoroll-hatchet.service; then
  echo "Stop Hatchet before installing its binaries." >&2
  exit 1
fi
# For a machine without Docker, transfer native binaries exported from CI
# and set HATCHET_ARTIFACT_DIR to the extracted artifact directory.

TMP="$(mktemp -d)"
CID=""
cleanup() {
  if [[ -n "$CID" ]]; then docker rm -f "$CID" >/dev/null 2>&1 || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT
mkdir -p "$TMP/bin" "$TMP/static-assets"

if [[ -n "${HATCHET_ARTIFACT_DIR:-}" ]]; then
  for binary in hatchet-lite hatchet-admin hatchet-migrate; do
    test -s "$HATCHET_ARTIFACT_DIR/bin/$binary" || {
      echo "missing artifact: $binary" >&2
      exit 1
    }
    install -m 0755 "$HATCHET_ARTIFACT_DIR/bin/$binary" "$TMP/bin/$binary"
  done
  test -s "$HATCHET_ARTIFACT_DIR/static-assets/index.html"
  cp -a "$HATCHET_ARTIFACT_DIR/static-assets/." "$TMP/static-assets/"
else
  command -v docker >/dev/null || {
    echo "Docker is needed only on a build host; set HATCHET_ARTIFACT_DIR to install prebuilt artifacts." >&2
    exit 1
  }
  IMAGE="${HATCHET_BUILD_IMAGE:-videoroll-native-hatchet-build:$VERSION}"
  if [[ -z "${HATCHET_BUILD_IMAGE:-}" ]]; then
    docker build --file "$SRC/Dockerfile.videoroll" --build-arg "VERSION=$VERSION" \
      --tag "$IMAGE" "$SRC"
  fi
  CID="$(docker create "$IMAGE")"
  for binary in hatchet-lite hatchet-admin hatchet-migrate; do
    docker cp "$CID:/$binary" "$TMP/bin/$binary"
    test -s "$TMP/bin/$binary"
    chmod 0755 "$TMP/bin/$binary"
  done
  docker cp "$CID:/static-assets/." "$TMP/static-assets/"
fi
test -s "$TMP/static-assets/index.html"

if [[ -n "$OUTPUT_DIR" ]]; then
  install -d -m 0755 "$OUTPUT_DIR/bin" "$OUTPUT_DIR/static-assets"
  install -m 0755 "$TMP/bin/"hatchet-* "$OUTPUT_DIR/bin/"
  cp -a "$TMP/static-assets/." "$OUTPUT_DIR/static-assets/"
  echo "Hatchet native artifacts exported to $OUTPUT_DIR"
  exit 0
fi

install -d -m 0755 /opt/videoroll/share/hatchet /opt/videoroll/bin
for binary in hatchet-lite hatchet-admin hatchet-migrate; do
  install -m 0755 "$TMP/bin/$binary" "/opt/videoroll/bin/$binary"
done
# Atomic directory replacement would be ideal; keep a versioned directory so
# existing in-use assets aren't partially overwritten during a failed copy.
ASSET_DIR="/opt/videoroll/share/hatchet/static-assets"
STAGED="/opt/videoroll/share/hatchet/.static-assets-next"
rm -rf "$STAGED"
mkdir -p "$STAGED"
cp -a "$TMP/static-assets/." "$STAGED/"
rm -rf "$ASSET_DIR"
mv "$STAGED" "$ASSET_DIR"
echo "Installed native Hatchet $VERSION (Docker not needed at runtime)"
