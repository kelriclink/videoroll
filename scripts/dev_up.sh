#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ ! -f social-auto-upload/sau_cli.py ]]; then
  echo "social-auto-upload submodule is missing; run: git submodule update --init --recursive" >&2
  exit 1
fi

ENV_FILE="${ENV_FILE:-.env}"

random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    sed -i "s|^${key}=.*$|${key}=${value}|" "$ENV_FILE"
  else
    printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
  fi
}

if [[ ! -f "$ENV_FILE" ]]; then
  umask 077
  cp .env.example "$ENV_FILE"
  set_env_value DEVELOPMENT_MODE true
  set_env_value S3_ACCESS_KEY_ID "dev-$(random_secret)"
  set_env_value S3_SECRET_ACCESS_KEY "$(random_secret)"
  set_env_value INTERNAL_API_SECRET "$(random_secret)"
  set_env_value ADMIN_BOOTSTRAP_SECRET "$(random_secret)"
  set_env_value APP_UID "$(id -u)"
  set_env_value APP_GID "$(id -g)"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE with unique local development secrets"
fi

install -d -m 0700 data/secrets data/social-publisher data/work data/models data/minio data/redis

docker compose -f docker-compose.yml --env-file "$ENV_FILE" up --build -d

echo ""
echo "Web UI: http://localhost:${WEB_PORT:-3000}"
echo "API:    http://localhost:${WEB_PORT:-3000}/api/docs"
echo ""
docker compose -f docker-compose.yml --env-file "$ENV_FILE" ps
