"""Re-run a stopped debug dubbing job with current form parameters.

Never mutates a running/leased job or a completed/canceled artifact. Reuses the
stable dubbing job ID and source assets; generation settings change cache keys.
"""
from __future__ import annotations
import hashlib
import uuid
from typing import Any
from sqlalchemy.orm import Session
from videoroll.apps.dubbing.job import lease_is_active
from videoroll.apps.dubbing.model_management import QWEN_MODELS,COSY_MODELS,folder,installed
from videoroll.apps.dubbing.quality import normalize_quality
from videoroll.db.models import DubbingJob,Task,TaskStatus


def reconfigure_dubbing(db: Session, *, task_id: uuid.UUID, job_id: uuid.UUID,
                        values: dict[str,Any], regenerate: bool = False) -> DubbingJob:
    task=db.query(Task).filter(Task.id==task_id).with_for_update().one_or_none()
    if task is None or task.status==TaskStatus.canceled:
        raise ValueError('source video task is stopped or missing')
    job=db.query(DubbingJob).filter(DubbingJob.task_id==task_id,DubbingJob.id==job_id).with_for_update().one_or_none()
    if job is None:raise LookupError('dubbing job not found')
    if job.status not in ('preview_ready','needs_review','paused','failed') or (job.lease_owner and lease_is_active(job.lease_until)):
        raise ValueError(f'cannot change parameters while dubbing job is {job.status}')
    active_other=db.query(DubbingJob).filter(
        DubbingJob.task_id==task_id, DubbingJob.id!=job_id,
        DubbingJob.status.in_(['queued','preparing','synthesizing','aligning','mixing','pause_requested','cancel_requested']),
    ).first()
    if active_other is not None:
        raise ValueError('another dubbing job is running for this video')
    old=dict(job.request_json or {})
    if str(values['subtitle_job_id'])!=str(job.subtitle_job_id) or values['subtitle_video_key']!=old.get('subtitle_video_key'):
        raise ValueError('source subtitle/video differs from this job; cancel it to create a new source job')
    model=str(values['model_path']);voice=str(values['voice'])
    if not installed(model):raise ValueError('TTS model not installed')
    profile_id=profile_sha=''
    if model in COSY_MODELS:
        from videoroll.apps.dubbing.cosyvoice3_adapter import runtime_ready
        from videoroll.apps.dubbing.voice_profiles import resolve
        if not runtime_ready():raise ValueError('CosyVoice3 runtime unavailable')
        if voice!='reference' or not values.get('reference_profile_id'):
            raise ValueError('choose a reference voice for CosyVoice3')
        profile,_=resolve(str(values['reference_profile_id']))
        if str(profile.get('task_id'))!=str(task_id):
            raise ValueError('voice profile belongs to a different video')
        if values['cosy_mode']=='zero_shot' and not profile.get('reference_text'):
            raise ValueError('zero-shot mode requires the reference transcript')
        if values['cosy_mode']=='instruct' and not str(values['instruct']).strip():
            raise ValueError('instruction mode requires a speaking instruction')
        profile_id=str(values['reference_profile_id'])
        profile_sha=str(profile['audio_sha256'])
    elif model in QWEN_MODELS:
        from videoroll.apps.dubbing.qwen_tts import CHINESE_SPEAKERS
        if voice not in CHINESE_SPEAKERS:raise ValueError('invalid Qwen TTS voice')
    else:raise ValueError('unsupported TTS engine')
    quality=normalize_quality(instruct=values['instruct'],temperature=values['temperature'],
        top_p=values['top_p'],batch_size=values['batch_size'],mix_mode=values['mix_mode'],
        model=model,cosy_speed=values['cosy_speed'],cosy_mode=values['cosy_mode'],
        profile_id=profile_id,profile_sha256=profile_sha,debug_timing=values['debug_timing'],
        auto_group_speech=values.get('auto_group_speech',False),
        auto_rewrite_speech=values.get('auto_rewrite_speech',False))
    if values['mix_mode']=='background_only':
        from videoroll.apps.dubbing.separation import available
        if not available():raise ValueError('background separation is unavailable')
    marker=folder(model)/'.videoroll-complete.json'
    if marker.is_file():quality['_model_revision']=hashlib.sha256(marker.read_bytes()).hexdigest()[:16]
    old_quality=dict(old.get('quality') or {})
    if not regenerate and model==old.get('model_path'):
        # Preserve a cache generation nonce when moving from preview to full.
        if old_quality.get('_sample_nonce'):
            quality['_sample_nonce']=old_quality['_sample_nonce']
    elif regenerate:
        # Explicitly produce new audio even when all user parameters match.
        quality['_sample_nonce']=uuid.uuid4().hex
    if regenerate and '_sample_nonce' not in quality:
        quality['_sample_nonce']=uuid.uuid4().hex
    reset_audio=(model!=old.get('model_path') or voice!=old.get('voice') or regenerate or
                 any(quality.get(k)!=old_quality.get(k) for k in (
                     'instruct','temperature','top_p','cosy_mode','cosy_speed',
                     'profile_id','profile_sha256','_model_revision','auto_group_speech')))
    old.update({'model_path':model,'voice':voice,'reference_profile_id':profile_id or None,
                'preview_only':bool(values['preview_only']),
                'retain_original_audio':values['mix_mode']=='original_low',
                'quality':quality,'cache_schema':2})
    job.request_json=old
    output=dict(job.output_json or {})
    output.pop('timing_issue',None)
    if reset_audio:
        output.pop('segments',None)
        output.pop('performance',None)
        output.pop('speech_units',None)
    job.output_json=output
    job.status='queued'
    job.progress=0
    job.error_message=None
    job.lease_owner=None
    job.lease_until=None
    db.commit()
    return job
