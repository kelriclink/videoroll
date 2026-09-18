from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import uuid

from videoroll.apps.subtitle_service.subtitle_finalization import complete_subtitle_handoff
from videoroll.db.models import RenderJob, RenderJobStatus, SubtitleJobStatus


ROOT = Path(__file__).resolve().parents[1]


class _Query:
    def __init__(self, existing: RenderJob | None) -> None:
        self._existing = existing

    def filter(self, *_args: object) -> "_Query":
        return self

    def order_by(self, *_args: object) -> "_Query":
        return self

    def first(self) -> RenderJob | None:
        return self._existing


class _DB:
    def __init__(self, *, existing: RenderJob | None = None, job: object | None = None) -> None:
        self.existing = existing
        self.job = job
        self.added: list[object] = []
        self.commits: list[tuple[object, int]] = []
        self.query_count = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    def query(self, model: object) -> _Query:
        assert model is RenderJob
        self.query_count += 1
        return _Query(self.existing)

    def commit(self) -> None:
        render_count = sum(isinstance(item, RenderJob) for item in self.added)
        status = getattr(self.job, "status", None)
        self.commits.append((status, render_count))


def _task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), lock_owner="subtitle_service.task_queue", lock_until=object())


def _job(task_id: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        task_id=task_id,
        status=SubtitleJobStatus.running,
        progress=80,
    )


def test_render_handoff_commits_render_and_subtitle_completion_together() -> None:
    task = _task()
    job = _job(task.id)
    db = _DB(job=job)
    logs: list[str] = []
    queue_kicks: list[bool] = []

    result = complete_subtitle_handoff(
        db=db,  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        automatic_runtime_profile=False,
        burn_in=True,
        soft_sub=False,
        render_payload={"input_key": "video.mp4", "srt_key": "subtitle.srt"},
        ensure_not_stopped=lambda: None,
        unlock_task=lambda _task: (_ for _ in ()).throw(AssertionError("render handoff must keep task lock")),
        kick_task_queue=lambda: queue_kicks.append(True),
        log=logs.append,
        upload_log=lambda: None,
    )

    assert result == {"status": "ok", "detail": "render queued"}
    assert job.status == SubtitleJobStatus.succeeded
    assert job.progress == 100
    render_jobs = [item for item in db.added if isinstance(item, RenderJob)]
    assert len(render_jobs) == 1
    assert render_jobs[0].status == RenderJobStatus.queued
    assert render_jobs[0].subtitle_job_id == job.id
    # The commit observes both the queued render and completed subtitle job.
    assert db.commits == [(SubtitleJobStatus.succeeded, 1)]
    assert logs == ["render queued; waiting for task queue"]
    assert queue_kicks == [True]


def test_render_handoff_reuses_existing_queued_render() -> None:
    task = _task()
    job = _job(task.id)
    existing = RenderJob(
        task_id=task.id,
        subtitle_job_id=job.id,
        status=RenderJobStatus.queued,
        progress=0,
        request_json={"existing": True},
    )
    db = _DB(existing=existing, job=job)

    result = complete_subtitle_handoff(
        db=db,  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        automatic_runtime_profile=True,
        burn_in=False,
        soft_sub=False,
        render_payload={"new": True},
        ensure_not_stopped=lambda: None,
        unlock_task=lambda _task: None,
        kick_task_queue=lambda: None,
        log=lambda _message: None,
        upload_log=lambda: None,
    )

    assert result["detail"] == "render queued"
    assert not any(isinstance(item, RenderJob) for item in db.added)
    assert db.query_count == 1
    assert job.status == SubtitleJobStatus.succeeded


def test_no_render_handoff_completes_job_and_releases_task_lock() -> None:
    task = _task()
    job = _job(task.id)
    db = _DB(job=job)
    unlocked: list[uuid.UUID] = []
    queue_kicks: list[bool] = []

    def unlock(value: object) -> None:
        unlocked.append(getattr(value, "id"))

    result = complete_subtitle_handoff(
        db=db,  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        job=job,  # type: ignore[arg-type]
        automatic_runtime_profile=False,
        burn_in=False,
        soft_sub=False,
        render_payload={},
        ensure_not_stopped=lambda: None,
        unlock_task=unlock,  # type: ignore[arg-type]
        kick_task_queue=lambda: queue_kicks.append(True),
        log=lambda _message: None,
        upload_log=lambda: None,
    )

    assert result == {"status": "ok"}
    assert job.status == SubtitleJobStatus.succeeded
    assert job.progress == 100
    assert unlocked == [task.id]
    assert db.query_count == 0
    assert queue_kicks == [True]


def test_worker_uses_shared_finalizer_for_resume_and_normal_paths() -> None:
    source = (
        ROOT / "src" / "videoroll" / "apps" / "subtitle_service" / "worker.py"
    ).read_text(encoding="utf-8")

    assert source.count("complete_subtitle_handoff(") == 2
    assert 'render queued; waiting for task queue' not in source
