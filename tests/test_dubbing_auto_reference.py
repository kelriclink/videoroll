"""Auto voice clone suggestion must only use original ASR and original raw audio."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from videoroll.apps.dubbing.auto_reference import rank_reference_candidates,source_asr_for_job
from videoroll.apps.dubbing.voice_profiles import create_profile,resolve,delete_profile
from videoroll.db.models import Base,Asset,AssetKind,SubtitleJob,SubtitleJobStatus,Task,TaskStatus,SourceType,SourceLicense
from videoroll.apps.orchestrator_api.routers.dubbing_models import _auto_reference_choices
from fastapi import HTTPException


SAMPLE = [
    {'start':20.0,'end':23.1,'text':'First, the amplifier increases the signal.'},
    {'start':23.2,'end':26.7,'text':'Then a second stage removes the noise.'},
    {'start':26.9,'end':30.1,'text':'Finally, we connect the output to the speaker.'},
    {'start':35.0,'end':37.8,'text':'This is the next section.'},
    {'start':37.9,'end':42.2,'text':'It will demonstrate the clock divider using a circuit.'},
]


def test_select_contiguous_original_cues_without_manual_timestamps():
    result=rank_reference_candidates(SAMPLE,limit=3)
    assert result
    first=result[0]
    assert 3 <= first['duration_seconds'] <= 16
    assert first['start_seconds'] >= 20
    assert first['reference_text'].startswith(('First,','This is'))
    assert all('中文' not in item['reference_text'] for item in result)
    assert all(item['speech_coverage']>=.69 for item in result)
    assert len(result) <= 3


def test_no_speech_or_overlapping_speakers_do_not_fake_good_reference():
    assert not rank_reference_candidates([{'start':0,'end':2,'text':'Hi'}])
    assert not rank_reference_candidates([{'start':0,'end':9,'text':'', 'confidence':.9}])
    assert not rank_reference_candidates([{'start':0,'end':9,'text':'unknown speech','confidence':.05}])
    mixed=[{'start':10,'end':12,'text':'One speaker has a short part','speaker_id':'A'},
           {'start':12.1,'end':13.9,'text':'Another person overlaps here','speaker_id':'B'}]
    assert not rank_reference_candidates(mixed)
    assert not rank_reference_candidates([{'start':float('nan'),'end':10,'text':'bad value'}])


def test_long_single_utterance_can_be_selected_without_retranslation():
    sample=[{'start':5.1,'end':12.2,'text':'Hello, we are explaining the electronics today.'}]
    result=rank_reference_candidates(sample)
    assert len(result)==1
    assert result[0]['reference_text']==sample[0]['text']
    assert result[0]['start_seconds']==5.1


class FakeStore:
    def __init__(self,files):self.files=files
    def path_for(self,key):
        if key not in self.files:raise FileNotFoundError(key)
        return self.files[key]


def test_asr_source_pinned_to_subtitle_job_and_never_uses_translated(tmp_path):
    original=tmp_path/'original.json';original.write_text(json.dumps(SAMPLE))
    zh=tmp_path/'translated.json';zh.write_text(json.dumps([{'start':20,'end':30,'text':'这是中文翻译，不应作为音色克隆参考文本。'}]))
    store=FakeStore({'source':original,'translated':zh})
    tid=uuid.uuid4()
    with create_engine('sqlite+pysqlite:///:memory:',connect_args={'check_same_thread':False},poolclass=StaticPool).begin() as conn:
        Base.metadata.create_all(conn)
        with Session(bind=conn) as db:
            task=Task(id=tid,source_type=SourceType.local,source_license=SourceLicense.own)
            db.add(task)
            db.add(Asset(task_id=tid,kind=AssetKind.segments_json,storage_key='source'))
            db.add(Asset(task_id=tid,kind=AssetKind.segments_json,storage_key='other-source'))
            job=SubtitleJob(id=uuid.uuid4(),task_id=tid,status=SubtitleJobStatus.succeeded,
                request_json={'artifacts':{'source_segments_key':'source','final_subtitle_segments_key':'translated'}})
            db.add(job);db.flush()
            read,key=source_asr_for_job(db,store,job)
            assert key=='source' and read==SAMPLE
            candidate=rank_reference_candidates(read)[0]
            assert '中文翻译' not in candidate['reference_text']
            job.request_json={'artifacts':{'final_subtitle_segments_key':'translated'}}
            with pytest.raises(ValueError,match='multiple ASR'):
                source_asr_for_job(db,store,job)


def test_auto_extract_wav_without_manual_text_or_start(tmp_path):
    src=tmp_path/'original.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','lavfi','-i','color=c=black:s=160x90:d=15:r=25',
        '-f','lavfi','-i','sine=frequency=440:duration=15',
        '-c:v','mpeg4','-c:a','aac',str(src)],check=True)
    candidates=rank_reference_candidates([
        {'start':2,'end':9,'text':'The original speaker describes how the electronics work.'},
    ])
    assert candidates
    item=candidates[0]
    voice=create_profile(source=src,task_id=uuid.uuid4(),asset_key='raw/original.mp4',
        name='自动识别的原声',start_seconds=item['start_seconds'],duration_seconds=item['duration_seconds'],
        reference_text=item['reference_text'],consent=True,base=tmp_path)
    assert voice['reference_text']==item['reference_text']
    record,wav=resolve(voice['id'],tmp_path)
    assert wav.is_file() and record['duration_seconds']>=6.9
    delete_profile(voice['id'],tmp_path)
    assert not wav.exists()


def test_auto_reference_http_api_uses_source_video_and_original_text(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from videoroll.apps.orchestrator_api.routers.dubbing_models import router
    from videoroll.apps.orchestrator_api.dependencies import get_db,get_store
    from videoroll.apps.dubbing import voice_profiles
    source=tmp_path/'source.webm'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','lavfi','-i','color=c=black:s=160x90:d=15:r=25',
        '-f','lavfi','-i','sine=frequency=220:duration=15',
        '-c:v','libvpx','-c:a','libvorbis',str(source)],check=True)
    original=tmp_path/'asr.json'
    original.write_text(json.dumps([{'start':3,'end':10,'text':'Here is an example of original spoken English technology narration.'}]))
    tid,sid=uuid.uuid4(),uuid.uuid4()
    engine=create_engine('sqlite+pysqlite:///:memory:',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Task(id=tid,source_type=SourceType.local,source_license=SourceLicense.own))
        db.add(SubtitleJob(id=sid,task_id=tid,status=SubtitleJobStatus.succeeded,
              request_json={'input':{'key':'raw/source.webm'},
                            'artifacts':{'source_segments_key':'asr/english.json',
                                         'final_subtitle_segments_key':'sub/chinese.json'}}))
        db.add(Asset(task_id=tid,kind=AssetKind.video_raw,storage_key='raw/source.webm'))
        db.add(Asset(task_id=tid,kind=AssetKind.segments_json,storage_key='asr/english.json'))
        db.commit()
    app=FastAPI();app.include_router(router)
    def db_dependency():
        with Session(engine) as db:yield db
    app.dependency_overrides[get_db]=db_dependency
    app.dependency_overrides[get_store]=lambda: FakeStore({'raw/source.webm':source,'asr/english.json':original})
    monkeypatch.setenv('DUBBING_MODEL_ROOT',str(tmp_path))
    with TestClient(app) as client:
        suggestions=client.get(f'/tasks/{tid}/dubbing/voice_candidates',params={'subtitle_job_id':str(sid)})
        assert suggestions.status_code==200,suggestions.text
        first=suggestions.json()['candidates'][0]
        assert first['reference_text'].startswith('Here is an example')
        data={'subtitle_job_id':str(sid),'candidate_index':0,'authorization_confirmed':False}
        assert client.post(f'/tasks/{tid}/dubbing/voice_profiles/auto',json=data).status_code==403
        data['authorization_confirmed']=True
        result=client.post(f'/tasks/{tid}/dubbing/voice_profiles/auto',json=data)
        assert result.status_code==201,result.text
        record=result.json()
        assert record['reference_text']==first['reference_text']
        assert record['task_id']==str(tid)
        assert record['source_asset_key']=='raw/source.webm'
        assert record['selection_method'].startswith('original_asr')
        assert record['duration_seconds']>6
        profile,wav=voice_profiles.resolve(record['id'],tmp_path)
        assert wav.is_file()
        voice_profiles.delete_profile(profile['id'],tmp_path)


def test_cosyvoice_job_allows_reference_voice_without_qwen_preset(tmp_path, monkeypatch):
    from videoroll.apps.dubbing.job import create_dubbing_job
    from videoroll.apps.dubbing import voice_profiles
    from sqlalchemy.orm import sessionmaker
    engine=create_engine('sqlite+pysqlite:///:memory:',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    tid,sid,pid=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    monkeypatch.setattr(voice_profiles,'resolve',lambda value: (
        {'task_id':str(tid),'reference_text':'Original spoken English transcript.',
         'audio_sha256':'a'*64},tmp_path/'reference.wav'))
    monkeypatch.setenv('DUBBING_MODEL_ROOT',str(tmp_path))
    with factory() as db:
        db.add(Task(id=tid,source_type=SourceType.local,source_license=SourceLicense.own,
                    status=TaskStatus.subtitle_ready))
        db.add(SubtitleJob(id=sid,task_id=tid,status=SubtitleJobStatus.succeeded,
                           request_json={'artifacts':{'final_subtitle_segments_key':'sub/chinese.json'}}))
        db.add(Asset(task_id=tid,kind=AssetKind.video_raw,storage_key='raw/source.webm'))
        db.commit()
        job=create_dubbing_job(db,task_id=tid,subtitle_job_id=sid,
            subtitle_video_key='raw/source.webm',operation_key='cosy:auto:1',
            model_path='cosyvoice3:base',voice='reference',reference_profile_id=pid,
            cosy_mode='zero_shot')
        assert job.status=='queued'
        assert job.request_json['voice']=='reference'
        assert job.request_json['quality']['profile_id']==str(pid)
        assert job.request_json['quality']['cosy_mode']=='zero_shot'
