#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../../src/web"
# Dependencies must already be installed from the lockfile. Never overwrite
# the running Python release or install anything into the production path.
test -x node_modules/.bin/vite
export VITE_RUST_NATIVE_DEPLOYMENT=true VITE_RUST_V2_UI=false
export VITE_RUST_V2_FULL_UI=false VITE_ORCHESTRATOR_URL=/api
exec npm run build
