from __future__ import annotations

import uuid
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session, sessionmaker

from videoroll.apps.orchestrator_api.infrastructure.internal_http import internal_http_headers
from videoroll.config import OrchestratorSettings
from videoroll.db.models import DubbingJob, PipelineRun, SubtitleJob, Task, TaskStatus
from videoroll.apps.outbox.service import create_outbox_event
from videoroll.apps.subtitle_service.queues import SUBTITLE_CONTROL_QUEUE
from videoroll.db.session import get_sessionmaker


AUTO_YOUTUBE_WORKFLOW_NAME = "VideoPipelineV1"
SUBTITLE_JOB_WORKFLOW_NAME = "SubtitleJobV1"
DUBBING_JOB_WORKFLOW_NAME = "DubbingJobV1"


@dataclass(frozen=True)
class PipelineLaunchResult:
    pipeline_run_id: uuid.UUID
    workflow_name: str
    external_run_id: str


logger = logging.getLogger(__name__)


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

    @staticmethod
    def _finish_external_launch(db: Session, run_id: uuid.UUID, external_run_id: str) -> None:
        run = db.get(PipelineRun, run_id)
        if run is None:
            raise RuntimeError(f"pipeline run disappeared after launch: {run_id}")
        # Serialize against stop_task (which locks Task then PipelineRun).
        task = db.query(Task).filter(Task.id == run.task_id).with_for_update().one()
        db.refresh(run, with_for_update=True)
        run.external_run_id = external_run_id
        if run.state == "cancel_requested" or task.status == TaskStatus.canceled:
            run.state = "cancel_requested"
            create_outbox_event(
                db, event_type="workflow.cancel", aggregate_type="pipeline_run",
                aggregate_id=run.id, task_name="subtitle_service.cancel_workflow_run",
                args={"args": [str(run.id), external_run_id], "queue": SUBTITLE_CONTROL_QUEUE},
                operation_key=f"workflow-cancel:{run.id}",
            )
        elif run.state not in {"succeeded", "failed", "canceled", "superseded"}:
            # Callback can close a very fast run before launch response is saved.
            run.state = "submitted"
            run.error_message = None
        db.add(run)
        db.commit()

    @staticmethod
    def _mark_launch_failed(db: Session, run_id: uuid.UUID, error: Exception) -> None:
        db.rollback()
        run = db.get(PipelineRun, run_id)
        if run is None:
            return
        task = db.query(Task).filter(Task.id == run.task_id).with_for_update().one()
        db.refresh(run, with_for_update=True)
        if run.state != "cancel_requested" and task.status != TaskStatus.canceled:
            run.state = "launch_failed"
            run.error_message = f"{type(error).__name__}: {error}"[:2000]
            run.finished_at = datetime.now(timezone.utc)
        else:
            run.state = "cancel_requested"
        db.add(run)
        db.commit()

    def reconcile_unresolved_cancellations(self, *, limit: int = 25) -> int:
        """Recover the run ID after a canceled launch lost its HTTP response.

        Only replay requests with a durable Hatchet idempotency key. The
        workflow endpoint returns the original run on a collision, so this
        path does not create a second active run for the same attempt.
        """
        db = self.session_factory()
        try:
            rows = (
                db.query(PipelineRun)
                .filter(PipelineRun.engine == "hatchet",
                        PipelineRun.state == "cancel_requested",
                        PipelineRun.external_run_id.is_(None))
                .order_by(PipelineRun.created_at.asc())
                .limit(max(1, min(int(limit), 100)))
                .all()
            )
            attempts = [(row.id, row.workflow_name, dict(row.request_json or {}), row.created_at)
                        for row in rows]
            db.rollback()  # no DB locks held across HTTP I/O
            recovered = 0
            routes = {AUTO_YOUTUBE_WORKFLOW_NAME: "/runs/auto-youtube",
                      SUBTITLE_JOB_WORKFLOW_NAME: "/runs/subtitle-job",
                      DUBBING_JOB_WORKFLOW_NAME: "/runs/dubbing-job"}
            for run_id, workflow_name, request, created_at in attempts:
                path = routes.get(workflow_name)
                if not path or not request.get("dispatch_key"):
                    continue  # Older unkeyed launches require manual reconciliation.
                # Hatchet's idempotency TTL is seven days; do not re-launch an
                # old canceled workflow after the key may have expired.
                created = created_at.replace(tzinfo=created_at.tzinfo or timezone.utc) if created_at else None
                if created is None or created < datetime.now(timezone.utc) - timedelta(days=6):
                    logger.warning("canceled Hatchet launch %s is too old for automatic idempotent recovery", run_id)
                    continue
                if workflow_name == AUTO_YOUTUBE_WORKFLOW_NAME:
                    payload = {"task_id": request["task_id"],
                               "priority": request.get("priority", 0),
                               "dispatch_key": request["dispatch_key"]}
                else:
                    payload = {"job_id": request["job_id"],
                               "priority": request.get("priority", 0),
                               "dispatch_key": request["dispatch_key"]}
                try:
                    with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                        response = client.post(f"{self.settings.workflow_service_url.rstrip('/')}{path}", json=payload)
                        response.raise_for_status()
                        external_id = str(response.json().get("workflow_run_id") or "").strip()
                    if not external_id:
                        raise RuntimeError("workflow service returned no run ID")
                    self._finish_external_launch(db, run_id, external_id)
                    recovered += 1
                except Exception:
                    db.rollback()
                    logger.exception("could not reconcile stopped Hatchet launch %s", run_id)
            return recovered
        finally:
            db.close()

    def launch_auto_youtube(self, task_id: uuid.UUID) -> PipelineLaunchResult:
        db = self.session_factory()
        try:
            # Lock Task while choosing the durable attempt. Another caller
            # retrying an unknown HTTP result must reuse the same Hatchet key.
            task = db.query(Task).filter(Task.id == task_id).with_for_update().one_or_none()
            if task is None:
                raise ValueError(f"task not found: {task_id}")
            if task.status == TaskStatus.canceled:
                raise ValueError("cannot launch a stopped YouTube task")
            priority = int(task.priority or 0)
            previous = (
                db.query(PipelineRun)
                .filter(PipelineRun.task_id == task_id, PipelineRun.engine == "hatchet",
                        PipelineRun.workflow_name == AUTO_YOUTUBE_WORKFLOW_NAME,
                        PipelineRun.state.in_(["submitting", "launch_failed"]),
                        PipelineRun.external_run_id.is_(None))
                .order_by(PipelineRun.created_at.desc())
                .with_for_update().first()
            )
            if previous and (previous.request_json or {}).get("dispatch_key"):
                created = previous.created_at
                if created is None or created.replace(tzinfo=created.tzinfo or timezone.utc) < datetime.now(timezone.utc) - timedelta(days=6):
                    raise RuntimeError("previous YouTube launch outcome is unconfirmed and its idempotency key expired; manual reconciliation required")
                run = previous
                run_id = run.id
                request = dict(run.request_json)
                run.state = "submitting"
                run.finished_at = None
            else:
                run_id = uuid.uuid4()
                request = {"task_id": str(task_id), "runtime_profile": True,
                           "priority": priority, "dispatch_key": f"auto-youtube:{run_id}"}
                run = PipelineRun(
                    id=run_id, task_id=task_id, engine="hatchet",
                    workflow_name=AUTO_YOUTUBE_WORKFLOW_NAME,
                    state="submitting", request_json=request,
                )
            db.add(run)
            db.commit()

            try:
                with httpx.Client(timeout=20.0, headers=internal_http_headers(self.settings)) as client:
                    response = client.post(
                        f"{self.settings.workflow_service_url.rstrip('/')}/runs/auto-youtube",
                        json={"task_id": str(task_id), "priority": priority,
                              "dispatch_key": str(request["dispatch_key"])},
                    )
                    response.raise_for_status()
                    payload = response.json()
                external_run_id = str(payload.get("workflow_run_id") or "").strip()
                if not external_run_id:
                    raise RuntimeError("workflow service did not return workflow_run_id")
            except Exception as exc:
                self._mark_launch_failed(db, run_id, exc)
                raise

            self._finish_external_launch(db, run_id, external_run_id)
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
            request = {"job_id": str(job_id), "priority": priority,
                       "dispatch_key": dispatch_key or f"subtitle:{run_id}"}
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
                self._mark_launch_failed(db, run_id, exc)
                raise
            self._finish_external_launch(db, run_id, external_run_id)
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
                self._mark_launch_failed(db, run_id, exc)
                raise
            self._finish_external_launch(db, run_id, external_run_id)
            return PipelineLaunchResult(run_id, DUBBING_JOB_WORKFLOW_NAME, external_run_id)
        finally:
            db.close()

