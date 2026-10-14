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
from videoroll.apps.dubbing.timing import (SpokenSegment, plan_dubbing_timeline,
                                            check_segment_fit, DubbingTimingError,
                                            reconcile_final_subtitle_boundary)
from videoroll.apps.dubbing.quality import audio_cache_key
from videoroll.apps.dubbing.speech_units import build_speech_units


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


def segment_cache_key(segment: SpokenSegment, model_path: str,
                      *, quality: dict[str, Any] | None = None) -> str:
    if quality is not None:
        return audio_cache_key(text=segment.speech_text, voice=segment.voice,
                               model=model_path, quality=quality)
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
    on_check: Callable[[], None] | None = None,
    quality: dict[str, Any] | None = None,
    batch_size: int = 1,
    on_performance: Callable[[int, float, float], None] | None = None,
    on_timing_adjustment: Callable[[list[dict[str, float | int | str]]], None] | None = None,
    on_speech_units: Callable[[list[dict[str, Any]]], None] | None = None,
    mix_mode: str = "replace",
    preview_only: bool = False,
) -> DubbingResult:
    """Synthesize/cache fragments, align them, mux P2; don't modify P1."""
    workspace.mkdir(parents=True, exist_ok=True)
    segments = spoken_segments_from_json(
        json.loads(subtitle_json.read_text(encoding="utf-8")),
        voice=voice, speech_overrides=speech_overrides,
    )
    duration = probe_duration(subtitle_video, ffprobe=ffprobe)
    max_speed = 2.0 if (quality or {}).get("debug_timing") is True else 1.25
    segments, adjustments = reconcile_final_subtitle_boundary(
        segments, video_duration=duration,
    )
    if adjustments and on_timing_adjustment is not None:
        on_timing_adjustment(adjustments)
    # Validate the original translated subtitle chronology before grouping:
    # grouping must never conceal broken source timestamps.
    plan_dubbing_timeline(
        segments, {p.index: max(0.01, (p.end-p.start) * 0.5) for p in segments},
        video_duration=duration, max_speed=max_speed,
    )
    units=build_speech_units(segments,
        enabled=bool((quality or {}).get("auto_group_speech", False)))
    segments=[unit.part for unit in units]
    if on_speech_units is not None:
        on_speech_units([{
            "index": unit.part.index, "source_indices": list(unit.source_indices),
            "start": round(unit.part.start, 3), "end": round(unit.part.end, 3),
            "speech_text": unit.part.speech_text,
            "duplicate_removed": unit.duplicate_removed,
        } for unit in units])
    # Early structural validation: reject invalid source timestamps before
    # spending hours on inference. Dummy durations test only intervals.
    plan_dubbing_timeline(
        segments, {s.index: max(0.01, (s.end - s.start) * 0.5) for s in segments},
        video_duration=duration, max_speed=max_speed,
    )
    if retain_original_audio or mix_mode in ("original_low", "background_only"):
        audio_probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=index", "-of", "csv=p=0",
             str(subtitle_video)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        original_audio_present = bool(audio_probe.stdout.strip())
        if not original_audio_present:
            raise DubbingTimingError("selected audio mix requires an original audio stream")
    audio_files: dict[int, Path] = {}
    tts_durations: dict[int, float] = {}
    keys: dict[str, str] = {}
    import time
    if batch_size not in (1, 2, 4):
        raise ValueError("batch_size must be 1, 2 or 4")
    if preview_only:
        batch_size = 1
    for start in range(0, len(segments), batch_size):
        if on_check:
            on_check()
        group = segments[start:start + batch_size]
        missing: list[tuple[SpokenSegment, Path]] = []
        for item in group:
            key = segment_cache_key(item, synthesizer.model_path, quality=quality)
            file = workspace / "segments" / f"{key}.wav"
            if not file.is_file() or file.stat().st_size <= 44:
                missing.append((item, file))
        began = time.perf_counter()
        # Explicitly batch only completely missing groups. Mixed cached/new
        # groups use the safe single-item route to preserve resumability.
        if len(missing) > 1 and len(missing) == len(group) and hasattr(synthesizer, "synthesize_batch"):
            for _item, file in missing:
                file.parent.mkdir(parents=True, exist_ok=True)
            getattr(synthesizer, "synthesize_batch")(
                [item.speech_text for item, _ in missing],
                [file for _, file in missing],
                voices=[item.voice for item, _ in missing],
            )
        else:
            for item, file in missing:
                if on_check:
                    on_check()
                file.parent.mkdir(parents=True, exist_ok=True)
                partial = file.with_name(f"{file.stem}.{os.getpid()}.{uuid.uuid4().hex}.pending.wav")
                try:
                    synthesizer.synthesize(item.speech_text, partial, voice=item.voice)
                    if wav_duration(partial) <= 0:
                        raise DubbingTimingError(f"segment {item.index}: TTS returned silence")
                    partial.replace(file)
                finally:
                    partial.unlink(missing_ok=True)
        elapsed = time.perf_counter() - began
        audio_generated = 0.0
        for n, item in enumerate(group, start=start + 1):
            if on_check:
                on_check()
            key = segment_cache_key(item, synthesizer.model_path, quality=quality)
            file = workspace / "segments" / f"{key}.wav"
            length = wav_duration(file)
            audio_files[item.index] = file
            tts_durations[item.index] = length
            keys[str(item.index)] = key
            audio_generated += length
            if on_segment_done is not None:
                on_segment_done(n, len(segments), key, file)
            # Persist the clip first, then flag a too-long line for review.
            # Resuming after a rewrite can reuse all prior valid clips.
            check_segment_fit(segments, n - 1, length, video_duration=duration,
                              max_speed=max_speed)
            if preview_only and n == 1:
                from videoroll.apps.dubbing.control import DubbingInterrupted
                raise DubbingInterrupted("preview_ready")
        if on_performance is not None:
            on_performance(len(group), elapsed, audio_generated)

    if on_stage is not None:
        on_stage("aligning", 88)
    placements = plan_dubbing_timeline(
        segments, tts_durations, video_duration=duration,
        max_speed=max_speed,
    )
    if on_stage is not None:
        on_stage("mixing", 90)
    track = workspace / "chinese_audio.wav"
    assemble_track(placements, audio_files, track, video_duration=duration, ffmpeg_path=ffmpeg)
    p2 = workspace / "dubbed.mp4"
    stem = None
    if mix_mode == "background_only":
        from videoroll.apps.dubbing.separation import background_stem
        if on_check:
            on_check()
        stem = background_stem(subtitle_video, workspace / "separation", ffmpeg=ffmpeg)
        if on_check:
            on_check()
    elif mix_mode not in ("replace", "original_low"):
        raise ValueError("invalid mix mode")
    command = build_dubbed_part_command(
        ffmpeg, subtitle_video, track, p2, video_duration=duration,
        original_audio_present=original_audio_present,
        retain_original_audio=(mix_mode == "original_low" or retain_original_audio),
        background_track=stem,
    )
    subprocess.run(command, check=True, capture_output=True, timeout=max(120, int(duration * 4)))
    if not p2.is_file() or not p2.stat().st_size:
        raise RuntimeError("P2 mux generated no video file")
    output_duration = probe_duration(p2, ffprobe=ffprobe)
    if abs(output_duration - duration) > 0.10:
        raise DubbingTimingError(f"P2 duration mismatch ({output_duration} vs {duration})")
    return DubbingResult(track, p2, duration, len(segments), keys)
