"""Run sentence-level synthesis and build the dubbed P2 media in one workspace.

All inputs are explicit, immutable storage paths. The caller owns the DB
transaction, durable checkpoint and asset registration; this module never
publishes a Bilibili archive or edits the original subtitled P1.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from videoroll.apps.dubbing.audio import assemble_track, wav_duration
from videoroll.apps.dubbing.mux import build_dubbed_part_command
from videoroll.apps.dubbing.timing import SpokenSegment, plan_dubbing_timeline, DubbingTimingError


class SpeechSynthesizer(Protocol):
    model_path: str

    def synthesize(self, text: str, output: Path, *, voice: str | None = None) -> None:
        ...


@dataclass(frozen=True)
class DubbingResult:
    audio_path: Path
    video_path: Path
    source_duration: float
    segments_total: int
    completed_keys: dict[str, str]


def probe_duration(path: Path, *, ffprobe: str = "ffprobe") -> float:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise DubbingTimingError(f"media duration unavailable: {path}") from exc
    if not 0 < duration < 48 * 3600:
        raise DubbingTimingError("invalid media duration")
    return duration


def spoken_segments_from_json(
    raw: Any, *, voice: str, speech_overrides: Mapping[str, str] | None = None,
) -> list[SpokenSegment]:
    if not isinstance(raw, list) or not raw:
        raise DubbingTimingError("translated segment JSON must be a nonempty list")
    output: list[SpokenSegment] = []
    if len(raw) > 25000:
        raise DubbingTimingError("too many subtitle segments")
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise DubbingTimingError(f"segment {index}: invalid JSON")
        caption = str(item.get("text") or "").strip()
        override = (speech_overrides or {}).get(str(index))
        speech = str(override if override is not None else item.get("speech_text") or caption).strip()
        if not speech or not caption:
            raise DubbingTimingError(f"segment {index}: missing translated text")
        if len(speech) > 1000:
            raise DubbingTimingError(f"segment {index}: excessive spoken text")
        output.append(
            SpokenSegment(
                index=index, start=float(item["start"]), end=float(item["end"]),
                subtitle_text=caption, speech_text=speech, voice=voice,
            )
        )
    return output


def segment_cache_key(segment: SpokenSegment, model_path: str) -> str:
    source = json.dumps({
        "schema": 1, "text": segment.speech_text,
        "voice": segment.voice, "model": model_path, "language": "Chinese",
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def generate_dubbed_media(
    subtitle_video: Path,
    subtitle_json: Path,
    workspace: Path,
    synthesizer: SpeechSynthesizer,
    *,
    voice: str = "Vivian",
    ffmpeg: str = "ffmpeg",
    ffprobe: str = "ffprobe",
    retain_original_audio: bool = False,
    original_audio_present: bool = True,
    on_segment_done: Callable[[int, int, str, Path], None] | None = None,
    speech_overrides: Mapping[str, str] | None = None,
    on_stage: Callable[[str, int], None] | None = None,
) -> DubbingResult:
    """Synthesize/cache fragments, align them, mux P2; don't modify P1."""
    workspace.mkdir(parents=True, exist_ok=True)
    segments = spoken_segments_from_json(
        json.loads(subtitle_json.read_text(encoding="utf-8")),
        voice=voice, speech_overrides=speech_overrides,
    )
    duration = probe_duration(subtitle_video, ffprobe=ffprobe)
    audio_files: dict[int, Path] = {}
    tts_durations: dict[int, float] = {}
    keys: dict[str, str] = {}
    for n, item in enumerate(segments, start=1):
        key = segment_cache_key(item, synthesizer.model_path)
        segment_file = workspace / "segments" / f"{key}.wav"
        if not segment_file.is_file() or segment_file.stat().st_size <= 44:
            segment_file.parent.mkdir(parents=True, exist_ok=True)
            partial = segment_file.with_name(
                f"{segment_file.stem}.{os.getpid()}.{uuid.uuid4().hex}.pending.wav"
            )
            partial.unlink(missing_ok=True)
            try:
                synthesizer.synthesize(item.speech_text, partial, voice=item.voice)
                sample_duration = wav_duration(partial)
                if sample_duration <= 0:
                    raise DubbingTimingError(f"segment {item.index}: TTS returned silence")
                partial.replace(segment_file)
            finally:
                partial.unlink(missing_ok=True)
        audio_files[item.index] = segment_file
        tts_durations[item.index] = wav_duration(segment_file)
        keys[str(item.index)] = key
        if on_segment_done is not None:
            on_segment_done(n, len(segments), key, segment_file)

    if on_stage is not None:
        on_stage("aligning", 88)
    placements = plan_dubbing_timeline(
        segments, tts_durations, video_duration=duration,
    )
    if on_stage is not None:
        on_stage("mixing", 90)
    track = workspace / "chinese_audio.wav"
    assemble_track(placements, audio_files, track, video_duration=duration, ffmpeg_path=ffmpeg)
    p2 = workspace / "dubbed.mp4"
    command = build_dubbed_part_command(
        ffmpeg, subtitle_video, track, p2, video_duration=duration,
        original_audio_present=original_audio_present,
        retain_original_audio=retain_original_audio,
    )
    subprocess.run(command, check=True, capture_output=True, timeout=max(120, int(duration * 4)))
    if not p2.is_file() or not p2.stat().st_size:
        raise RuntimeError("P2 mux generated no video file")
    output_duration = probe_duration(p2, ffprobe=ffprobe)
    if abs(output_duration - duration) > 0.10:
        raise DubbingTimingError(f"P2 duration mismatch ({output_duration} vs {duration})")
    return DubbingResult(track, p2, duration, len(segments), keys)
