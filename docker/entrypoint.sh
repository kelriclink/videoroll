#!/usr/bin/env bash
set -euo pipefail

role="${DEPLOYMENT_ROLE:-application}"
python -m videoroll.deployment --role "$role"

if [[ "${1:-}" == "outbox-dispatcher" ]]; then
  # Celery Beat owns periodic queue ticks and the durable-outbox wake-up.  It
  # is intentionally a distinct PID 1 from every worker and HTTP server.
  touch /tmp/outbox-dispatcher-ready
  exec celery -A videoroll.apps.subtitle_service.worker:celery_app beat \
    -l INFO --schedule /tmp/celerybeat-schedule
fi

exec "$@"
