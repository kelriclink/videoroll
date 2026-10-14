"""Authorized, immutable short reference WAVs for zero-shot voice synthesis."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import uuid
import wave
from array import array
from videoroll.apps.dubbing.model_management import atomic_json, lock, read_json, root


def directory(base: Path | None = None) -> Path:
    return (base or root()) / 'voice_profiles'


def list_profiles(base: Path | None = None) -> list[dict]:
    result = read_json(directory(base) / 'profiles.json')
    items = result.get('profiles', [])
    return items if isinstance(items, list) else []


def resolve(profile_id: str, base: Path | None = None) -> tuple[dict, Path]:
    try:valid = str(uuid.UUID(profile_id))
    except (ValueError, TypeError):raise ValueError('invalid reference voice ID') from None
    for item in list_profiles(base):
        if item.get('id') == valid:
            reference = directory(base) / f'{valid}.wav'
            if not reference.is_file() or reference.stat().st_size < 4000:
                raise ValueError('reference WAV is no longer available')
            if hashlib.sha256(reference.read_bytes()).hexdigest() != item.get('audio_sha256'):
                raise ValueError('reference WAV content has changed')
            return item, reference
    raise ValueError('reference voice not found')


def create_profile(*, source: Path, task_id: uuid.UUID, asset_key: str,
                   name: str, start_seconds: float, duration_seconds: float,
                   reference_text: str, consent: bool, base: Path | None = None,
                   ffmpeg: str = 'ffmpeg') -> dict:
    if not consent:raise ValueError('explicit authorization to clone this voice is required')
    name = name.strip();reference_text = reference_text.strip()
    if not 1 <= len(name) <= 60 or len(reference_text) > 1200:
        raise ValueError('invalid voice name or reference transcript')
    if not math.isfinite(start_seconds) or not 0 <= start_seconds <= 48 * 3600:
        raise ValueError('invalid reference start time')
    if not math.isfinite(duration_seconds) or not 3 <= duration_seconds <= 30:
        raise ValueError('reference duration must be between 3 and 30 seconds')
    if not source.is_file():raise ValueError('source video is not available')
    base = base or root();location = directory(base)
    location.mkdir(mode=0o700, parents=True, exist_ok=True)
    pid = str(uuid.uuid4());dest = location / f'{pid}.wav'
    try:
        result = subprocess.run([
            ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
            '-ss', f'{start_seconds:.3f}', '-t', f'{duration_seconds:.3f}',
            '-i', str(source), '-vn', '-map', '0:a:0', '-ac', '1', '-ar', '16000',
            '-c:a', 'pcm_s16le', str(dest),
        ], timeout=110, capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError('failed to extract clean reference audio (no source audio track?)')
        with wave.open(str(dest), 'rb') as file:
            if file.getnchannels() != 1 or file.getframerate() != 16000 or file.getsampwidth() != 2:
                raise ValueError('invalid reference PCM format')
            length = file.getnframes() / 16000
            frames = file.readframes(file.getnframes())
        dest.chmod(0o600)
        if length < 2.5 or length > 30.1:raise ValueError('reference audio does not meet duration limits')
        values = array('h');values.frombytes(frames)
        rms = math.sqrt(sum(float(v)**2 for v in values[::8]) / max(1,len(values[::8]))) / 32768
        if rms < 0.002:raise ValueError('reference audio is nearly silent')
        record = {
            'id': pid, 'name': name, 'task_id': str(task_id),
            'source_asset_key': asset_key, 'start_seconds': start_seconds,
            'duration_seconds': round(length, 3), 'reference_text': reference_text,
            'audio_sha256': hashlib.sha256(dest.read_bytes()).hexdigest(),
            'authorization_confirmed': True,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        with lock(base):
            profiles = list_profiles(base)
            atomic_json(location / 'profiles.json', {'profiles': profiles + [record]})
            (location / 'profiles.json').chmod(0o600)
        return record
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def delete_profile(profile_id: str, base: Path | None = None) -> None:
    base = base or root()
    record, file = resolve(profile_id, base)
    with lock(base):
        profiles = list_profiles(base)
        atomic_json(directory(base) / 'profiles.json',
                    {'profiles': [p for p in profiles if p.get('id') != record['id']]})
        file.unlink(missing_ok=True)
