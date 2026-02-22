#!/usr/bin/env bash
set -euo pipefail

UVICORN_APP="${UVICORN_APP:-videoroll.apps.monolith.main:app}"
UVICORN_HOST="${UVICORN_HOST:-0.0.0.0}"
UVICORN_PORT="${UVICORN_PORT:-8000}"

CELERY_SUB_APP="${CELERY_SUB_APP:-${CELERY_APP:-videoroll.apps.subtitle_service.worker:celery_app}}"
CELERY_SUB_QUEUE="${CELERY_SUB_QUEUE:-${CELERY_QUEUE:-subtitle}}"
CELERY_PUB_APP="${CELERY_PUB_APP:-videoroll.apps.bilibili_publisher.worker:celery_app}"
CELERY_PUB_QUEUE="${CELERY_PUB_QUEUE:-publish}"
CELERY_PUB_CONCURRENCY="${CELERY_PUB_CONCURRENCY:-1}"

echo "Starting celery worker (subtitle): $CELERY_SUB_APP queue=$CELERY_SUB_QUEUE"
celery -A "$CELERY_SUB_APP" worker -Q "$CELERY_SUB_QUEUE" -l INFO &
CELERY_SUB_PID=$!

echo "Starting celery worker (publish): $CELERY_PUB_APP queue=$CELERY_PUB_QUEUE concurrency=$CELERY_PUB_CONCURRENCY"
celery -A "$CELERY_PUB_APP" worker -Q "$CELERY_PUB_QUEUE" -l INFO --concurrency "$CELERY_PUB_CONCURRENCY" &
CELERY_PUB_PID=$!

echo "Starting uvicorn: $UVICORN_APP on $UVICORN_HOST:$UVICORN_PORT"
uvicorn "$UVICORN_APP" --host "$UVICORN_HOST" --port "$UVICORN_PORT" &
UVICORN_PID=$!

term_handler() {
  echo "Stopping..."
  kill -TERM "$UVICORN_PID" "$CELERY_SUB_PID" "$CELERY_PUB_PID" 2>/dev/null || true
  wait "$UVICORN_PID" "$CELERY_SUB_PID" "$CELERY_PUB_PID" 2>/dev/null || true
}

trap term_handler SIGINT SIGTERM

wait -n "$UVICORN_PID" "$CELERY_SUB_PID" "$CELERY_PUB_PID"
term_handler
