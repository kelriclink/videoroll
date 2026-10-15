#!/usr/bin/env python3
"""Read-only deployment guard. Busy/queued work blocks a native code switch."""
from __future__ import annotations

import os
import sys

# Cast enums to text: unknown schema/connection errors must fail closed.
CHECKS = {
    "subtitle_jobs": "status::text IN ('queued', 'running')",
    "render_jobs": "status::text IN ('queued', 'running')",
    "render_executions": "state IN ('claimed', 'running')",
    "publish_jobs": "state::text = 'submitting' OR upload_active",
    "pipeline_runs": "state IN ('submitting', 'submitted', 'running')",
    # Future pending cleanup events are scheduled, not active work.
    # Processing events and due pending events still block code switches.
    "outbox_events": "status = 'processing' OR (status = 'pending' AND available_at <= now())",
    "operation_inbox": "status IN ('pending', 'running', 'processing')",
}


def busy_jobs(connection) -> dict[str, int]:
    counts = {}
    with connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '5s'")
        for table, predicate in CHECKS.items():
            cursor.execute(f"SELECT count(*) FROM {table} WHERE {predicate}")
            count = cursor.fetchone()[0]
            if count:
                counts[table] = count
    return counts


def main() -> int:
    import psycopg

    dsn = os.environ["DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            counts = busy_jobs(connection)
    except Exception:
        # Driver exceptions can contain connection details. Do not expose them.
        print("Cannot verify database task state; deployment refused.", file=sys.stderr)
        return 1
    if counts:
        print(f"Deployment refused: unfinished work {counts}. Pause schedules and finish/cancel work first.", file=sys.stderr)
        return 1
    print("No queued/running work found; no database rows changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
