#!/usr/bin/env bash
set -euo pipefail

role="${1:?usage: videoroll-run-role ROLE}"

CORE_VENV="${VIDEOROLL_CORE_VENV:-/opt/videoroll/venvs/core}"
SUBTITLE_VENV="${VIDEOROLL_SUBTITLE_VENV:-/opt/videoroll/venvs/subtitle}"
WORKFLOW_VENV="${VIDEOROLL_WORKFLOW_VENV:-/opt/videoroll/venvs/workflow}"
SUBTITLE_WORKFLOW_VENV="${VIDEOROLL_SUBTITLE_WORKFLOW_VENV:-/opt/videoroll/venvs/subtitle-workflow}"

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

preflight() {
  local python="$1"
  local deployment_role="$2"
  DEPLOYMENT_ROLE="$deployment_role" "$python" -m videoroll.deployment
}

case "$role" in
  orchestrator)
    export DEPLOYMENT_ROLE=orchestrator
    preflight "$CORE_VENV/bin/python" orchestrator
    exec "$CORE_VENV/bin/uvicorn" videoroll.apps.orchestrator_api.main:app --host 127.0.0.1 --port 8000 --root-path /api
    ;;
  egress-gateway)
    export DEPLOYMENT_ROLE=egress-gateway
    exec "$CORE_VENV/bin/uvicorn" videoroll.apps.egress_gateway.main:app --host 127.0.0.1 --port 8020
    ;;
  subtitle-service)
    export DEPLOYMENT_ROLE=subtitle-api
    preflight "$SUBTITLE_VENV/bin/python" subtitle-api
    exec "$SUBTITLE_VENV/bin/uvicorn" videoroll.apps.subtitle_service.main:app --host 127.0.0.1 --port 8001
    ;;
  youtube-ingest)
    export DEPLOYMENT_ROLE=youtube-ingest-api
    preflight "$CORE_VENV/bin/python" youtube-ingest-api
    exec "$CORE_VENV/bin/uvicorn" videoroll.apps.youtube_ingest.main:app --host 127.0.0.1 --port 8002
    ;;
  bilibili-publisher)
    export DEPLOYMENT_ROLE=bilibili-publisher-api
    preflight "$CORE_VENV/bin/python" bilibili-publisher-api
    exec "$CORE_VENV/bin/uvicorn" videoroll.apps.bilibili_publisher.main:app --host 127.0.0.1 --port 8003
    ;;
  workflow-api)
    export DEPLOYMENT_ROLE=workflow-api
    exec "$WORKFLOW_VENV/bin/uvicorn" videoroll.workflows.api:app --host 127.0.0.1 --port 8030
    ;;
  workflow-worker)
    export DEPLOYMENT_ROLE=workflow-worker
    exec "$WORKFLOW_VENV/bin/python" -m videoroll.workflows.worker
    ;;
  subtitle-workflow-worker)
    export DEPLOYMENT_ROLE=subtitle-workflow-worker
    preflight "$SUBTITLE_WORKFLOW_VENV/bin/python" subtitle-workflow-worker
    exec "$SUBTITLE_WORKFLOW_VENV/bin/python" -m videoroll.workflows.subtitle_worker
    ;;
  subtitle-control-worker)
    export DEPLOYMENT_ROLE=subtitle-control-worker
    preflight "$SUBTITLE_VENV/bin/python" subtitle-control-worker
    exec "$SUBTITLE_VENV/bin/celery" -A videoroll.apps.subtitle_service.worker:celery_app worker -Q subtitle-control -l INFO --concurrency 1
    ;;
  publish-worker)
    export DEPLOYMENT_ROLE=publish-worker
    preflight "$CORE_VENV/bin/python" publish-worker
    exec "$CORE_VENV/bin/celery" -A videoroll.apps.bilibili_publisher.worker:celery_app worker -Q publish -l INFO --concurrency "${CELERY_PUB_CONCURRENCY:-1}"
    ;;
  outbox-dispatcher)
    export DEPLOYMENT_ROLE=outbox-dispatcher
    preflight "$CORE_VENV/bin/python" outbox-dispatcher
    exec "$CORE_VENV/bin/celery" -A videoroll.apps.subtitle_service.worker:celery_app beat -l INFO --schedule /opt/videoroll/current/data/state/celerybeat-schedule
    ;;
  render-worker)
    export DEPLOYMENT_ROLE=render-worker
    exec "$SUBTITLE_VENV/bin/python" -m videoroll.apps.render_worker.runtime
    ;;
  *)
    echo "unknown VideoRoll native role: $role" >&2
    exit 64
    ;;
esac
