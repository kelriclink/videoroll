#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/web"

if [[ ! -d node_modules ]]; then
  npm install --no-fund --no-audit
fi

npm run dev -- --host 0.0.0.0 --port 3000

