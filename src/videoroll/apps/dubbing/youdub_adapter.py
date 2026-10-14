"""VideoRoll integration of the original YouDub WebUI audio alignment engine.

Upstream licensed Apache-2.0, copied verbatim into
videoroll.vendor.youdub.adapters.audio, from liuzhao1225/YouDub-webui.
The upstream merge_tts_audio() (global/local stretch, gaps, timeline drift)
is called directly. No VideoRoll per-clip deadline/speed constraints remain.

The adapter only maps our existing speech units and saved WAVs to YouDub's
translation.json + 0001.wav.. inputs and validates its output.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import shutil
import wave
from pathlib import Path
from typing import Mapping, Sequence

from videoroll.apps.dubbing.timing import SpokenSegment


@dataclass(frozen=True)
class YouDubAlignment:
    audio_path: Path
    timings_path: Path
    audio_duration: float
    source_duration: float
    max_start_drift: float
    max_end_drift: float
    final_duration: float


def align_youdub(
    segments: Sequence[SpokenSegment],
    audio_files: Mapping[int, Path],
    *,
    source_duration: float,
    workspace: Path,
) -> YouDubAlignment:
    """Use upstream YouDub's *actual* audio merge, not a reimplementation.

    The source media and original synthesized WAVs are never mutated. The
    separate pilot directory can be removed/recreated without invalidating
    durable VideoRoll TTS caches.
    """
    if not math.isfinite(source_duration) or source_duration <= 0:
        raise ValueError('invalid source duration')
    if len(segments) < 1 or len(segments) > 2000:
        raise ValueError('invalid speech unit count')
    workspace = workspace / 'youdub'
    tts = workspace / 'segments' / 'tts'
    metadata = workspace / 'metadata'
    tts.mkdir(parents=True, exist_ok=True)
    metadata.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    last_start = -1.0
    for n, part in enumerate(segments, 1):
        if (not all(math.isfinite(x) for x in (part.start, part.end))
                or part.start + 0.001 < last_start
                or part.end <= part.start or part.end > source_duration + 0.01
                or not part.speech_text.strip()):
            raise ValueError(f'YouDub input has invalid source speech unit {part.index}')
        wav = audio_files.get(part.index)
        if wav is None or not wav.is_file():
            raise FileNotFoundError(f'YouDub input audio missing for {part.index}')
        dest = tts / f'{n:04d}.wav'
        # Compare against the source; do not reuse a stale output after a
        # manual edit, a new generation, or a different cache revision.
        if dest.exists():
            dest.unlink()
        try:
            dest.hardlink_to(wav)
        except OSError:
            shutil.copyfile(wav, dest)
        items.append({
            'start_time': round(part.start * 1000),
            'end_time': round(part.end * 1000),
            'dst': part.speech_text,
            'speaker': part.voice,
            'audio_mode': 'tts',
            'source_index': part.index,
        })
        last_start = part.start

    translation = metadata / 'translation.json'
    translation.write_text(json.dumps({'translation': items}, ensure_ascii=False),
                           encoding='utf-8')
    # YouDub skips cached outputs. Remove only this isolated merge stage's
    # derived files so that resume always respects the current inputs.
    for file in (workspace / 'tmp' / 'audio_dubbing.wav',
                 workspace / 'metadata' / 'timings.json'):
        file.unlink(missing_ok=True)
    # Import the upstream audio stack only in the dedicated dubbing worker.
    # VideoRoll API/maintenance services must not import NumPy/Librosa/Torch.
    from videoroll.vendor.youdub.adapters.audio import merge_tts_audio
    merged, timing = merge_tts_audio(translation, tts, workspace)
    with wave.open(str(merged), 'rb') as w:
        audio_duration = w.getnframes() / w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        sample_rate = w.getframerate()
    # YouDub omits trailing silence. Preserve that upstream WAV but provide
    # a complete full-duration audio asset to VideoRoll's publishing path.
    if audio_duration + 0.0001 < source_duration:
        padded = workspace / 'tmp' / 'video_length_audio.wav'
        with wave.open(str(merged), 'rb') as original, wave.open(str(padded), 'wb') as dst:
            dst.setparams(original.getparams())
            while chunk := original.readframes(32768):
                dst.writeframesraw(chunk)
            pending = max(0, round(source_duration * sample_rate) -
                          round(audio_duration * sample_rate))
            while pending > 0:
                n = min(pending, 32768)
                dst.writeframesraw(bytes(n * channels * width))
                pending -= n
        merged = padded
        audio_duration = round(source_duration * sample_rate) / sample_rate
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise ValueError('YouDub produced empty/malformed audio')
    actual = json.loads(timing.read_text(encoding='utf-8'))['translation']
    drift_start = max(abs(x['actual_start_time'] - x['start_time']) / 1000 for x in actual)
    drift_end = max(abs(x['actual_end_time'] - x['end_time']) / 1000 for x in actual)
    return YouDubAlignment(
        audio_path=merged,
        timings_path=timing,
        audio_duration=audio_duration,
        source_duration=source_duration,
        max_start_drift=drift_start,
        max_end_drift=drift_end,
        final_duration=max(source_duration, audio_duration,
                           float(actual[-1]['actual_end_time']) / 1000),
    )
