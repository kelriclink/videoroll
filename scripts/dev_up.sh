#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f social-auto-upload/sau_cli.py ]]; then
  echo "social-auto-upload submodule is missing; run: git submodule update --init --recursive" >&2
  exit 1
fi

ENV_FILE="${ENV_FILE:-.env}"
if [[ ! -f "$ENV_FILE" ]]; then
  cp .env.example "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example"
fi

docker compose -f docker-compose.yml --env-file "$ENV_FILE" up --build -d

echo ""
echo "Web UI:              http://localhost:3000"
echo "API (monolith):      http://localhost:3000/api/docs"
echo "Subtitle Service:    http://localhost:3000/api/subtitle-service/docs"
echo "YouTube Ingest:      http://localhost:3000/api/youtube-ingest/docs"
echo "Bilibili Publisher:  http://localhost:3000/api/bilibili-publisher/docs"
echo "Social Publisher:     internal Docker service (managed from 投稿设置)"
echo "MinIO Console:       http://localhost:9001 (user/pass from .env)"
echo ""
docker compose -f docker-compose.yml --env-file "$ENV_FILE" ps
