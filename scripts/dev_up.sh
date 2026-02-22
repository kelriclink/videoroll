#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  cp .env.example "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example"
fi

docker compose --env-file "$ENV_FILE" up --build -d

echo ""
echo "Web UI:              http://localhost:3000"
echo "API (monolith):      http://localhost:8000/docs"
echo "Subtitle Service:    http://localhost:8000/subtitle-service/docs"
echo "YouTube Ingest:      http://localhost:8000/youtube-ingest/docs"
echo "Bilibili Publisher:  http://localhost:8000/bilibili-publisher/docs"
echo "MinIO Console:       http://localhost:9001 (user/pass from .env)"
echo ""
docker compose --env-file "$ENV_FILE" ps
