from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.config import OrchestratorSettings
from videoroll.db.models import DubbingJob, PipelineRun, SubtitleJob, Task
from videoroll.db.session import get_sessionmaker


AUTO_YOUTUBE_WORKFLOW_NAME = "VideoPipelineV1"
SUBTITLE_JOB_WORKFLOW_NAME = "SubtitleJobV1"
DUBBING_JOB_WORKFLOW_NAME = "DubbingJobV1"


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

    def launch_subtitle_job(self, job_id: uuid.UUID, *, dispatch_key: str | None = None) -> PipelineLaunchResult:
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
            request = {"job_id": str(job_id), "priority": priority}
            if dispatch_key:
                request["dispatch_key"] = dispatch_key
            run = PipelineRun(
                id=run_id,
                task_id=job.task_id,
                engine="hatchet",
                workflow_name=SUBTITLE_JOB_WORKFLOW_NAME,
                state="submitting",
                request_json=request,
            )
            db.add(run)
            db.commit()
            try:
                with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                    response = client.post(
                        f"{self.settings.workflow_service_url.rstrip('/')}/runs/subtitle-job",
                        json=request,
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

    def launch_dubbing_job(self, job_id: uuid.UUID, *, dispatch_key: str | None = None) -> PipelineLaunchResult:
        db = self.session_factory()
        run_id = uuid.uuid4()
        try:
            job = db.get(DubbingJob, job_id)
            if job is None:
                raise ValueError(f"dubbing job not found: {job_id}")
            task = db.get(Task, job.task_id)
            if task is None:
                raise ValueError(f"task not found for dubbing job: {job.task_id}")
            request = {
                "job_id": str(job_id), "priority": int(task.priority or 0),
                "dispatch_key": dispatch_key or f"{job_id}:{uuid.uuid4().hex}",
            }
            run = PipelineRun(
                id=run_id, task_id=job.task_id, engine="hatchet",
                workflow_name=DUBBING_JOB_WORKFLOW_NAME, state="submitting",
                request_json=request,
            )
            db.add(run)
            db.commit()
            try:
                with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                    response = client.post(
                        f"{self.settings.workflow_service_url.rstrip('/')}/runs/dubbing-job",
                        json=request,
                    )
                    response.raise_for_status()
                    external_run_id = str(response.json().get("workflow_run_id") or "").strip()
                if not external_run_id:
                    raise RuntimeError("workflow service returned no dubbing run id")
            except Exception as exc:
                db.rollback()
                failed = db.get(PipelineRun, run_id)
                if failed is not None:
                    failed.state = "launch_failed"
                    failed.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                    failed.finished_at = datetime.now(timezone.utc)
                    db.commit()
                raise
            persisted = db.get(PipelineRun, run_id)
            if persisted is None:
                raise RuntimeError("dubbing pipeline run vanished")
            persisted.external_run_id = external_run_id
            persisted.state = "submitted"
            db.commit()
            return PipelineLaunchResult(run_id, DUBBING_JOB_WORKFLOW_NAME, external_run_id)
        finally:
            db.close()

