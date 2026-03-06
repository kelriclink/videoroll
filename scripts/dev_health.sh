#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-http://localhost:3000/api}"

curl -fsS "$BASE/health" >/dev/null && echo "orchestrator-api ok"
curl -fsS "$BASE/subtitle-service/health" >/dev/null && echo "subtitle-service ok"
curl -fsS "$BASE/youtube-ingest/health" >/dev/null && echo "youtube-ingest ok"
curl -fsS "$BASE/bilibili-publisher/health" >/dev/null && echo "bilibili-publisher ok"

if curl -fsS http://localhost:3000 >/dev/null; then
  echo "web ok"
else
  echo "web not running (start with ./scripts/dev_web.sh)"
fi
