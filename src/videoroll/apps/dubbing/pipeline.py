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
from dataclasses import dataclass, replace
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from videoroll.apps.dubbing.audio import wav_duration
from videoroll.apps.dubbing.mux import build_dubbed_part_command
from videoroll.apps.dubbing.pauses import PauseResult, prepare_long_pauses, prepared_name
from videoroll.apps.dubbing.youdub_adapter import align_youdub
from videoroll.apps.dubbing.timing import (
    SpokenSegment, DubbingTimingError, reconcile_final_subtitle_boundary,
    estimate_spoken_seconds, speech_slot_seconds,
)
from videoroll.apps.dubbing.quality import audio_cache_key
from videoroll.apps.dubbing.speech_units import (SpeechUnit, build_speech_units,
    apply_speech_unit_overrides)


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
    on_segment_prepared: Callable[[str, Path, PauseResult], None] | None = None,
    speech_overrides: Mapping[str, str] | None = None,
    on_stage: Callable[[str, int], None] | None = None,
    on_check: Callable[[], None] | None = None,
    quality: dict[str, Any] | None = None,
    batch_size: int = 1,
    on_performance: Callable[[int, float, float], None] | None = None,
    on_timing_adjustment: Callable[[list[dict[str, float | int | str]]], None] | None = None,
    on_speech_units: Callable[[list[dict[str, Any]]], None] | None = None,
    speech_unit_overrides: Mapping[str, object] | None = None,
    on_auto_rewrite: Callable[[SpeechUnit, float, float], str | None] | None = None,
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
    max_speed = (quality or {}).get('max_audio_speed')
    if max_speed is None:
        max_speed = 2.0 if (quality or {}).get('debug_timing') is True else 1.25
    max_speed = float(max_speed)
    if not 1.0 <= max_speed <= 2.5:
        raise DubbingTimingError('invalid maximum post-TTS audio speed')
    segments, adjustments = reconcile_final_subtitle_boundary(
        segments, video_duration=duration,
    )
    if adjustments and on_timing_adjustment is not None:
        on_timing_adjustment(adjustments)
    # Validate chronology only. YouDub permits overlapping caption windows
    # and schedules utterances in sequence after the preceding voice.
    previous_start = -1.0
    for part in segments:
        if (not isfinite(part.start) or not isfinite(part.end)
                or part.start < 0 or part.start + 1e-6 < previous_start
                or part.end <= part.start or part.end > duration + 0.02):
            raise DubbingTimingError(
                f'segment {part.index}: invalid or unsorted source interval '
                f'({part.start:.3f}..{part.end:.3f}, source {duration:.3f}s)'
            )
        previous_start = part.start
    units = apply_speech_unit_overrides(build_speech_units(
        segments, enabled=bool((quality or {}).get('auto_group_speech', False))),
        speech_unit_overrides)
    segments = [unit.part for unit in units]

    def report_speech_units() -> None:
        if on_speech_units is not None:
            on_speech_units([{
                'index': unit.part.index, 'source_indices': list(unit.source_indices),
                'start': round(unit.part.start, 3), 'end': round(unit.part.end, 3),
                'speech_text': unit.part.speech_text,
                'slot_seconds': round(speech_slot_seconds(segments, i, video_duration=duration), 3),
                'estimated_audio_seconds': estimate_spoken_seconds(unit.part.speech_text),
                'estimated_overlong': (estimate_spoken_seconds(unit.part.speech_text) >
                    speech_slot_seconds(segments, i, video_duration=duration) * max_speed),
            } for i, unit in enumerate(units)])

    report_speech_units()
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
    def effective_audio(raw_file: Path, key: str) -> tuple[Path, float]:
        # Keep original TTS immutable. For CosyVoice3, pathological multi-
        # second low-energy gaps are removed in a separately versioned WAV.
        if not synthesizer.model_path.startswith('cosyvoice3:'):
            return raw_file, wav_duration(raw_file)
        prepared = prepare_long_pauses(
            raw_file, workspace / 'segments' / 'prepared' / prepared_name(key),
            ffmpeg=ffmpeg,
        )
        if prepared.pauses_shortened and on_segment_prepared is not None:
            on_segment_prepared(key, prepared.path, prepared)
        return prepared.path, prepared.prepared_seconds

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
        audio_generated = 0.0
        for n, item in enumerate(group, start=start + 1):
            if on_check:
                on_check()
            key = segment_cache_key(item, synthesizer.model_path, quality=quality)
            file = workspace / "segments" / f"{key}.wav"
            # Upload original first, then publish a separate derived WAV.
            # This keeps the original available even when the fit check fails.
            audio_generated += wav_duration(file)
            if on_segment_done is not None:
                on_segment_done(n, len(segments), key, file)
            file, length = effective_audio(file, key)
            audio_files[item.index] = file
            tts_durations[item.index] = length
            keys[str(item.index)] = key
            # YouDub alignment is not restricted to this subtitle's original
            # time slot. Complete TTS for every unit; the upstream global/local
            # mixer schedules late clips after the preceding voice.
            if preview_only and n == 1:
                from videoroll.apps.dubbing.control import DubbingInterrupted
                raise DubbingInterrupted("preview_ready")
        if on_performance is not None:
            on_performance(len(group), time.perf_counter() - began, audio_generated)

    if on_stage is not None:
        on_stage("aligning", 88)
    if on_check:
        on_check()
    # Use YouDub's original merge_tts_audio: global stretch, local stretch,
    # gap padding, and time drift. No per-clip SpeechTooLong gate remains.
    aligned = align_youdub(segments, audio_files, source_duration=duration,
                           workspace=workspace)
    if on_stage is not None:
        on_stage("mixing", 90)
    track = aligned.audio_path
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
        ffmpeg, subtitle_video, track, p2, video_duration=aligned.final_duration,
        source_video_duration=duration,
        original_audio_present=original_audio_present,
        retain_original_audio=(mix_mode == "original_low" or retain_original_audio),
        background_track=stem,
    )
    subprocess.run(command, check=True, capture_output=True, timeout=max(120, int(duration * 4)))
    if not p2.is_file() or not p2.stat().st_size:
        raise RuntimeError("P2 mux generated no video file")
    output_duration = probe_duration(p2, ffprobe=ffprobe)
    if abs(output_duration - aligned.final_duration) > 0.25:
        raise DubbingTimingError(
            f'YouDub P2 duration mismatch ({output_duration:.3f} vs '
            f'{aligned.final_duration:.3f})')
    return DubbingResult(track, p2, output_duration, len(segments), keys)
