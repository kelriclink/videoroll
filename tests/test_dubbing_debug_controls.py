from __future__ import annotations
from unittest.mock import patch
import uuid
import pytest
from videoroll.apps.dubbing.quality import normalize_quality,audio_cache_key
from videoroll.apps.dubbing.timing import SpokenSegment,check_segment_fit,SpeechTooLong
from videoroll.apps.dubbing.reconfigure import reconfigure_dubbing
from videoroll.apps.dubbing.job import create_dubbing_job
from videoroll.db.models import DubbingJob
from tests.test_dubbing_production_controls import _prepare_job_media


def _settings(preview_only=False,model_path='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'):
    return dict(subtitle_job_id=None,subtitle_video_key='',operation_key='debug:manual',
        voice='Vivian',model_path=model_path,retain_original_audio=False,
        preview_only=preview_only,instruct='',temperature=0.9,top_p=0.9,
        batch_size=1,mix_mode='replace',reference_profile_id=None,
        cosy_mode='cross_lingual',cosy_speed=1.0,debug_timing=True)


def test_debug_timing_allows_first_sentence_141_percent_without_changing_default():
    cues=[SpokenSegment(index=1,start=0.4,end=5.84,
        subtitle_text='这是测试',speech_text='这是测试',voice='Vivian')]
    with pytest.raises(SpeechTooLong):
        check_segment_fit(cues,0,7.68,video_duration=60)
    assert check_segment_fit(cues,0,7.68,video_duration=60,max_speed=2.0)==pytest.approx(7.68/5.94,rel=.01)


def test_regenerate_uses_new_cache_key_even_for_identical_text():
    quality=normalize_quality(model='cosyvoice3:base',profile_id='p',profile_sha256='a',
                              debug_timing=True)
    old=audio_cache_key(text='测试',voice='reference',model='cosyvoice3:base',quality=quality)
    newer=dict(quality,_sample_nonce='random-sample')
    assert audio_cache_key(text='测试',voice='reference',model='cosyvoice3:base',quality=newer)!=old
    assert newer['debug_timing'] is True


def test_rerun_existing_preview_with_debug_parameters_and_force_regeneration(tmp_path,monkeypatch):
    factory,store,task_id,job_id=_prepare_job_media(tmp_path)
    with factory() as db:
        job=db.get(DubbingJob,job_id)
        job.status='preview_ready'
        job.request_json={**job.request_json,'quality':{'temperature':0.9,'top_p':0.9}}
        job.output_json={'segments':{'cached':'some.wav'}, 'timing_issue':{'segment_index':1}}
        subtitle_id=job.subtitle_job_id
        video_key=job.request_json['subtitle_video_key']
        db.commit()
    values=_settings(preview_only=False)
    values.update(subtitle_job_id=subtitle_id,subtitle_video_key=video_key)
    with patch('videoroll.apps.dubbing.reconfigure.installed',return_value=True):
        with factory() as db:
            updated=reconfigure_dubbing(db,task_id=task_id,job_id=job_id,values=values,regenerate=True)
            assert updated.status=='queued'
            assert updated.request_json['preview_only'] is False
            assert updated.request_json['quality']['debug_timing'] is True
            assert updated.request_json['quality']['_sample_nonce']
            assert not (updated.output_json or {}).get('segments')
            assert (updated.output_json or {}).get('timing_issue') is None
    with factory() as db:
        assert db.get(DubbingJob,job_id).status=='queued'


def test_rerun_rejects_changes_to_source_or_race_against_running_job(tmp_path):
    factory,store,task_id,job_id=_prepare_job_media(tmp_path)
    with factory() as db:
        job=db.get(DubbingJob,job_id)
        subtitle_id=job.subtitle_job_id
        video_key=job.request_json['subtitle_video_key']
        job.status='preview_ready';db.commit()
    values=_settings();values.update(subtitle_job_id=subtitle_id,subtitle_video_key='wrong-source')
    with factory() as db,pytest.raises(ValueError,match='source subtitle/video'):
        reconfigure_dubbing(db,task_id=task_id,job_id=job_id,values=values)
    values['subtitle_video_key']=video_key
    with factory() as db:
        job=db.get(DubbingJob,job_id);job.status='synthesizing';db.commit()
    with factory() as db,pytest.raises(ValueError,match='cannot change parameters'):
        reconfigure_dubbing(db,task_id=task_id,job_id=job_id,values=values)
