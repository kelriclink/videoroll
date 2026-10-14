"""Single-GPU engine scheduler shared by every VideoRoll dubbing workflow.

The service stays warm; engine-specific interpreters stay isolated. Only one
inference is active on V100, never two conflicting CUDA contexts/models.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
from threading import Lock
import time
from typing import Any

from videoroll.apps.dubbing.model_management import COSY_MODELS, QWEN_MODELS, folder, installed
from videoroll.apps.dubbing.cosyvoice3_adapter import (runtime_python, runtime_ready, cosine_runtime_environment)
from videoroll.apps.dubbing.voice_profiles import resolve


def _allowed_output(path: str) -> Path:
    allowed = Path(os.environ.get('DUBBING_OUTPUT_ROOT', '/DATA/videoroll/data/storage/work')).resolve()
    result = Path(path)
    if (not result.is_absolute() or result.suffix != '.wav' or
        not result.resolve().is_relative_to(allowed)):
        raise ValueError('output WAV must be inside VideoRoll work directory')
    return result


class SpeechEnginePool:
    """Lazy model load + strict serialization, one warm engine at a time."""
    def __init__(self):
        self._lock = Lock()
        self._process: subprocess.Popen | None = None
        self._key: str | None = None
        self._model: str | None = None
        self._busy = False
        self._sentences = 0
        self._failures = 0
        self._last_error = ''

    def state(self) -> dict[str, Any]:
        return {'model': self._model, 'loaded': self._process is not None and self._process.poll() is None,
                'busy': self._busy, 'sentences': self._sentences, 'failures': self._failures,
                'last_error': self._last_error}

    @staticmethod
    def _response(proc: subprocess.Popen, timeout: float) -> dict:
        assert proc.stdout is not None
        ready = select.poll()
        ready.register(proc.stdout.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
        if not ready.poll(int(timeout * 1000)):
            raise TimeoutError('TTS inference timed out')
        data = proc.stdout.readline()
        if not data:raise RuntimeError(f'TTS engine exited unexpectedly ({proc.poll()})')
        obj = json.loads(data)
        if not isinstance(obj, dict):raise RuntimeError('invalid TTS engine result')
        return obj

    def _stop(self) -> None:
        proc = self._process
        self._process = None
        self._key = self._model = None
        if proc is None:return
        if proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b'{"op":"close"}\n')
                    proc.stdin.flush()
                    proc.stdin.close()
                proc.wait(timeout=8)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                proc.terminate()
                try:proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        if proc.stdout:proc.stdout.close()

    def _load(self, model: str, voice: str, quality: dict[str, Any]) -> None:
        if not installed(model):raise ValueError('selected TTS model is not installed')
        if model in QWEN_MODELS:
            argv = [os.environ.get('DUBBING_QWEN_PYTHON', '/opt/videoroll/venvs/dubbing/bin/python'),
                    '-u', '-m', 'videoroll.apps.dubbing.qwen_bridge',
                    '--model', model, '--model-path', str(folder(model)),
                    '--voice', voice, '--device', 'cuda:0',
                    '--instruct', str(quality.get('instruct') or ''),
                    '--temperature', str(quality.get('temperature', 0.9)),
                    '--top-p', str(quality.get('top_p', 0.9))]
            env = {**os.environ, 'PYTHONPATH': os.environ.get('PYTHONPATH', '')}
        elif model in COSY_MODELS:
            if not runtime_ready():raise RuntimeError('CosyVoice 3 runtime is not ready')
            profile_id = str(quality.get('profile_id') or '')
            profile, reference = resolve(profile_id)
            if profile.get('audio_sha256') != quality.get('profile_sha256'):
                raise RuntimeError('reference voice fingerprint has changed')
            argv = [str(runtime_python()), '-u', '-m', 'videoroll.apps.dubbing.cosyvoice3_bridge',
                    '--model-root', str(folder(model)), '--variant', model.split(':')[1],
                    '--reference', str(reference), '--mode', str(quality.get('cosy_mode') or 'cross_lingual'),
                    '--prompt-text', str(profile.get('reference_text') or ''),
                    '--instruct', str(quality.get('instruct') or '')]
            env = cosine_runtime_environment()
        else:raise ValueError('unknown TTS engine')
        # Switch engines only after the prior CUDA subprocess exits; GPU freed.
        self._stop()
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=None, bufsize=0)
        self._process = proc
        try:
            greeting = self._response(proc, float(os.environ.get('DUBBING_MODEL_LOAD_TIMEOUT', '300')))
            if not greeting.get('ready'):
                raise RuntimeError(f'TTS model failed to load: {greeting.get("error", "unknown")}')
        except BaseException:
            self._stop()
            raise
        self._model = model

    def synthesize(self, *, model: str, voice: str, quality: dict[str, Any],
                   items: list[dict[str, str]], timeout: float = 600) -> dict[str, Any]:
        if not 1 <= len(items) <= 4:raise ValueError('TTS accepts 1–4 utterances')
        if model in COSY_MODELS and len(items) != 1:
            raise ValueError('CosyVoice3 does not support parallel texts yet')
        if model not in QWEN_MODELS + COSY_MODELS:
            raise ValueError('unsupported model')
        if model in COSY_MODELS:
            # Revalidate immutable voice identity on EVERY request, even when
            # the GPU has a warm copy. Deleting/changing a reference takes effect.
            voice_profile, _ = resolve(str(quality.get('profile_id') or ''))
            if voice_profile.get('audio_sha256') != quality.get('profile_sha256'):
                raise ValueError('reference voice fingerprint mismatch')
        texts, outputs, voices = [], [], []
        for item in items:
            text = item.get('text', '').strip()
            if not 0 < len(text) <= 1200:raise ValueError('text must contain 1–1200 characters')
            texts.append(text)
            outputs.append(str(_allowed_output(item.get('output', ''))))
            voices.append(item.get('voice') or voice)
        # Server trusts no user-derived path outside its confined storage.
        if model in QWEN_MODELS:
            # Qwen CustomVoice can switch speakers per sentence; changing
            # Vivian/Serena alone must not unload/reload 1.7B GPU weights.
            generation = {k: quality.get(k) for k in
                          ('instruct', 'temperature', 'top_p', '_model_revision')}
        else:
            # CosyVoice speaker prompt and mel-speed are session-specific.
            generation = {k: quality.get(k) for k in
                          ('profile_id','profile_sha256','cosy_mode','instruct','cosy_speed','_model_revision')}
        key_material = {'model': model, 'generation': generation}
        key = hashlib.sha256(json.dumps(key_material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with self._lock:
            self._busy = True
            try:
                if self._key != key or self._process is None or self._process.poll() is not None:
                    self._load(model, voice, quality)
                    self._key = key
                assert self._process is not None and self._process.stdin is not None
                request = ({'op': 'synthesize', 'text': texts[0], 'output': outputs[0],
                            'voice': voices[0], 'speed': float(quality.get('cosy_speed', 1.0))}
                           if len(items) == 1 else
                           {'op': 'batch', 'texts': texts, 'outputs': outputs, 'voices': voices})
                self._process.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode())
                self._process.stdin.flush()
                answer = self._response(self._process, min(timeout, 1200))
                if not answer.get('ok'):
                    raise RuntimeError(str(answer.get('error') or 'unknown engine error'))
                for name in outputs:
                    path = Path(name)
                    if not path.is_file() or path.stat().st_size < 44:
                        raise RuntimeError(f'TTS did not generate a valid WAV: {path.name}')
                self._sentences += len(items)
                return {'ok': True, 'model': model, 'outputs': outputs,
                        'sentences': len(items), 'service': 'videoroll-dubbing'}
            except Exception as exc:
                self._failures += 1
                self._last_error = f'{type(exc).__name__}: {str(exc)[:300]}'
                # A crashed/timeout CUDA process must be killed, otherwise
                # the next request may inherit an indefinitely wedged process.
                if (isinstance(exc, (TimeoutError, BrokenPipeError, OSError))
                    or 'out of memory' in str(exc).lower()
                    or (self._process is not None and self._process.poll() is not None)):
                    self._stop()
                raise
            finally:
                self._busy = False

    def unload(self) -> None:
        with self._lock:self._stop()


pool = SpeechEnginePool()
