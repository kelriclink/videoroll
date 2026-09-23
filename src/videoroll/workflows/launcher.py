from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.config import OrchestratorSettings
from videoroll.db.models import PipelineRun, SubtitleJob, Task
from videoroll.db.session import get_sessionmaker


AUTO_YOUTUBE_WORKFLOW_NAME = "VideoPipelineV1"
SUBTITLE_JOB_WORKFLOW_NAME = "SubtitleJobV1"


@dataclass(frozen=True)
class PipelineLaunchResult:
    pipeline_run_id: uuid.UUID
    workflow_name: str
    external_run_id: str


class PipelineLauncher:
    """Submit VideoRoll pipelines to the dedicated Hatchet workflow service."""

    def __init__(
        self,
        settings: OrchestratorSettings,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory or get_sessionmaker(settings.database_url)

    def launch_auto_youtube(self, task_id: uuid.UUID) -> PipelineLaunchResult:
        db = self.session_factory()
        run_id = uuid.uuid4()
        try:
            task = db.get(Task, task_id)
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            priority = int(task.priority or 0)

            run = PipelineRun(
                id=run_id,
                task_id=task_id,
                engine="hatchet",
                workflow_name=AUTO_YOUTUBE_WORKFLOW_NAME,
                state="submitting",
                request_json={"task_id": str(task_id), "runtime_profile": True, "priority": priority},
            )
            db.add(run)
            db.commit()

            try:
                with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                    response = client.post(
                        f"{self.settings.workflow_service_url.rstrip('/')}/runs/auto-youtube",
                        json={"task_id": str(task_id), "priority": priority},
                    )
                    response.raise_for_status()
                    payload = response.json()
                external_run_id = str(payload.get("workflow_run_id") or "").strip()
                if not external_run_id:
                    raise RuntimeError("workflow service did not return workflow_run_id")
            except Exception as exc:
                db.rollback()
                failed = db.get(PipelineRun, run_id)
                if failed is not None:
                    failed.state = "launch_failed"
                    failed.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                    failed.finished_at = datetime.now(timezone.utc)
                    db.add(failed)
                    db.commit()
                raise

            persisted = db.get(PipelineRun, run_id)
            if persisted is None:
                raise RuntimeError(f"pipeline run disappeared after launch: {run_id}")
            persisted.external_run_id = external_run_id
            persisted.state = "submitted"
            persisted.error_message = None
            db.add(persisted)
            db.commit()
            return PipelineLaunchResult(
                pipeline_run_id=run_id,
                workflow_name=AUTO_YOUTUBE_WORKFLOW_NAME,
                external_run_id=external_run_id,
            )
        finally:
            db.close()

    def launch_subtitle_job(self, job_id: uuid.UUID) -> PipelineLaunchResult:
        db = self.session_factory()
        run_id = uuid.uuid4()
        try:
            job = db.get(SubtitleJob, job_id)
            if job is None:
                raise ValueError(f"subtitle job not found: {job_id}")
            task = db.get(Task, job.task_id)
            if task is None:
                raise ValueError(f"task not found for subtitle job: {job.task_id}")
            priority = int(task.priority or 0)
            run = PipelineRun(
                id=run_id,
                task_id=job.task_id,
                engine="hatchet",
                workflow_name=SUBTITLE_JOB_WORKFLOW_NAME,
                state="submitting",
                request_json={"job_id": str(job_id), "priority": priority},
            )
            db.add(run)
            db.commit()
            try:
                with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                    response = client.post(
                        f"{self.settings.workflow_service_url.rstrip('/')}/runs/subtitle-job",
                        json={"job_id": str(job_id), "priority": priority},
                    )
                    response.raise_for_status()
                    payload = response.json()
                external_run_id = str(payload.get("workflow_run_id") or "").strip()
                if not external_run_id:
                    raise RuntimeError("workflow service did not return workflow_run_id")
            except Exception as exc:
                db.rollback()
                failed = db.get(PipelineRun, run_id)
                if failed is not None:
                    failed.state = "launch_failed"
                    failed.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                    failed.finished_at = datetime.now(timezone.utc)
                    db.add(failed)
                    db.commit()
                raise
            persisted = db.get(PipelineRun, run_id)
            if persisted is None:
                raise RuntimeError(f"pipeline run disappeared after launch: {run_id}")
            persisted.external_run_id = external_run_id
            persisted.state = "submitted"
            persisted.error_message = None
            db.add(persisted)
            db.commit()
            return PipelineLaunchResult(
                pipeline_run_id=run_id,
                workflow_name=SUBTITLE_JOB_WORKFLOW_NAME,
                external_run_id=external_run_id,
            )
        finally:
            db.close()

