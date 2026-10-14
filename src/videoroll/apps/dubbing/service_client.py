"""VideoRoll workflow adapter: TTS always goes through the unified service."""
from __future__ import annotations
import os
from pathlib import Path
import httpx
def _read_token() -> str:
    file = Path(os.environ.get('DUBBING_SERVICE_TOKEN_FILE', '/opt/videoroll/config/dubbing-service.token'))
    token = file.read_text().strip()
    if len(token) < 32:raise RuntimeError('internal dubbing service token unavailable')
    return token


def base_url() -> str:
    # Never point the model service at arbitrary remote hosts. The service is
    # local to the worker host until explicitly secured cross-node transport.
    value = os.environ.get('DUBBING_SERVICE_URL', 'http://127.0.0.1:8040').rstrip('/')
    if value not in ('http://127.0.0.1:8040', 'http://localhost:8040'):
        raise ValueError('dubbing service must use the local private listener')
    return value


class RemoteSpeechSynthesizer:
    def __init__(self, *, model_path: str, voice: str, quality: dict,
                 request_timeout: float = 750):
        self.model_path = model_path
        self.voice = voice
        self.quality = dict(quality)
        self.request_timeout = request_timeout
        self._client = httpx.Client(base_url=base_url(), timeout=httpx.Timeout(request_timeout, connect=5),
                                    headers={'X-VideoRoll-Token': _read_token()})

    def _send(self, items: list[dict]) -> None:
        try:
            result = self._client.post('/v1/synthesize', json={
                'model': self.model_path, 'voice': self.voice, 'quality': self.quality,
                'items': items, 'timeout_seconds': min(self.request_timeout - 5, 1200),
            })
            result.raise_for_status()
            outcome = result.json()
            if not outcome.get('ok'):raise RuntimeError('dubbing service did not complete synthesis')
        except httpx.HTTPError as exc:
            raise RuntimeError(f'unified dubbing service unavailable: {type(exc).__name__}: {str(exc)[:280]}') from exc

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        self._send([{'text': text, 'output': str(output), 'voice': voice or self.voice}])

    def synthesize_batch(self, texts: list[str], outputs: list[Path], *, voices: list[str]) -> None:
        if len(texts) != len(outputs) or len(texts) != len(voices):
            raise ValueError('invalid TTS batch sizes')
        self._send([{'text': text, 'output': str(output), 'voice': voice}
                    for text, output, voice in zip(texts, outputs, voices)])

    def close(self) -> None:
        self._client.close()


def unified_service_status() -> dict:
    """Bounded API proxy used by VideoRoll settings and operations UI."""
    try:
        result = httpx.get(base_url() + '/v1/status',
                           headers={'X-VideoRoll-Token': _read_token()}, timeout=2.5)
        result.raise_for_status()
        data = result.json()
        engine = data.get('engine') or {}
        return {'connected': True, 'model': engine.get('model'),
                'loaded': bool(engine.get('loaded')),
                'busy': bool(engine.get('busy')),
                'sentences': int(engine.get('sentences') or 0),
                'failures': int(engine.get('failures') or 0),
                'last_error': str(engine.get('last_error') or '')[:200],
                'gpu_slots': int(data.get('gpu_slots') or 1)}
    except (httpx.HTTPError, OSError, ValueError, RuntimeError):
        return {'connected': False, 'model': None, 'loaded': False,
                'busy': False, 'sentences': 0, 'failures': 0, 'gpu_slots': 1}
