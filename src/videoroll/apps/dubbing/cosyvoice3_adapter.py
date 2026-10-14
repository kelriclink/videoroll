"""Isolated, warm-per-job CosyVoice3 bridge for VideoRoll's TTS protocol."""
from __future__ import annotations
import json
import os
from pathlib import Path
import select
import subprocess

COSYVOICE_MODELS = ('cosyvoice3:base', 'cosyvoice3:rl')
COSYVOICE_REPO = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'
COSYVOICE_FOLDER = 'Fun-CosyVoice3-0.5B-2512'


def runtime_python() -> Path:
    return Path(os.environ.get('COSYVOICE3_PYTHON', '/opt/videoroll/venvs/cosyvoice3/bin/python'))


def source_repo() -> Path:
    return Path(os.environ.get('COSYVOICE3_REPO', '/opt/videoroll/external/CosyVoice'))


def runtime_ready() -> bool:
    # Package installation alone does not prove an SM7.0 V100 can generate
    # valid audio in BOTH Base and RL modes. Operators add this marker only
    # after a real two-variant WAV smoke test (finite samples, non-silence).
    return (runtime_python().is_file()
            and (runtime_python().parent.parent / '.runtime-ready').is_file()
            and (runtime_python().parent.parent / '.v100-base-rl-validated').is_file()
            and (source_repo() / 'cosyvoice/cli/cosyvoice.py').is_file()
            and (source_repo() / 'third_party/Matcha-TTS').is_dir())


class CosyVoice3Synthesizer:
    def __init__(self, *, model_path: str, model_dir: Path, reference: Path,
                 prompt_text: str = '', mode: str = 'cross_lingual',
                 instruct: str = '', speed: float = 1.0, fp16: bool = False,
                 startup_timeout: float = 240, request_timeout: float = 600):
        if model_path not in COSYVOICE_MODELS:
            raise ValueError('unsupported CosyVoice3 variant')
        if mode not in ('cross_lingual', 'zero_shot', 'instruct'):
            raise ValueError('unsupported clone mode')
        if not 0.8 <= speed <= 1.25:raise ValueError('CosyVoice3 speed must be 0.8–1.25')
        if not runtime_ready():raise RuntimeError('CosyVoice3 Python 3.10 runtime is not installed')
        if not reference.is_file():raise ValueError('voice reference audio is missing')
        self.model_path = model_path
        self.speed = speed
        self.request_timeout = request_timeout
        # Stderr is intentionally inherited by the Hatchet worker journald;
        # stdout exclusively carries line-framed JSON responses.
        self._proc = subprocess.Popen([
            str(runtime_python()), '-u', '-m', 'videoroll.apps.dubbing.cosyvoice3_bridge',
            '--model-root', str(model_dir), '--variant', model_path.split(':')[1],
            '--reference', str(reference), '--mode', mode, '--prompt-text', prompt_text,
            '--instruct', instruct, *(['--fp16'] if fp16 else []),
        ], env={**os.environ, 'PYTHONPATH': f'{source_repo()}:{source_repo()}/third_party/Matcha-TTS:{os.environ.get("PYTHONPATH", "")}'},
           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        try:
            reply = self._read(startup_timeout)
            if not reply.get('ready'):
                raise RuntimeError(f'CosyVoice3 model initialization failed: {reply.get("error", "unknown")}')
            self.sample_rate = int(reply['sample_rate'])
        except BaseException:
            self.close()
            raise

    def _read(self, timeout: float) -> dict:
        assert self._proc.stdout is not None
        watch = select.poll()
        watch.register(self._proc.stdout.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
        if not watch.poll(int(timeout * 1000)):
            raise TimeoutError('CosyVoice3 inference exceeded deadline')
        line = self._proc.stdout.readline()
        if not line:raise RuntimeError(f'CosyVoice3 inference subprocess exited ({self._proc.poll()})')
        reply = json.loads(line)
        if not isinstance(reply, dict): raise RuntimeError('invalid CosyVoice3 protocol response')
        return reply

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        if self._proc.poll() is not None:raise RuntimeError('CosyVoice3 subprocess is not running')
        assert self._proc.stdin is not None
        request = {'op': 'synthesize', 'text': text, 'output': str(output), 'speed': self.speed}
        self._proc.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode())
        self._proc.stdin.flush()
        reply = self._read(self.request_timeout)
        if not reply.get('ok'):
            raise RuntimeError(f'CosyVoice3 synthesis failed: {reply.get("error", "unknown")}')
        if not output.is_file() or output.stat().st_size < 44:
            raise RuntimeError('CosyVoice3 subprocess did not save a WAV')

    def close(self) -> None:
        proc = getattr(self, '_proc', None)
        if proc is None:return
        if proc.poll() is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b'{"op":"close"}\n')
                    proc.stdin.flush()
                    proc.stdin.close()
                proc.wait(timeout=8)
            except (OSError, BrokenPipeError, subprocess.TimeoutExpired):
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill();proc.wait(timeout=3)
        if proc.stdout is not None:proc.stdout.close()
        self._proc = None

    def __del__(self) -> None:
        try:self.close()
        except Exception:pass
