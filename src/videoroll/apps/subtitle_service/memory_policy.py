from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.config import SubtitleServiceSettings
from videoroll.db.models import RenderJob, RenderJobStatus, SourceType, SubtitleJob, SubtitleJobStatus, Task, TaskStatus
from videoroll.utils import resources

_MIB = 1024**2
_MEMORY_WAIT_KEY = "_memory_wait_reason"
_REMOTE_ENGINES = {"mock", "external-whisper", "groq-whisper", "cloudflare-workers-ai"}


def local_asr_budget_mb(request: Any, defaults: dict[str, Any], settings: SubtitleServiceSettings) -> int:
    """Budget the selected model without changing its precision or engine."""
    request = request if isinstance(request, dict) else {}
    asr = request.get("asr") if isinstance(request.get("asr"), dict) else {}
    engine = str(asr.get("engine") or "auto").strip()
    if engine in {"", "auto"}:
        engine = str(defaults.get("default_engine") or settings.asr_engine).strip()
    if engine in _REMOTE_ENGINES:
        return 0
    explicit = int(settings.local_asr_memory_mb)
    if explicit > 0:
        return explicit
    model = str(asr.get("model") or defaults.get("default_model") or "").strip().lower()
    # Match standard names and exported/Hugging Face directory basenames only;
    # an unknown custom model must not inherit tiny's low budget accidentally.
    name = model.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    for tier, budget in (("tiny", 1024), ("base", 1536), ("small", 2048), ("medium", 4096)):
        if re.fullmatch(rf"(?:(?:faster-)?whisper[-_])?{tier}(?:\.en)?(?:[-_](?:fp16|fp32|int8|int4))?(?:[-_]ov)?", name):
            return budget
    return 8192


def set_memory_wait_reason(job: SubtitleJob, reason: str | None) -> None:
    request = dict(job.request_json) if isinstance(job.request_json, dict) else {}
    if reason:
        if request.get(_MEMORY_WAIT_KEY) == reason:
            return
        request[_MEMORY_WAIT_KEY] = reason
    elif _MEMORY_WAIT_KEY in request:
        request.pop(_MEMORY_WAIT_KEY)
    else:
        return
    job.request_json = request


def memory_wait_reason(job: SubtitleJob) -> str | None:
    request = job.request_json if isinstance(job.request_json, dict) else {}
    value = request.get(_MEMORY_WAIT_KEY)
    return str(value)[:1000] if value else None


@dataclass
class MemoryAdmission:
    """A snapshot plus reservations protected by the task queue settings lock.

    Reserve full model budgets even for running jobs: a freshly leased worker
    may not have loaded its model yet. This intentionally errs on the side of
    leaving spare RAM instead of guessing unobserved per-task peak RSS.
    """

    settings: SubtitleServiceSettings
    defaults: dict[str, Any]
    total_mb: int | None
    available_mb: int | None
    scope: str = "scheduler"
    reservations: dict[uuid.UUID, int] = field(default_factory=dict)
    bootstrap_request: dict[str, Any] = field(default_factory=dict)

    @property
    def reserved_mb(self) -> int:
        return sum(self.reservations.values())

    def budget_for(self, job: SubtitleJob) -> int:
        return local_asr_budget_mb(job.request_json, self.defaults, self.settings)

    def reason(self, task_id: uuid.UUID | None, budget_mb: int) -> str | None:
        if budget_mb <= 0:
            return None
        location = "执行容器" if self.scope == "worker" else "调度节点"
        if self.total_mb is None or self.available_mb is None:
            return f"等待内存信息：{location}的内存统计不可用，暂缓启动本地 ASR。"
        reserved = self.reserved_mb - self.reservations.get(task_id, 0)
        usable = min(self.total_mb, self.available_mb) - self.settings.memory_reserve_mb - reserved
        if budget_mb > usable:
            return (
                f"等待内存：{location}可用 {self.available_mb} MiB，保留 {self.settings.memory_reserve_mb} MiB，"
                f"其他任务预算 {reserved} MiB，本任务需要 {budget_mb} MiB。"
            )
        return None

    def reserve(self, task_id: uuid.UUID, budget_mb: int) -> str | None:
        reason = self.reason(task_id, budget_mb)
        if reason is None:
            self.reservations[task_id] = max(self.reservations.get(task_id, 0), budget_mb)
        return reason

    def summary(self, max_concurrency: int) -> dict[str, Any]:
        budget = local_asr_budget_mb({}, self.defaults, self.settings)
        # Include explicit larger per-job models when the saved default is tiny.
        budget = max([budget, *self.reservations.values()])
        if budget <= 0:
            effective = max_concurrency
        else:
            free = 0 if self.available_mb is None else max(
                0, self.available_mb - self.settings.memory_reserve_mb - self.reserved_mb
            )
            effective = min(max_concurrency, len(self.reservations) + free // budget)
        return {
            "scope": self.scope,
            "effective_max_concurrency": effective,
            "total_memory_mb": self.total_mb,
            "available_memory_mb": self.available_mb,
            "reserved_memory_mb": self.reserved_mb,
            "reserve_memory_mb": self.settings.memory_reserve_mb,
            "local_asr_memory_mb": budget,
            "waiting_reason": self.reason(None, budget) if effective < max_concurrency else None,
        }


def load_memory_admission(
    db: Session,
    settings: SubtitleServiceSettings,
    *,
    now: datetime,
    task_lock_owner: str,
    dispatched_progress: int = 1,
    scope: str = "scheduler",
) -> MemoryAdmission:
    """Read durable claims; DB errors propagate instead of opening admission.

    Call inside the same settings-row transaction that dispatches/claims work.
    API callers may take a read-only snapshot. No broker statistics are needed.
    """
    stats = resources.read_effective_memory_stats()
    admission = MemoryAdmission(
        settings=settings,
        defaults=get_asr_settings(db, settings),
        total_mb=int(stats["total_bytes"]) // _MIB if stats else None,
        available_mb=int(stats["available_bytes"]) // _MIB if stats else None,
        scope=scope,
    )
    live_task = Task.status.notin_([TaskStatus.canceled, TaskStatus.published])
    jobs = (
        db.query(SubtitleJob).join(Task, Task.id == SubtitleJob.task_id)
        .filter(live_task, SubtitleJob.status.in_([SubtitleJobStatus.queued, SubtitleJobStatus.running])).all()
    )
    jobs_by_task: set[uuid.UUID] = set()
    for job in jobs:
        jobs_by_task.add(job.task_id)
        if job.status == SubtitleJobStatus.running or int(job.progress or 0) == dispatched_progress:
            budget = admission.budget_for(job)
            admission.reservations[job.task_id] = max(admission.reservations.get(job.task_id, 0), budget)
    render_jobs = (
        db.query(RenderJob).join(Task, Task.id == RenderJob.task_id)
        .filter(live_task, RenderJob.status.in_([RenderJobStatus.queued, RenderJobStatus.running])).all()
    )
    for job in render_jobs:
        jobs_by_task.add(job.task_id)
        if job.status == RenderJobStatus.running or int(job.progress or 0) == dispatched_progress:
            admission.reservations.setdefault(job.task_id, 0)
    profile = get_auto_profile(db)
    admission.bootstrap_request = {"asr": {"engine": profile.get("asr_engine"), "model": profile.get("asr_model")}}
    bootstrap_budget = local_asr_budget_mb(admission.bootstrap_request, admission.defaults, settings)
    for task_id, in (
        db.query(Task.id).filter(
            live_task,
            Task.source_type == SourceType.youtube,
            Task.lock_owner == task_lock_owner,
            Task.lock_until.is_not(None),
            Task.lock_until > now,
        ).all()
    ):
        if task_id not in jobs_by_task:
            admission.reservations[task_id] = bootstrap_budget
    return admission
