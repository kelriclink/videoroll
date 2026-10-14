"""One internal VideoRoll dubbing service for all supported TTS models.

External web clients MUST use the authenticated Orchestrator APIs instead.
Only binds loopback; writes only under VideoRoll's own data directory.
"""
from __future__ import annotations
import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from videoroll.apps.dubbing.service_manager import pool

app = FastAPI(title='VideoRoll Dubbing Service', docs_url=None, redoc_url=None, openapi_url=None)


def _read_token() -> str:
    tokenfile = Path(os.environ.get('DUBBING_SERVICE_TOKEN_FILE', '/opt/videoroll/config/dubbing-service.token'))
    if not tokenfile.is_file():raise RuntimeError('dubbing service token has not been provisioned')
    token = tokenfile.read_text().strip()
    if len(token) < 32:raise RuntimeError('dubbing service token too short')
    return token


def authenticate(x_videoroll_token: str | None = Header(default=None)) -> None:
    try:expected = _read_token()
    except RuntimeError as exc:raise HTTPException(503, 'service authentication unavailable') from exc
    if x_videoroll_token is None or not hmac.compare_digest(x_videoroll_token, expected):
        raise HTTPException(401, 'invalid internal service credential')


class Item(BaseModel):
    text: str = Field(min_length=1, max_length=1200)
    output: str = Field(min_length=6, max_length=2048)
    voice: str = Field(default='', max_length=64)


class Synthesize(BaseModel):
    model: str = Field(min_length=4, max_length=180)
    voice: str = Field(min_length=1, max_length=64)
    quality: dict[str, Any] = Field(default_factory=dict)
    items: list[Item] = Field(min_length=1, max_length=4)
    timeout_seconds: float = Field(default=600.0, ge=10, le=1200)


@app.get('/health')
def health() -> dict[str, Any]:
    return {'ok': True, 'name': 'videoroll-dubbing', 'gpu_slots': 1,
            'model_loaded': pool.state()['loaded'], 'busy': pool.state()['busy']}


@app.get('/v1/status', dependencies=[Depends(authenticate)])
def status() -> dict[str, Any]:
    from videoroll.apps.dubbing.model_management import inventory
    return {'service': 'videoroll-dubbing', 'gpu_slots': 1,
            'engine': pool.state(), 'models': inventory()['models']}


@app.post('/v1/synthesize', dependencies=[Depends(authenticate)])
async def synthesize(request: Synthesize) -> dict[str, Any]:
    try:
        return await run_in_threadpool(pool.synthesize, model=request.model,
            voice=request.voice, quality=request.quality,
            items=[item.model_dump() for item in request.items],
            timeout=request.timeout_seconds)
    except ValueError as exc:raise HTTPException(409, str(exc)) from exc
    except (TimeoutError, OSError, RuntimeError) as exc:
        raise HTTPException(503, f'{type(exc).__name__}: {str(exc)[:300]}') from exc


@app.post('/v1/unload', dependencies=[Depends(authenticate)])
async def unload() -> dict[str, bool]:
    await run_in_threadpool(pool.unload)
    return {'unloaded': True}
