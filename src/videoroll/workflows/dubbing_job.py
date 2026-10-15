"""Independent Hatchet pool for Chinese dubbing jobs."""
from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from hatchet_sdk import TTLBasedIdempotencyConfig
from pydantic import BaseModel


DUBBING_JOB_TASK_NAME = "DubbingJobV1"


class DubbingJobWorkflowInput(BaseModel):
    job_id: str
    dispatch_key: str


def _run_dubbing_job(job_id: str) -> dict[str, str]:
    from videoroll.apps.dubbing.job import execute_dubbing_job
    from videoroll.config import get_subtitle_settings
    from videoroll.db.migrate import initialize_database
    from videoroll.db.session import get_sessionmaker
    from videoroll.storage.filesystem import FileStore

    settings = get_subtitle_settings()
    initialize_database(settings.database_url)
    factory = get_sessionmaker(settings.database_url)
    return execute_dubbing_job(
        uuid.UUID(job_id), session_factory=factory,
        store=FileStore(settings), work_dir=Path(settings.work_dir),
    )


def push_dubbing_finished(job_id: str, status: str, error: str | None = None) -> None:
    # Hatchet durable event: the parent VideoPipelineV1 can display an
    # explicit dubbing-wait stage without occupying a core worker slot.
    import httpx
    from videoroll.config import get_orchestrator_settings
    from videoroll.apps.security.service_auth import INTERNAL_TOKEN_HEADER, service_token
    settings = get_orchestrator_settings()
    payload = {"dubbing_job_id": job_id, "status": status, "error": error}
    with httpx.Client(timeout=20, headers={INTERNAL_TOKEN_HEADER: service_token(settings)}) as client:
        response = client.post(
            f"{settings.workflow_service_url.rstrip('/')}/events/dubbing-finished",
            json=payload,
        )
        response.raise_for_status()


def close_dubbing_pipeline_audit(job_id: str, *, status: str, error: str | None = None) -> None:
    from datetime import datetime, timezone
    from videoroll.config import get_subtitle_settings
    from videoroll.db.models import DubbingJob, PipelineRun
    from videoroll.db.session import get_sessionmaker

    factory = get_sessionmaker(get_subtitle_settings().database_url)
    with factory() as db:
        job = db.get(DubbingJob, uuid.UUID(job_id))
        if job is None:
            return
        runs = db.query(PipelineRun).filter(
            PipelineRun.task_id == job.task_id,
            PipelineRun.workflow_name == DUBBING_JOB_TASK_NAME,
            PipelineRun.state.in_(("submitted", "running", "submitting")),
        ).all()
        for run in runs:
            if str((run.request_json or {}).get("job_id") or "") != job_id:
                continue
            run.state = status
            run.finished_at = datetime.now(timezone.utc)
            run.error_message = error if status == "failed" else None
            db.add(run)
        db.commit()


def register_dubbing_job_task(hatchet: Any) -> Any:
    try:
        from hatchet_sdk import DesiredWorkerLabel
        pool = [DesiredWorkerLabel(key="pool", value="dubbing", required=True)]
    except ImportError:
        pool = []
    @hatchet.task(
        name=DUBBING_JOB_TASK_NAME,
        input_validator=DubbingJobWorkflowInput,
        idempotency=TTLBasedIdempotencyConfig(
            key_expression="input.dispatch_key", ttl=timedelta(days=7),
        ),
        retries=1,
        backoff_factor=2.0,
        backoff_max_seconds=60,
        schedule_timeout=timedelta(days=7),
        execution_timeout=timedelta(hours=36),
        desired_worker_labels=pool,
    )
    def dubbing_job(input: DubbingJobWorkflowInput, ctx: Any) -> dict[str, str]:
        try:
            result = _run_dubbing_job(input.job_id)
        except Exception as exc:
            if int(getattr(ctx, "retry_count", 0) or 0) >= 1:
                message = f"{type(exc).__name__}: {exc}"[:800]
                push_dubbing_finished(input.job_id, "failed", message)
                try:
                    close_dubbing_pipeline_audit(input.job_id, status="failed", error=message)
                except Exception:
                    pass  # Parent Hatchet workflow still receives a failed event.
            raise
        result_status = str(result.get("status") or "succeeded")
        if result_status == "succeeded":
            if not result.get("video_key"):
                raise RuntimeError("YouDub completed without a registered video asset")
            push_dubbing_finished(input.job_id, "succeeded")
            try:
                close_dubbing_pipeline_audit(input.job_id, status="succeeded")
            except Exception:
                pass  # Event already released the parent workflow.
        elif result_status in ("paused", "canceled"):
            # A pause/cancel must fail the parent wait promptly, not strand P1.
            push_dubbing_finished(input.job_id, "failed", result_status)
            try:
                close_dubbing_pipeline_audit(input.job_id, status="failed", error=result_status)
            except Exception:
                pass
        return {"status": result_status, "video_key": str(result.get("video_key") or "")}
    return dubbing_job
