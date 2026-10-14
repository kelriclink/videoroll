"""Contract tests without downloading or executing GPU model weights."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import uuid
from unittest.mock import patch
import pytest
from videoroll.apps.dubbing.cosyvoice3_adapter import COSYVOICE_MODELS, CosyVoice3Synthesizer, runtime_ready
from videoroll.apps.dubbing.cosyvoice3_bridge import _rl_view
from videoroll.apps.dubbing.quality import normalize_quality, audio_cache_key
from videoroll.apps.dubbing.model_management import MODELS, installed, folder, state_file, status, choose
from videoroll.apps.dubbing.voice_profiles import create_profile, delete_profile, list_profiles, resolve


def _fake_model(base: Path):
    d=base/'Fun-CosyVoice3-0.5B-2512';d.mkdir()
    for name in ('cosyvoice3.yaml','llm.pt','llm.rl.pt','flow.pt','hift.pt',
                 'campplus.onnx','speech_tokenizer_v3.onnx','CosyVoice-BlankEN/model.safetensors'):
        file=d/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'a'*4096)
    (d/'.videoroll-complete.json').write_text('{"repo":"official"}')
    return d


def test_base_and_rl_share_weights_without_mutating_base(tmp_path):
    d=_fake_model(tmp_path)
    assert all(installed(k,tmp_path) for k in COSYVOICE_MODELS)
    assert folder('cosyvoice3:base',tmp_path) == folder('cosyvoice3:rl',tmp_path)
    assert state_file('cosyvoice3:base',tmp_path) == state_file('cosyvoice3:rl',tmp_path)
    view=_rl_view(d)
    assert (view/'llm.pt').resolve()==(d/'llm.rl.pt').resolve()
    assert (d/'llm.pt').read_bytes()==b'a'*4096
    assert (view/'CosyVoice-BlankEN/model.safetensors').is_file()
    assert (view/'cosyvoice3.yaml').is_file()
    assert status('cosyvoice3:base',tmp_path)['installed']
    assert choose('cosyvoice3:rl','reference','cuda:0',tmp_path)['model']=='cosyvoice3:rl'
    with pytest.raises(ValueError,match='cuda:0'):
        choose('cosyvoice3:rl','reference','cpu',tmp_path)


def test_missing_rl_or_incomplete_snapshot_not_reported_installed(tmp_path):
    d=_fake_model(tmp_path);(d/'llm.rl.pt').unlink()
    assert installed('cosyvoice3:base',tmp_path)
    assert not installed('cosyvoice3:rl',tmp_path)
    (d/'.videoroll-complete.json').unlink()
    assert not installed('cosyvoice3:base',tmp_path)


def test_reference_audio_from_authorized_video(tmp_path):
    source=tmp_path/'video.mp4'
    subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-y',
        '-f','lavfi','-i','color=c=black:s=160x90:d=7:r=25',
        '-f','lavfi','-i','sine=frequency=400:duration=7',
        '-c:v','mpeg4','-c:a','aac',str(source)],check=True)
    tid=uuid.uuid4()
    with pytest.raises(ValueError,match='authorization'):
        create_profile(source=source,task_id=tid,asset_key='raw/video.mp4',name='Test',
                       start_seconds=0,duration_seconds=5,reference_text='test',consent=False,base=tmp_path)
    record=create_profile(source=source,task_id=tid,asset_key='raw/video.mp4',name='Test',
                          start_seconds=0,duration_seconds=5,reference_text='reference transcript',
                          consent=True,base=tmp_path)
    assert 4.9<record['duration_seconds']<5.1
    assert record['authorization_confirmed']
    assert len(list_profiles(tmp_path))==1
    profile,wav=resolve(record['id'],tmp_path)
    assert profile['audio_sha256']==record['audio_sha256'] and wav.is_file()
    delete_profile(record['id'],tmp_path)
    assert list_profiles(tmp_path)==[]
    assert not wav.exists()


def test_cosyspeed_and_profile_affect_cache():
    kwargs=dict(model='cosyvoice3:rl',profile_id=str(uuid.uuid4()),profile_sha256='a'*64)
    base=normalize_quality(**kwargs)
    changed=normalize_quality(**kwargs,cosy_speed=1.2)
    a=audio_cache_key(text='测试',voice='reference',model='cosyvoice3:rl',quality=base)
    b=audio_cache_key(text='测试',voice='reference',model='cosyvoice3:rl',quality=changed)
    assert a!=b
    assert a!=audio_cache_key(text='测试',voice='reference',model='cosyvoice3:base',quality=base)
    assert a!=audio_cache_key(text='测试',voice='reference',model='cosyvoice3:rl',quality={**base,'profile_sha256':'b'*64})
    with pytest.raises(ValueError,match='sequential'):
        normalize_quality(**kwargs,batch_size=2)


def test_isolated_runtime_requirement_can_be_detected(tmp_path,monkeypatch):
    monkeypatch.setenv('COSYVOICE3_PYTHON',str(tmp_path/'absent-python'))
    assert not runtime_ready()
    with pytest.raises(RuntimeError,match='not installed'):
        CosyVoice3Synthesizer(model_path='cosyvoice3:base',model_dir=tmp_path,
                              reference=tmp_path/'missing.wav')


def test_cosy_runtime_rejects_unverified_gpu_inference(tmp_path, monkeypatch):
    from videoroll.apps.dubbing.cosyvoice3_adapter import runtime_ready
    venv = tmp_path / 'venv'
    py = venv / 'bin/python'
    py.parent.mkdir(parents=True)
    py.write_text('fake executable')
    (venv / '.runtime-ready').write_text('deps installed')
    repo = tmp_path / 'src'
    (repo / 'cosyvoice/cli').mkdir(parents=True)
    (repo / 'cosyvoice/cli/cosyvoice.py').write_text('module')
    (repo / 'third_party/Matcha-TTS').mkdir(parents=True)
    monkeypatch.setenv('COSYVOICE3_PYTHON', str(py))
    monkeypatch.setenv('COSYVOICE3_REPO', str(repo))
    assert runtime_ready() is False
    (venv / '.v100-base-rl-validated').write_text('both variants tested')
    assert runtime_ready() is True
