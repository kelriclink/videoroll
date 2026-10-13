"""Independent Hatchet pool for Chinese dubbing jobs."""
from __future__ import annotations

import os
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
    from videoroll.apps.dubbing.qwen_tts import QwenCustomVoice
    from videoroll.config import get_subtitle_settings
    from videoroll.db.migrate import initialize_database
    from videoroll.db.session import get_sessionmaker
    from videoroll.storage.filesystem import FileStore

    settings = get_subtitle_settings()
    initialize_database(settings.database_url)
    return execute_dubbing_job(
        uuid.UUID(job_id),
        session_factory=get_sessionmaker(settings.database_url),
        store=FileStore(settings),
        work_dir=Path(settings.work_dir),
        synthesizer_factory=lambda model, voice: QwenCustomVoice(
            model_path=model, device=str(os.getenv("DUBBING_TTS_DEVICE") or "cuda:0"), voice=voice,
        ),
        ffmpeg=settings.ffmpeg_path,
    )


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
        schedule_timeout=timedelta(minutes=30),
        execution_timeout=timedelta(hours=24),
        desired_worker_labels=pool,
    )
    def dubbing_job(input: DubbingJobWorkflowInput, _ctx: Any) -> dict[str, str]:
        result = _run_dubbing_job(input.job_id)
        return {"status": "succeeded", "video_key": str(result["video_key"])}
    return dubbing_job
