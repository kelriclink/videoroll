"""Unified service: auth, model pooling, per-GPU serialization and path safety."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from videoroll.apps.dubbing import serving, service_manager
from videoroll.apps.dubbing.service_client import RemoteSpeechSynthesizer
from videoroll.apps.dubbing.service_manager import SpeechEnginePool


def test_service_requires_secret_token_and_has_health(tmp_path, monkeypatch):
    f = tmp_path / 'token';f.write_text('abcdef' * 8)
    monkeypatch.setenv('DUBBING_SERVICE_TOKEN_FILE', str(f))
    with TestClient(serving.app) as client:
        assert client.get('/health').json()['ok'] is True
        assert client.get('/v1/status').status_code == 401
        assert client.get('/v1/status', headers={'X-VideoRoll-Token': 'incorrect'}).status_code == 401
        result = client.get('/v1/status', headers={'X-VideoRoll-Token': f.read_text()})
        assert result.status_code == 200
        assert result.json()['gpu_slots'] == 1


def test_service_forwards_to_single_engine_scheduler(tmp_path, monkeypatch):
    f = tmp_path / 'token';f.write_text('abcdef' * 8)
    monkeypatch.setenv('DUBBING_SERVICE_TOKEN_FILE', str(f))
    called=[]
    def synthesize(**kwargs):
        called.append(kwargs)
        return {'ok': True, 'outputs':[i['output'] for i in kwargs['items']]}
    monkeypatch.setattr(serving.pool, 'synthesize', synthesize)
    with TestClient(serving.app) as client:
        headers={'X-VideoRoll-Token': f.read_text()}
        payload={'model':'Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice', 'voice':'Vivian',
                 'quality':{'temperature':0.9},
                 'items':[{'text':'你好，这是测试。','output':str(tmp_path/'out.wav')}],
                 'timeout_seconds':600}
        assert client.post('/v1/synthesize',json=payload).status_code == 401
        result=client.post('/v1/synthesize',json=payload,headers=headers)
        assert result.status_code == 200
        assert called[0]['model']==payload['model']
        assert called[0]['quality']==payload['quality']
        assert called[0]['items'][0]['text']=='你好，这是测试。'


def test_service_rejects_arbitrary_output_paths(tmp_path, monkeypatch):
    monkeypatch.setenv('DUBBING_OUTPUT_ROOT', str(tmp_path))
    manager=SpeechEnginePool()
    model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
    with pytest.raises(ValueError, match='inside VideoRoll work'):
        manager.synthesize(model=model,voice='Vivian',quality={},
                           items=[{'text':'你好', 'output':'/tmp/malicious.wav'}])
    with pytest.raises(ValueError, match='inside VideoRoll work'):
        manager.synthesize(model=model,voice='Vivian',quality={},
                           items=[{'text':'你好', 'output':'relative.wav'}])


def test_model_switch_reuses_same_warm_process_then_releases_prior_one(tmp_path, monkeypatch):
    monkeypatch.setenv('DUBBING_OUTPUT_ROOT',str(tmp_path))
    pool=SpeechEnginePool()
    launches=[]
    monkeypatch.setattr(service_manager, 'resolve', lambda profile_id: (
        {'audio_sha256': 'abc'}, Path('/tmp/reference.wav')))
    class Engine:
        def poll(self):return None
        def __init__(self):
            class In:
                def __init__(self):self.writes=[]
                def write(self,data):self.writes.append(data)
                def flush(self):pass
            self.stdin=In()
    def load(model, voice, quality):
        pool._process=Engine()
        pool._model=model
        launches.append(model)
    def respond(proc,timeout):
        request=json.loads(proc.stdin.writes[-1])
        for output in request.get('outputs', [request.get('output')]):
            out=Path(output)
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_bytes(b'RIFF' + b'0' * 128)
        return {'ok':True}
    monkeypatch.setattr(pool,'_load',load)
    monkeypatch.setattr(pool,'_response',respond)
    for model in ('cosyvoice3:base','cosyvoice3:base','cosyvoice3:rl'):
        pool.synthesize(model=model,voice='reference',quality={'cosy_mode':'cross_lingual', 'profile_id':'test-id', 'profile_sha256':'abc'},
                        items=[{'text':'中文测试', 'output':str(tmp_path/'tts.wav')}])
    assert launches==['cosyvoice3:base','cosyvoice3:rl']
    assert pool.state()['sentences']==3
    assert pool.state()['busy'] is False


def test_qwen_speaker_switch_does_not_reload_weights(tmp_path, monkeypatch):
    monkeypatch.setenv('DUBBING_OUTPUT_ROOT',str(tmp_path))
    pool=SpeechEnginePool()
    loads=[]
    class Engine:
        def __init__(self):
            class Input:
                def __init__(self):self.writes=[]
                def write(self,b):self.writes.append(b)
                def flush(self):pass
            self.stdin=Input()
        def poll(self):return None
    def load(model,voice,quality):
        loads.append(voice)
        pool._process=Engine()
        pool._model=model
    def respond(proc,timeout):
        request=json.loads(proc.stdin.writes[-1])
        Path(request['output']).write_bytes(b'RIFF'+b'0'*128)
        return {'ok':True}
    monkeypatch.setattr(pool,'_load',load)
    monkeypatch.setattr(pool,'_response',respond)
    model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice'
    for i,voice in enumerate(('Vivian','Serena','Eric')):
        pool.synthesize(model=model,voice=voice,quality={'temperature':0.9},
                        items=[{'text':'测试', 'output':str(tmp_path/f'{i}.wav')}])
    assert loads==['Vivian']
    assert pool.state()['sentences']==3



def test_manager_serializes_gpu_requests(tmp_path, monkeypatch):
    monkeypatch.setenv('DUBBING_OUTPUT_ROOT',str(tmp_path))
    pool=SpeechEnginePool()
    active=0
    peak=0
    lock=threading.Lock()
    class Engine:
        def poll(self):return None
        class Input:
            def __init__(self):self.writes=[]
            def write(self,data):self.writes.append(data)
            def flush(self):pass
        def __init__(self):self.stdin=self.Input()
    def fake_load(model,voice,quality):
        pool._process=Engine()
        pool._model=model
    def fake_respond(proc,timeout):
        nonlocal active,peak
        with lock:active+=1;peak=max(active,peak)
        time.sleep(0.03)
        request=json.loads(proc.stdin.writes[-1]);Path(request['output']).write_bytes(b'RIFF'+b'0'*128)
        with lock:active-=1
        return {'ok':True}
    monkeypatch.setattr(pool,'_load',fake_load)
    monkeypatch.setattr(pool,'_response',fake_respond)
    def run(i):
        return pool.synthesize(model='Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice',voice='Vivian',quality={},
            items=[{'text':f'语音{i}', 'output':str(tmp_path/f'{i}.wav')}])
    with ThreadPoolExecutor(max_workers=4) as executor:
        results=list(executor.map(run,range(4)))
    assert all(r['ok'] for r in results)
    assert peak==1
