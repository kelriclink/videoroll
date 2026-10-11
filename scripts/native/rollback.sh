#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" != "--execute" ]]; then
  echo "refusing to change running services without --execute" >&2
  echo "usage: sudo $0 --execute" >&2
  exit 64
fi
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "run as root: sudo $0 --execute" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

systemctl disable --now videoroll.target videoroll-ffplayout.service || true
# Legacy companions (if present) are optional; no native component requires them.
if [[ "${VIDEOROLL_STOP_LEGACY_COMPANIONS:-0}" == "1" ]]; then
  bash "$ROOT_DIR/scripts/native/companions.sh" down || true
fi
"$ROOT_DIR/scripts/prod_compose.sh" up -d --no-build --remove-orphans
rm -f "${VIDEOROLL_DATA_ROOT:-/opt/videoroll/data}/native-cutover-complete"

echo "Compose stack restored. Database migrations are not automatically downgraded."
