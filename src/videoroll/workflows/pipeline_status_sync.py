"""Authoritative Hatchet-to-VideoRoll audit synchronization.

The Task / PublishBatch business results are independent from the Hatchet
workflow execution. Never infer the parent workflow's completion from the task
status or trigger/cancel an external workflow during reconciliation.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import or_
from sqlalchemy.orm import Session

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.db.models import PipelineRun, SubtitleJob, SubtitleJobStatus, SourceType, Task, TaskStatus
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


def reconcile_stranded_auto_subtitle_jobs(settings, *, limit: int = 200) -> int:
    """Fail only never-started jobs owned by an already failed auto workflow.

    Never modify a leased job, a newer workflow attempt or a standalone
    subtitle workflow. Leave Task and downloaded assets in place for restart.
    """
    factory = get_sessionmaker(settings.database_url)
    changed = 0
    with factory() as db:
        candidates = (
            db.query(SubtitleJob)
            .join(Task, Task.id == SubtitleJob.task_id)
            .filter(
                Task.source_type == SourceType.youtube,
                SubtitleJob.status == SubtitleJobStatus.queued,
                SubtitleJob.progress == 0,
                SubtitleJob.lease_owner.is_(None),
                SubtitleJob.heartbeat_at.is_(None),
            )
            .order_by(SubtitleJob.created_at.desc())
            .limit(max(1, min(limit, 500)))
            .with_for_update(of=SubtitleJob, skip_locked=True)
            .all()
        )
        for job in candidates:
            parent = (
                db.query(PipelineRun)
                .filter(
                    PipelineRun.task_id == job.task_id,
                    PipelineRun.workflow_name == AUTO_WORKFLOW_NAME,
                )
                .order_by(PipelineRun.created_at.desc())
                .first()
            )
            if (
                parent is None or parent.state != "failed"
                or parent.finished_at is None
                or parent.created_at > job.created_at
                or parent.finished_at < job.created_at
            ):
                continue
            concurrent_run = (
                db.query(PipelineRun.id)
                .filter(
                    PipelineRun.task_id == job.task_id,
                    PipelineRun.state.in_(tuple(_ACTIVE)),
                )
                .first()
            )
            if concurrent_run is not None:
                continue
            job.status = SubtitleJobStatus.failed
            job.error_message = (
                "Parent VideoPipelineV1 failed before subtitle processing started; "
                "restart the automatic YouTube workflow using saved source media."
            )
            job.updated_at = datetime.now(timezone.utc)
            db.add(job)
            changed += 1
        if changed:
            db.commit()
    return changed

def recover_orphaned_youtube_download_pipelines(settings, *, limit: int = 3) -> dict[str, int]:
    """Recreate at most one failed parent for an unowned, interrupted download.

    The operation inbox marker is the durable once-only recovery claim. Existing
    Task IDs and download checkpoints are reused; published/cancelled/manual
    tasks and historical terminal download errors are never revived.
    """
    from videoroll.db.models import OperationInbox
    from videoroll.workflows.launcher import PipelineLauncher

    factory = get_sessionmaker(settings.database_url)
    recovered = 0
    errors = 0
    for _ in range(max(0, min(int(limit), 10))):
        with factory() as db:
            now = datetime.now(timezone.utc)
            candidates = (db.query(OperationInbox)
                .filter(OperationInbox.operation_key.like("youtube-download:%"),
                        OperationInbox.status == "processing",
                        OperationInbox.lease_until < now,
                        or_(OperationInbox.request_json["auto_workflow_recoveries"].as_integer().is_(None),
                            OperationInbox.request_json["auto_workflow_recoveries"].as_integer() < 1))
                .order_by(OperationInbox.created_at.asc())
                .limit(200).all())
            chosen = None
            for operation in candidates:
                info = operation.request_json if isinstance(operation.request_json, dict) else {}
                if int(info.get("auto_workflow_recoveries", 0)) >= 1:
                    continue
                try:
                    task_id = uuid.UUID(operation.operation_key.removeprefix("youtube-download:"))
                except ValueError:
                    continue
                task = db.get(Task, task_id)
                if task is None or task.status != TaskStatus.ingested or not str(task.created_by or "").startswith("auto_youtube;"):
                    continue
                parent = (db.query(PipelineRun).filter(
                    PipelineRun.task_id == task_id,
                    PipelineRun.workflow_name == AUTO_WORKFLOW_NAME)
                    .order_by(PipelineRun.created_at.desc()).first())
                if parent is None or parent.state != "failed":
                    continue
                row = (db.query(OperationInbox).filter(OperationInbox.id == operation.id)
                       .with_for_update(skip_locked=True).one_or_none())
                if row is None or row.status != "processing" or row.lease_until is None:
                    continue
                deadline = row.lease_until.replace(tzinfo=row.lease_until.tzinfo or timezone.utc)
                if deadline >= now:
                    continue
                claim = dict(row.request_json or {})
                if int(claim.get("auto_workflow_recoveries", 0)) >= 1:
                    continue
                claim["auto_workflow_recoveries"] = 1
                row.request_json = claim
                db.commit()
                chosen = task_id
                break
        if chosen is None:
            break
        try:
            PipelineLauncher(settings).launch_auto_youtube(chosen)
            recovered += 1
        except Exception:
            errors += 1
            logger.exception("failed to restart orphaned YouTube pipeline task=%s", chosen)
    return {"recovered": recovered, "errors": errors}


def recover_failed_auto_launches(settings, *, limit: int = 3) -> dict[str, int]:
    """Retry recent automatic launch failures using their original Hatchet dispatch key.

    An unavailable workflow-api must not indefinitely strand ingested scanner
    tasks. The retry is throttled, checked against current business state and
    bounded by Hatchet's seven-day idempotency TTL.
    """
    from videoroll.workflows.launcher import PipelineLauncher

    now = datetime.now(timezone.utc)
    factory = get_sessionmaker(settings.database_url)
    with factory() as db:
        rows = (
            db.query(PipelineRun)
            .join(Task, Task.id == PipelineRun.task_id)
            .filter(
                PipelineRun.engine == "hatchet",
                PipelineRun.workflow_name == AUTO_WORKFLOW_NAME,
                PipelineRun.state == "launch_failed",
                PipelineRun.external_run_id.is_(None),
                PipelineRun.created_at >= now - timedelta(days=6),
                PipelineRun.updated_at <= now - timedelta(seconds=90),
                Task.status == TaskStatus.ingested,
                Task.created_by.like("auto_youtube;%"),
            )
            .order_by(PipelineRun.updated_at.asc())
            .limit(max(1, min(int(limit), 10)))
            .all()
        )
        candidates = [(row.task_id, row.id, (row.request_json or {}).get("dispatch_key")) for row in rows]

    stats = {"checked": 0, "launched": 0, "errors": 0}
    launcher = PipelineLauncher(settings)
    for task_id, run_id, key in candidates:
        if not isinstance(key, str) or not key.startswith("auto-youtube:"):
            continue
        # A concurrent manual restart may already have launched this task.
        with factory() as db:
            active = db.query(PipelineRun.id).filter(
                PipelineRun.task_id == task_id,
                PipelineRun.id != run_id,
                PipelineRun.workflow_name == AUTO_WORKFLOW_NAME,
                PipelineRun.state.in_(("submitting", "submitted", "running")),
            ).first()
            task = db.get(Task, task_id)
            if active is not None or task is None or task.status != TaskStatus.ingested:
                continue
        stats["checked"] += 1
        try:
            launcher.launch_auto_youtube(task_id)
            stats["launched"] += 1
        except Exception as exc:
            stats["errors"] += 1
            logger.warning("failed to recover automatic Hatchet launch task=%s: %s", task_id, exc)
            if isinstance(exc, httpx.TransportError):
                break  # Don't flood an unavailable workflow API.
    return stats


def reconcile_failed_child_subtitle_jobs(settings, *, limit: int = 200) -> int:
    """Clear phantom queued jobs whose own Hatchet child run failed.

    Parent reconciliation deliberately excludes jobs created after parent
    failure. These later independent retries need separate synchronization.
    """
    factory = get_sessionmaker(settings.database_url)
    changed = 0
    with factory() as db:
        jobs = (
            db.query(SubtitleJob)
            .join(Task, Task.id == SubtitleJob.task_id)
            .filter(
                Task.status == TaskStatus.failed,
                SubtitleJob.status == SubtitleJobStatus.queued,
                SubtitleJob.progress == 0,
                SubtitleJob.lease_owner.is_(None),
                SubtitleJob.heartbeat_at.is_(None),
            )
            .order_by(SubtitleJob.created_at.asc())
            .limit(max(1, min(int(limit), 500)))
            .with_for_update(of=SubtitleJob, skip_locked=True)
            .all()
        )
        for job in jobs:
            runs = db.query(PipelineRun).filter(
                PipelineRun.task_id == job.task_id,
                PipelineRun.workflow_name == "SubtitleJobV1",
            ).order_by(PipelineRun.created_at.desc()).all()
            latest = next((r for r in runs if str((r.request_json or {}).get("job_id")) == str(job.id)), None)
            if latest is None or latest.state not in {"failed", "canceled"}:
                continue
            active = db.query(PipelineRun.id).filter(
                PipelineRun.task_id == job.task_id,
                PipelineRun.state.in_(tuple(_ACTIVE)),
            ).first()
            if active is not None:
                continue
            job.status = SubtitleJobStatus.failed
            job.error_message = (
                "Hatchet SubtitleJobV1 failed before this queued job started; "
                "retry from the task after resolving the original failure."
            )
            job.updated_at = datetime.now(timezone.utc)
            db.add(job)
            changed += 1
        if changed:
            db.commit()
    return changed


def reconcile_auto_pipeline_runs(settings, *, limit: int = 25) -> dict[str, int]:
    """Poll authoritative Hatchet state for all workflow types, repairing stale rows.

    The database transaction is *not* held while calling Hatchet. Status writes
    verify the same external run ID again under a row lock. Network failures
    leave the audit unchanged so a subsequent scan can retry safely.
    """
    factory = get_sessionmaker(settings.database_url)
    counts = {"checked": 0, "updated": 0, "errors": 0}
    with factory() as db:
        candidates = db.query(PipelineRun.id, PipelineRun.external_run_id).filter(
            PipelineRun.engine == "hatchet",
            # SubtitleJobV1 and other Hatchet child workflows must also be
            # reconciled: otherwise their submitted rows stay forever active
            # after the authoritative Hatchet run reached a terminal state.
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
    counts["stranded_jobs_failed"] = (reconcile_stranded_auto_subtitle_jobs(settings)
                                      + reconcile_failed_child_subtitle_jobs(settings))
    return counts
