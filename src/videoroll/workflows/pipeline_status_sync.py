"""Authoritative Hatchet-to-VideoRoll audit synchronization.

The Task / PublishBatch business results are independent from the Hatchet
workflow execution. Never infer the parent workflow's completion from the task
status or trigger/cancel an external workflow during reconciliation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.db.models import PipelineRun, Task
from videoroll.db.session import get_sessionmaker

logger = logging.getLogger(__name__)
AUTO_WORKFLOW_NAME = "VideoPipelineV1"
_ACTIVE = frozenset(("submitting", "submitted", "running", "cancel_requested"))
_TERMINAL = frozenset(("succeeded", "failed", "canceled", "launch_failed", "superseded"))
_HATCHET_TERMINAL = {
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "canceled",
    "CANCELED": "canceled",
    "EVICTED": "failed",
}


def _mark_run(db: Session, run: PipelineRun, *, state: str, error: str | None = None) -> bool:
    """State transition under caller's row lock; preserve already terminal states."""
    if run.state in _TERMINAL or run.state not in _ACTIVE:
        return False
    if state not in {"succeeded", "failed", "canceled", "running"}:
        return False
    if state == "running" and run.state not in {"submitted", "submitting"}:
        return False
    if run.state == state:
        return False
    run.state = state
    if state in _TERMINAL:
        run.finished_at = datetime.now(timezone.utc)
        run.error_message = error[:2000] if error and state == "failed" else None
    db.add(run)
    return True


def finish_auto_pipeline_audit(task_id: str, dispatch_key: str, *, succeeded: bool) -> bool:
    """Hatchet on-success/on-failure callback: update only the exact attempt."""
    from videoroll.config import get_subtitle_settings

    marker = str(dispatch_key or "").strip()
    if not marker:
        return False
    factory = get_sessionmaker(get_subtitle_settings().database_url)
    with factory() as db:
        candidates = db.query(PipelineRun).filter(
            PipelineRun.task_id == uuid.UUID(task_id),
            PipelineRun.engine == "hatchet",
            PipelineRun.workflow_name == AUTO_WORKFLOW_NAME,
        ).with_for_update().all()
        target = next((run for run in candidates
                       if (run.request_json or {}).get("dispatch_key") == marker), None)
        if target is None:
            logger.warning("Hatchet callback has no matching VideoPipeline attempt task=%s", task_id)
            return False
        changed = _mark_run(db, target, state="succeeded" if succeeded else "failed",
                            error=None if succeeded else "Hatchet workflow failed; inspect Hatchet task runs")
        if changed:
            db.commit()
        return changed


def reconcile_auto_pipeline_runs(settings, *, limit: int = 25) -> dict[str, int]:
    """Poll authoritative external run state; repair missed callbacks and stale rows.

    The database transaction is *not* held while calling Hatchet. Status writes
    verify the same external run ID again under a row lock. Network failures
    leave the audit unchanged so a subsequent scan can retry safely.
    """
    factory = get_sessionmaker(settings.database_url)
    counts = {"checked": 0, "updated": 0, "errors": 0}
    with factory() as db:
        candidates = db.query(PipelineRun.id, PipelineRun.external_run_id).filter(
            PipelineRun.engine == "hatchet",
            PipelineRun.workflow_name == AUTO_WORKFLOW_NAME,
            PipelineRun.state.in_(tuple(_ACTIVE)),
            PipelineRun.external_run_id.isnot(None),
        ).order_by(PipelineRun.created_at.asc()).limit(max(1, min(limit, 100))).all()
    with httpx.Client(timeout=8, headers=internal_http_headers(settings)) as client:
        for run_id, external_id in candidates:
            try:
                response = client.get(
                    f"{settings.workflow_service_url.rstrip('/')}/runs/{external_id}/status"
                )
                response.raise_for_status()
                reported = str(response.json()["status"]).strip().upper()
                mapped = _HATCHET_TERMINAL.get(reported)
                if mapped is None and reported == "RUNNING":
                    mapped = "running"
                counts["checked"] += 1
                if mapped is None:
                    continue
                with factory() as db:
                    row = db.query(PipelineRun).filter(PipelineRun.id == run_id).with_for_update().one_or_none()
                    if row is None or row.external_run_id != external_id:
                        continue
                    if _mark_run(db, row, state=mapped,
                                 error="Hatchet reports workflow FAILED" if mapped == "failed" else None):
                        db.commit()
                        counts["updated"] += 1
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                counts["errors"] += 1
                logger.warning("Hatchet workflow status reconciliation failed for %s: %s", run_id, exc)
    return counts
