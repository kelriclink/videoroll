#!/usr/bin/env bash
set -euo pipefail

# Offline rollout gate.  It deliberately does not start Compose or contact an
# external service, so it is safe to run before and after a deployment.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
exec "$PYTHON_BIN" -m pytest -q tests/test_security_rollout.py
