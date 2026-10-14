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
    from videoroll.apps.dubbing.qwen_tts import QwenCustomVoice
    from videoroll.apps.dubbing.model_management import folder, installed, selection
    from videoroll.config import get_subtitle_settings
    from videoroll.db.migrate import initialize_database
    from videoroll.db.session import get_sessionmaker
    from videoroll.db.models import DubbingJob
    from videoroll.storage.filesystem import FileStore

    settings = get_subtitle_settings()
    selected = selection()
    initialize_database(settings.database_url)
    factory = get_sessionmaker(settings.database_url)
    with factory() as db:
        current = db.get(DubbingJob, uuid.UUID(job_id))
        if current is None:
            raise ValueError("dubbing job does not exist")
        quality = dict((current.request_json or {}).get("quality") or {})

    from videoroll.apps.dubbing.model_management import COSY_MODELS
    opened = []
    def make_synthesizer(model: str, voice: str):
        if not installed(model):
            raise RuntimeError('Selected TTS model has not been downloaded')
        if model in COSY_MODELS:
            from videoroll.apps.dubbing.cosyvoice3_adapter import CosyVoice3Synthesizer
            from videoroll.apps.dubbing.voice_profiles import resolve
            profile_id = str(quality.get('profile_id') or '')
            profile, reference = resolve(profile_id)
            if profile.get('audio_sha256') != quality.get('profile_sha256'):
                raise RuntimeError('reference audio was changed after job submission')
            engine = CosyVoice3Synthesizer(
                model_path=model, model_dir=folder(model), reference=reference,
                prompt_text=str(profile.get('reference_text') or ''),
                mode=str(quality.get('cosy_mode') or 'cross_lingual'),
                instruct=str(quality.get('instruct') or ''),
                speed=float(quality.get('cosy_speed', 1.0)), fp16=False,
            )
            opened.append(engine)
            return engine
        return QwenCustomVoice(
            model_path=str(folder(model)), cache_identity=model,
            device=selected["device"], voice=voice,
            instruct=str(quality.get("instruct") or ""),
            temperature=float(quality.get("temperature") or 0.9),
            top_p=float(quality.get("top_p") or 0.9),
        )
    try:
        return execute_dubbing_job(
            uuid.UUID(job_id),
            session_factory=get_sessionmaker(settings.database_url),
            store=FileStore(settings),
            work_dir=Path(settings.work_dir),
            synthesizer_factory=make_synthesizer,
            ffmpeg=settings.ffmpeg_path,
        )
    finally:
        for engine in opened:
            engine.close()


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
        return {"status": str(result.get("status") or "succeeded"),
                "video_key": str(result.get("video_key") or "")}
    return dubbing_job
