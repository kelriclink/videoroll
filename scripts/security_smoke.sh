#!/usr/bin/env bash
set -euo pipefail

# Offline rollout gate.  It deliberately does not start Compose or contact an
# external service, so it is safe to run before and after a deployment.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  python_bin="$PYTHON_BIN"
elif command -v python >/dev/null 2>&1; then
  python_bin=python
elif command -v python3 >/dev/null 2>&1; then
  python_bin=python3
else
  echo "Python 3.12+ is required to run the security smoke test." >&2
  exit 127
fi

exec "$python_bin" -m pytest -q tests/test_security_rollout.py
