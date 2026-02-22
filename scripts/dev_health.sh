#!/usr/bin/env bash
set -euo pipefail

curl -fsS http://localhost:8000/health >/dev/null && echo "orchestrator-api ok"
curl -fsS http://localhost:8000/subtitle-service/health >/dev/null && echo "subtitle-service ok"
curl -fsS http://localhost:8000/youtube-ingest/health >/dev/null && echo "youtube-ingest ok"
curl -fsS http://localhost:8000/bilibili-publisher/health >/dev/null && echo "bilibili-publisher ok"

if curl -fsS http://localhost:3000 >/dev/null; then
  echo "web ok"
else
  echo "web not running (start with ./scripts/dev_web.sh)"
fi
