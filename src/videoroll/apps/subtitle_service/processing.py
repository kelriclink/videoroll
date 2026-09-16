from __future__ import annotations

from array import array
import base64
from difflib import SequenceMatcher
import io
import inspect
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import wave
from contextlib import nullcontext
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import httpx

from videoroll.ai.client import (
    OpenAIChatConfig,
    create_openai_http_client,
    request_openai_json_object,
    request_openai_json_object_with_thinking,
)
from videoroll.ai.service import AIService
from videoroll.utils.openai_compat import build_openai_audio_transcriptions_url

logger = logging.getLogger(__name__)

_FW_VAD_MIN_SILENCE_MS = 500
_FW_VAD_SPEECH_PAD_MS = 180
_FW_NO_SPEECH_THRESHOLD = 0.45
_FW_LOG_PROB_THRESHOLD = -0.8
_FW_COMPRESSION_RATIO_THRESHOLD = 2.2
_ASR_SILENCE_PEAK_THRESHOLD = 0.005
_ASR_SILENCE_RMS_THRESHOLD = 0.0008
_ASR_SILENCE_ACTIVE_THRESHOLD = 0.015
_ASR_SILENCE_ACTIVE_RATIO_THRESHOLD = 0.0005
_ASR_SAMPLE_RATE = 16000
_ASR_PCM_READ_FRAMES = 65536
# Bound PCM, VAD and feature-extraction memory independently of video length.
# The overlap preserves speech at an outer window boundary; captions are
# reconciled with the same timeline logic used for the remote ASR chunks.
_LOCAL_ASR_WINDOW_SECONDS = 120.0
_LOCAL_ASR_OVERLAP_SECONDS = 5.0
_OPENVINO_VAD_THRESHOLD = 0.5
_OPENVINO_VAD_MIN_SPEECH_MS = 250
_OPENVINO_VAD_MAX_SPEECH_SECONDS = 30.0
_OPENVINO_VAD_MIN_SILENCE_MS = 500
_OPENVINO_VAD_SPEECH_PAD_MS = 180
_OPENVINO_PIPELINE_CACHE: dict[tuple[str, str], Any] = {}
_OPENVINO_PIPELINE_CACHE_LOCK = threading.Lock()
_GROQ_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
# Groq accepts FLAC and its synchronous endpoint is more reliable when each
# request contains a short, fixed amount of audio. FLAC roughly halves the
# upload size of 16 kHz mono PCM without sacrificing ASR quality or timestamps.
_GROQ_CHUNK_SECONDS = 45.0
_GROQ_CHUNK_OVERLAP_SECONDS = 5.0
_GROQ_MAX_REQUEST_ATTEMPTS = 5
_GROQ_RETRY_BASE_SECONDS = 3.0
_GROQ_RETRY_MAX_SECONDS = 45.0
_GROQ_CHECKPOINT_VERSION = 2


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None
    secondary_text: str | None = None


def segment_to_dict(seg: Segment) -> dict[str, Any]:
    data: dict[str, Any] = {
        "start": float(seg.start),
        "end": float(seg.end),
        "text": str(seg.text or "").strip(),
    }
    if seg.confidence is not None:
        data["confidence"] = seg.confidence
    secondary = str(seg.secondary_text or "").strip()
    if secondary:
        data["secondary_text"] = secondary
    return data


def segments_to_json_data(segments: Iterable[Segment]) -> list[dict[str, Any]]:
    return [segment_to_dict(seg) for seg in segments]


def segments_from_json_data(data: Any) -> list[Segment]:
    if not isinstance(data, list):
        return []

    out: list[Segment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        secondary_text = str(item.get("secondary_text") or "").strip() or None
        if not text and not secondary_text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or 0.0)
        out.append(
            Segment(
                start=start,
                end=end,
                text=text,
                confidence=item.get("confidence"),
                secondary_text=secondary_text,
            )
        )
    return out


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


@lru_cache(maxsize=8)
def _ffmpeg_supported_encoders(ffmpeg_path: str) -> frozenset[str]:
    try:
        proc = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return frozenset()

    encoders: set[str] = set()
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Encoders:") or line.startswith("------"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            encoders.add(parts[1].strip())
    return frozenset(encoders)


def _ffmpeg_supports_encoder(ffmpeg_path: str, encoder: str) -> bool:
    supported = _ffmpeg_supported_encoders(ffmpeg_path)
    if not supported:
        # If probing fails, let the real ffmpeg invocation surface the underlying error.
        return True
    return encoder in supported


def _append_processing_log_note(log_path: Path | None, message: str) -> None:
    if log_path is None:
        return
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as handle:
            handle.write((f"\n{message.strip()}\n").encode("utf-8", errors="replace"))
    except Exception:
        pass


def _run_logged(
    cmd: list[str],
    *,
    log_path: Path | None,
    live_upload_cb: Callable[[], None] | None = None,
    live_upload_interval_seconds: float = 3.0,
) -> None:
    if log_path is None:
        _run(cmd)
        return

    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        f = log_path.open("ab")
    except Exception:
        # If logging can't even open the file, still run the command so the pipeline doesn't break.
        _run(cmd)
        return

    with f:
        try:
            f.write(("\n$ " + " ".join(cmd) + "\n").encode("utf-8", errors="replace"))
            f.flush()
        except Exception:
            pass

        if live_upload_cb is None:
            subprocess.run(cmd, check=True, stdout=f, stderr=f)
            return

        try:
            proc = subprocess.Popen(cmd, stdout=f, stderr=f)
        except Exception:
            # If we can't start the process with live upload, fall back to the simple runner.
            subprocess.run(cmd, check=True, stdout=f, stderr=f)
            return

        interval = float(live_upload_interval_seconds or 0)
        if interval <= 0:
            interval = 3.0
        # Keep it responsive without busy-looping.
        tick_sleep = min(0.25, interval)

        next_upload_at = time.monotonic() + interval
        while True:
            rc = proc.poll()
            now = time.monotonic()
            if now >= next_upload_at:
                try:
                    live_upload_cb()
                except Exception:
                    pass
                next_upload_at = now + interval
            if rc is not None:
                break
            time.sleep(tick_sleep)

        try:
            live_upload_cb()
        except Exception:
            pass

        if rc != 0:
            raise subprocess.CalledProcessError(int(rc), cmd)


def extract_audio(
    ffmpeg_path: str,
    video_path: Path,
    audio_path: Path,
    *,
    log_path: Path | None = None,
    live_upload_cb: Callable[[], None] | None = None,
) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        str(audio_path),
    ]
    _run_logged(cmd, log_path=log_path, live_upload_cb=live_upload_cb)


def convert_subtitle_to_srt(
    ffmpeg_path: str,
    subtitle_path: Path,
    srt_path: Path,
    *,
    log_path: Path | None = None,
    live_upload_cb: Callable[[], None] | None = None,
) -> None:
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(subtitle_path),
        str(srt_path),
    ]
    _run_logged(cmd, log_path=log_path, live_upload_cb=live_upload_cb)


def transcribe_mock(_audio_path: Path) -> list[Segment]:
    return [
        Segment(
            start=0.0,
            end=5.0,
            text="（示例字幕：当前使用 mock ASR。可改为 faster-whisper 或 openvino，并安装对应 ASR 依赖以获得真实转写。）",
            confidence=1.0,
        )
    ]


def transcribe_faster_whisper(
    audio_path: Path,
    model_name: str,
    language: str = "auto",
    device: str = "cpu",
    compute_type: str = "int8",
    cpu_threads: int | None = None,
    num_workers: int | None = None,
) -> list[Segment]:
    if _audio_path_is_effectively_silent(audio_path):
        logger.info("skipping faster-whisper for effectively silent audio %s", audio_path)
        return []

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("faster-whisper is not installed. Rebuild with INSTALL_ASR=1.") from e

    lang = None if language in {"", "auto", None} else language
    model_kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
    if cpu_threads is not None:
        model_kwargs["cpu_threads"] = int(cpu_threads)
    if num_workers is not None:
        model_kwargs["num_workers"] = int(num_workers)
    model = WhisperModel(model_name, **model_kwargs)
    transcribe_kwargs: dict[str, Any] = {}
    if lang is not None:
        transcribe_kwargs["language"] = lang

    try:
        transcribe_sig = inspect.signature(model.transcribe)
        supported = set(transcribe_sig.parameters)
    except Exception:
        supported = set()

    if "vad_filter" in supported:
        transcribe_kwargs["vad_filter"] = True
    if "vad_parameters" in supported:
        transcribe_kwargs["vad_parameters"] = {
            "min_silence_duration_ms": _FW_VAD_MIN_SILENCE_MS,
            "speech_pad_ms": _FW_VAD_SPEECH_PAD_MS,
        }
    if "condition_on_previous_text" in supported:
        # Avoid propagating hallucinated context across silent / music-only spans.
        transcribe_kwargs["condition_on_previous_text"] = False
    if "no_speech_threshold" in supported:
        transcribe_kwargs["no_speech_threshold"] = _FW_NO_SPEECH_THRESHOLD
    if "log_prob_threshold" in supported:
        transcribe_kwargs["log_prob_threshold"] = _FW_LOG_PROB_THRESHOLD
    if "compression_ratio_threshold" in supported:
        transcribe_kwargs["compression_ratio_threshold"] = _FW_COMPRESSION_RATIO_THRESHOLD

    def transcribe_window(audio: Any) -> list[Segment]:
        try:
            seg_iter, _info = model.transcribe(audio, **transcribe_kwargs)
            raw_segments = list(seg_iter)
        except ValueError as e:
            # This can also be raised while consuming the lazy segment iterator.
            if "empty sequence" in str(e).lower():
                logger.info("faster-whisper returned no speech after VAD for %s", audio_path)
                return []
            raise
        kept = _filter_faster_whisper_segments(raw_segments)
        if raw_segments:
            logger.info("faster-whisper kept %d/%d segments for %s", len(kept), len(raw_segments), audio_path)
        return kept

    try:
        with wave.open(str(audio_path), "rb") as source:
            _validate_asr_wav(source)
            windowed = source.getnframes() > int(_LOCAL_ASR_WINDOW_SECONDS * _ASR_SAMPLE_RATE)
    except (OSError, EOFError, wave.Error, RuntimeError):
        # Keep faster-whisper's existing support for other input formats.
        windowed = False
    if not windowed:
        return transcribe_window(str(audio_path))

    import numpy as np  # type: ignore

    out: list[Segment] = []
    for audio_data, offset_seconds in _iter_wav_windows_as_float_mono_16k(audio_path):
        # faster-whisper accepts ndarray inputs; a path makes it decode the
        # entire file before its own internal 30-second decoding loop starts.
        samples = np.frombuffer(audio_data, dtype=np.float32)
        local_duration = len(audio_data) / float(_ASR_SAMPLE_RATE)
        for segment in transcribe_window(samples):
            start = min(local_duration, max(0.0, float(segment.start)))
            end = min(local_duration, max(start, float(segment.end)))
            if end > start:
                out.append(
                    Segment(
                        start=offset_seconds + start,
                        end=offset_seconds + end,
                        text=segment.text,
                        confidence=segment.confidence,
                        secondary_text=segment.secondary_text,
                    )
                )
        del samples, audio_data
    return reconcile_overlapping_asr_segments(out)


def transcribe_external_whisper(
    audio_path: Path,
    *,
    base_url: str,
    api_key: str,
    model_name: str,
    language: str = "auto",
    timeout_seconds: float = 180.0,
) -> list[Segment]:
    """Transcribe audio through an OpenAI-compatible Whisper API."""
    key = str(api_key or "").strip()
    model = str(model_name or "").strip()
    if not key:
        raise RuntimeError("external Whisper API key is not set")
    if not model:
        raise RuntimeError("external Whisper model is not set")
    url = build_openai_audio_transcriptions_url(base_url)
    data: dict[str, str] = {"model": model, "response_format": "verbose_json"}
    lang = str(language or "").strip()
    if lang and lang.lower() != "auto":
        data["language"] = lang
    timeout = max(1.0, min(600.0, float(timeout_seconds)))
    headers = {"Authorization": f"Bearer {key}"}
    try:
        with audio_path.open("rb") as audio_file:
            response = httpx.post(
                url,
                headers=headers,
                data=data,
                files={"file": (audio_path.name, audio_file, "audio/wav")},
                timeout=timeout,
            )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        detail = (exc.response.text or "").strip().replace("\n", " ")[:500]
        raise RuntimeError(f"external Whisper API failed (status={exc.response.status_code}): {detail}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"external Whisper API request failed: {exc}") from exc
    except ValueError as exc:
        raise RuntimeError("external Whisper API returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("external Whisper API response must be an object")
    segments_raw = payload.get("segments")
    out: list[Segment] = []
    if isinstance(segments_raw, list):
        for item in segments_raw:
            if not isinstance(item, dict):
                continue
            text = _normalize_asr_text(str(item.get("text") or ""))
            if not text:
                continue
            try:
                start = max(0.0, float(item.get("start") or 0.0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                start, end = 0.0, 0.0
            out.append(Segment(start=start, end=end, text=text))
    if out:
        return out
    text = _normalize_asr_text(str(payload.get("text") or ""))
    if not text:
        return []
    try:
        duration = _wav_duration_seconds(audio_path)
    except Exception:
        duration = 0.0
    return [Segment(start=0.0, end=max(0.0, float(duration)), text=text)]


def _groq_chunk_windows(
    audio_path: Path,
    *,
    chunk_seconds: float = _GROQ_CHUNK_SECONDS,
    overlap_seconds: float = _GROQ_CHUNK_OVERLAP_SECONDS,
) -> list[tuple[float, float]]:
    """Return deterministic fixed-duration windows for a PCM WAV."""
    try:
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            total_frames = source.getnframes()
            if channels <= 0 or sample_width <= 0 or sample_rate <= 0 or total_frames <= 0:
                raise ValueError("invalid WAV format")
            chunk_frames = max(1, int(max(1.0, float(chunk_seconds)) * sample_rate))
            overlap_frames = min(
                max(0, chunk_frames - 1),
                max(0, int(max(0.0, float(overlap_seconds)) * sample_rate)),
            )
            windows: list[tuple[float, float]] = []
            start_frame = 0
            while start_frame < total_frames:
                frame_count = min(chunk_frames, total_frames - start_frame)
                duration = frame_count / sample_rate
                windows.append((start_frame / sample_rate, duration))
                if start_frame + frame_count >= total_frames:
                    break
                start_frame += max(1, frame_count - overlap_frames)
            return windows
    except (EOFError, OSError, ValueError, wave.Error) as exc:
        raise RuntimeError(f"Groq Whisper requires a valid WAV audio file: {exc}") from exc


def _encode_groq_flac_chunk(
    audio_path: Path,
    *,
    offset: float,
    duration: float,
    ffmpeg_path: str,
) -> bytes:
    """Encode one source window as lossless 16 kHz mono FLAC."""
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="groq-whisper-", suffix=".flac", delete=False) as handle:
            temp_path = Path(handle.name)
        cmd = [
            str(ffmpeg_path or "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, float(offset)):.6f}",
            "-t",
            f"{max(0.001, float(duration)):.6f}",
            "-i",
            str(audio_path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            "-compression_level",
            "8",
            str(temp_path),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        data = temp_path.read_bytes()
    except FileNotFoundError as exc:
        raise RuntimeError(f"ffmpeg executable not found: {ffmpeg_path}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[-500:]
        raise RuntimeError(f"failed to encode Groq FLAC chunk: {detail or exc}") from exc
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    if not data:
        raise RuntimeError("failed to encode Groq FLAC chunk: ffmpeg returned an empty file")
    return data


def _groq_checkpoint_identity(
    *,
    audio_identity: str,
    model: str,
    language: str,
    chunk_seconds: float,
    overlap_seconds: float,
    chunk_count: int,
    vad_enabled: bool,
    vad_threshold: float,
) -> dict[str, Any]:
    return {
        "version": _GROQ_CHECKPOINT_VERSION,
        "audio_identity": str(audio_identity or "").strip(),
        "model": model,
        "language": language,
        "format": "flac",
        "chunk_seconds": float(chunk_seconds),
        "overlap_seconds": float(overlap_seconds),
        "chunk_count": int(chunk_count),
        "vad_enabled": bool(vad_enabled),
        "vad_threshold": float(vad_threshold) if vad_enabled else None,
    }


def _load_groq_checkpoint(
    checkpoint_path: Path | None,
    *,
    identity: dict[str, Any],
    windows: list[tuple[float, float]],
) -> dict[int, list[Segment]]:
    if checkpoint_path is None:
        return {}
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    for key, value in identity.items():
        if payload.get(key) != value:
            return {}
    completed = payload.get("completed_chunks")
    if not isinstance(completed, dict):
        return {}
    restored: dict[int, list[Segment]] = {}
    for raw_index, item in completed.items():
        if not isinstance(item, dict):
            continue
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            continue
        if index < 1 or index > len(windows):
            continue
        offset, duration = windows[index - 1]
        try:
            stored_offset = float(item.get("offset"))
            stored_duration = float(item.get("duration"))
        except (TypeError, ValueError):
            continue
        if abs(stored_offset - offset) > 0.001 or abs(stored_duration - duration) > 0.001:
            continue
        segments = segments_from_json_data(item.get("segments"))
        restored[index] = segments
    return restored


def _save_groq_checkpoint(
    checkpoint_path: Path | None,
    *,
    identity: dict[str, Any],
    windows: list[tuple[float, float]],
    completed: dict[int, list[Segment]],
) -> None:
    if checkpoint_path is None:
        return
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **identity,
        "completed_chunks": {
            str(index): {
                "offset": windows[index - 1][0],
                "duration": windows[index - 1][1],
                "segments": segments_to_json_data(segments),
            }
            for index, segments in sorted(completed.items())
        },
    }
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{checkpoint_path.name}.",
            suffix=".partial",
            dir=checkpoint_path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(checkpoint_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _segments_from_groq_result(result: dict[str, Any], *, fallback_duration: float) -> list[Segment]:
    out: list[Segment] = []
    segments_raw = result.get("segments")
    if isinstance(segments_raw, list):
        for item in segments_raw:
            if not isinstance(item, dict):
                continue
            text = _normalize_asr_text(str(item.get("text") or ""))
            if not text:
                continue
            try:
                start = max(0.0, float(item.get("start") or 0.0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                continue
            out.append(Segment(start=start, end=end, text=text))
    if out:
        return out

    words = result.get("words")
    if isinstance(words, list):
        timed_words: list[tuple[float, float, str]] = []
        for item in words:
            if not isinstance(item, dict):
                continue
            word = _normalize_asr_text(str(item.get("word") or ""))
            if not word:
                continue
            try:
                start = max(0.0, float(item.get("start") or 0.0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                continue
            timed_words.append((start, end, word))
        if timed_words:
            return [
                Segment(
                    start=timed_words[0][0],
                    end=timed_words[-1][1],
                    text=_normalize_asr_text(" ".join(word for _, _, word in timed_words)),
                )
            ]

    text = _normalize_asr_text(str(result.get("text") or ""))
    return [Segment(start=0.0, end=max(0.0, float(fallback_duration)), text=text)] if text else []


_ASR_ALIGNMENT_TOKEN_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\W_]+(?:['’][^\W_]+)?",
    re.UNICODE,
)


def _asr_alignment_tokens(text: str) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for match in _ASR_ALIGNMENT_TOKEN_RE.finditer(str(text or "")):
        token = unicodedata.normalize("NFKC", match.group(0)).casefold()
        if token:
            out.append((token, match.start(), match.end()))
    return out


def _join_asr_text(prefix: str, suffix: str) -> str:
    left = str(prefix or "").rstrip()
    right = str(suffix or "").lstrip()
    if not left:
        return right
    if not right:
        return left
    if right[0] in ",.!?;:，。！？；：、)]}）】》」』”’":
        return f"{left}{right}"
    if (
        "\u3040" <= left[-1] <= "\u30ff"
        or "\u3400" <= left[-1] <= "\u9fff"
        or "\u3040" <= right[0] <= "\u30ff"
        or "\u3400" <= right[0] <= "\u9fff"
    ):
        return f"{left}{right}"
    return f"{left} {right}"


def _trim_asr_token_prefix(text: str, token_count: int) -> str:
    tokens = _asr_alignment_tokens(text)
    if token_count <= 0:
        return str(text or "").strip()
    if token_count >= len(tokens):
        return ""
    return str(text or "")[tokens[token_count - 1][2] :].lstrip()


def _contains_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    width = len(needle)
    return any(haystack[index : index + width] == needle for index in range(len(haystack) - width + 1))


def _meaningful_asr_token_match(tokens: list[str]) -> bool:
    if len(tokens) >= 3:
        return True
    return len(tokens) >= 2 and sum(len(token) for token in tokens) >= 8


def _stitch_related_asr_text(previous_text: str, current_text: str) -> str | None:
    """Reconcile repeated or continuing text from overlapping ASR windows."""
    previous_tokens_with_spans = _asr_alignment_tokens(previous_text)
    current_tokens_with_spans = _asr_alignment_tokens(current_text)
    previous_tokens = [token for token, _, _ in previous_tokens_with_spans]
    current_tokens = [token for token, _, _ in current_tokens_with_spans]
    if not previous_tokens or not current_tokens:
        return None
    if previous_tokens == current_tokens:
        return previous_text if len(previous_text.strip()) >= len(current_text.strip()) else current_text
    if _contains_token_sequence(current_tokens, previous_tokens):
        return current_text
    if _contains_token_sequence(previous_tokens, current_tokens):
        return previous_text

    max_overlap = min(len(previous_tokens), len(current_tokens))
    for size in range(max_overlap, 0, -1):
        overlap_tokens = previous_tokens[-size:]
        if overlap_tokens != current_tokens[:size] or not _meaningful_asr_token_match(overlap_tokens):
            continue
        suffix = _trim_asr_token_prefix(current_text, size)
        return _join_asr_text(previous_text, suffix)

    matcher = SequenceMatcher(None, previous_tokens, current_tokens, autojunk=False)
    anchored_matches = [
        block
        for block in matcher.get_matching_blocks()
        if block.size > 0
        and len(previous_tokens) - (block.a + block.size) <= 1
        and block.b <= 1
        and _meaningful_asr_token_match(previous_tokens[block.a : block.a + block.size])
    ]
    if anchored_matches:
        block = max(anchored_matches, key=lambda item: item.size)
        shorter_size = max(1, min(len(previous_tokens), len(current_tokens)))
        if block.size / shorter_size >= 0.65:
            if len(current_tokens) > len(previous_tokens):
                return current_text
            if len(previous_tokens) > len(current_tokens):
                return previous_text
            return previous_text if len(previous_text.strip()) >= len(current_text.strip()) else current_text
        suffix = _trim_asr_token_prefix(current_text, block.b + block.size)
        return _join_asr_text(previous_text, suffix)

    if matcher.ratio() >= 0.78 and _meaningful_asr_token_match(previous_tokens[: min(len(previous_tokens), 3)]):
        if len(current_tokens) > len(previous_tokens):
            return current_text
        return previous_text
    return None


def _combined_segment_confidence(previous: Segment, current: Segment) -> float | None:
    values = [value for value in (previous.confidence, current.confidence) if value is not None]
    return max(values) if values else None


def reconcile_overlapping_asr_segments(
    segments: Iterable[Segment],
    *,
    minimum_duration: float = 0.12,
) -> list[Segment]:
    """Stitch related ASR captions and guarantee a non-overlapping timeline."""
    candidates = sorted(segments, key=lambda item: (float(item.start), float(item.end), item.text))
    out: list[Segment] = []
    min_duration = max(0.01, float(minimum_duration))
    for raw_segment in candidates:
        text = _normalize_asr_text(raw_segment.text)
        if not text:
            continue
        segment = Segment(
            start=max(0.0, float(raw_segment.start)),
            end=max(max(0.0, float(raw_segment.start)), float(raw_segment.end)),
            text=text,
            confidence=raw_segment.confidence,
            secondary_text=raw_segment.secondary_text,
        )
        if not out:
            out.append(segment)
            continue

        previous = out[-1]
        if segment.start >= previous.end:
            out.append(segment)
            continue

        overlap_duration = max(0.0, min(previous.end, segment.end) - segment.start)
        shorter_duration = max(
            0.0,
            min(previous.end - previous.start, segment.end - segment.start),
        )
        substantial_overlap = overlap_duration >= max(
            min_duration * 2.0,
            min(0.5, shorter_duration * 0.5),
        )
        # A complete short utterance can be shorter than the absolute overlap
        # threshold; high coverage still identifies its duplicated chunk result.
        if shorter_duration > 0:
            substantial_overlap = substantial_overlap or overlap_duration >= shorter_duration * 0.8
        if substantial_overlap:
            # Similar words only indicate duplicate recognition when the
            # captions also cover the same speech. At a slight timing overlap,
            # even identical text may be a separate spoken repetition.
            stitched_text = _stitch_related_asr_text(previous.text, segment.text)
            out[-1] = Segment(
                start=previous.start,
                end=max(previous.end, segment.end),
                text=stitched_text if stitched_text is not None else _join_asr_text(previous.text, segment.text),
                confidence=_combined_segment_confidence(previous, segment),
                secondary_text=previous.secondary_text or segment.secondary_text,
            )
            continue

        clipped_previous_duration = segment.start - previous.start
        if clipped_previous_duration >= min_duration:
            out[-1] = Segment(
                start=previous.start,
                end=segment.start,
                text=previous.text,
                confidence=previous.confidence,
                secondary_text=previous.secondary_text,
            )
            out.append(segment)
            continue

        shifted_start = previous.end
        if segment.end - shifted_start >= min_duration:
            out.append(
                Segment(
                    start=shifted_start,
                    end=segment.end,
                    text=segment.text,
                    confidence=segment.confidence,
                    secondary_text=segment.secondary_text,
                )
            )
            continue

        out[-1] = Segment(
            start=previous.start,
            end=max(previous.end, segment.end),
            text=_join_asr_text(previous.text, segment.text),
            confidence=_combined_segment_confidence(previous, segment),
            secondary_text=previous.secondary_text or segment.secondary_text,
        )
    return out


def _merge_groq_segments(chunks: Iterable[tuple[float, Iterable[Segment]]]) -> list[Segment]:
    """Apply chunk offsets and reconcile text repeated across overlap windows."""
    candidates: list[Segment] = []
    for offset, segments in chunks:
        for segment in segments:
            candidates.append(
                Segment(
                    start=max(0.0, float(segment.start) + float(offset)),
                    end=max(float(segment.start) + float(offset), float(segment.end) + float(offset)),
                    text=segment.text,
                    confidence=segment.confidence,
                    secondary_text=segment.secondary_text,
                )
            )
    return reconcile_overlapping_asr_segments(candidates)


def _groq_status_is_retryable(status_code: int) -> bool:
    return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599


def _groq_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return a bounded backoff, honoring a numeric Retry-After header."""
    if response is not None:
        retry_after = str(response.headers.get("retry-after") or "").strip()
        try:
            if retry_after:
                return min(_GROQ_RETRY_MAX_SECONDS, max(0.5, float(retry_after)))
        except (TypeError, ValueError):
            pass
    exponential = _GROQ_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1))
    return min(_GROQ_RETRY_MAX_SECONDS, random.uniform(exponential * 0.75, exponential * 1.25))


def transcribe_groq_whisper(
    audio_path: Path,
    *,
    api_key: str,
    model_name: str = "whisper-large-v3-turbo",
    language: str = "auto",
    base_url: str = _GROQ_DEFAULT_BASE_URL,
    timeout_seconds: float = 180.0,
    chunk_seconds: float = _GROQ_CHUNK_SECONDS,
    overlap_seconds: float = _GROQ_CHUNK_OVERLAP_SECONDS,
    ffmpeg_path: str = "ffmpeg",
    checkpoint_path: Path | None = None,
    audio_identity: str = "",
    vad_enabled: bool = True,
    vad_threshold: float = _OPENVINO_VAD_THRESHOLD,
) -> list[Segment]:
    """Transcribe through Groq using resumable, VAD-gated FLAC chunks."""
    key = str(api_key or "").strip()
    model = str(model_name or "").strip()
    if not key:
        raise RuntimeError("Groq Whisper API key is not set")
    if model not in {"whisper-large-v3", "whisper-large-v3-turbo"}:
        raise RuntimeError("Groq Whisper model must be whisper-large-v3 or whisper-large-v3-turbo")
    url = build_openai_audio_transcriptions_url(base_url or _GROQ_DEFAULT_BASE_URL)
    timeout = max(1.0, min(600.0, float(timeout_seconds)))
    lang = str(language or "").strip()
    normalized_lang = "" if lang.lower() == "auto" else lang
    normalized_chunk_seconds = max(1.0, float(chunk_seconds))
    normalized_overlap_seconds = min(
        max(0.0, float(overlap_seconds)),
        max(0.0, normalized_chunk_seconds - 0.001),
    )
    normalized_vad_threshold = max(0.01, min(0.99, float(vad_threshold)))
    windows = _groq_chunk_windows(
        audio_path,
        chunk_seconds=normalized_chunk_seconds,
        overlap_seconds=normalized_overlap_seconds,
    )
    identity = _groq_checkpoint_identity(
        audio_identity=str(audio_identity or audio_path.resolve()),
        model=model,
        language=normalized_lang,
        chunk_seconds=normalized_chunk_seconds,
        overlap_seconds=normalized_overlap_seconds,
        chunk_count=len(windows),
        vad_enabled=bool(vad_enabled),
        vad_threshold=normalized_vad_threshold,
    )
    completed = _load_groq_checkpoint(checkpoint_path, identity=identity, windows=windows)
    results: list[tuple[float, Iterable[Segment]]] = []
    if completed:
        logger.info("Groq Whisper restored %d/%d chunks from checkpoint", len(completed), len(windows))
    for index, (offset, duration) in enumerate(windows, start=1):
        restored_segments = completed.get(index)
        if restored_segments is not None:
            results.append((offset, restored_segments))
            continue
        if vad_enabled:
            has_speech = _groq_window_has_speech(
                audio_path,
                offset=offset,
                duration=duration,
                threshold=normalized_vad_threshold,
            )
            if has_speech is False:
                logger.info(
                    "Groq Whisper skipped chunk %d/%d (offset=%.2fs duration=%.2fs): Silero VAD found no speech",
                    index,
                    len(windows),
                    offset,
                    duration,
                )
                completed[index] = []
                _save_groq_checkpoint(checkpoint_path, identity=identity, windows=windows, completed=completed)
                results.append((offset, []))
                continue
        audio_bytes = _encode_groq_flac_chunk(
            audio_path,
            offset=offset,
            duration=duration,
            ffmpeg_path=ffmpeg_path,
        )
        data: dict[str, str] = {
            "model": model,
            "response_format": "verbose_json",
            # Groq defaults verbose_json timestamps to segment granularity.
            # Its live multipart endpoint rejects the unbracketed parameter
            # shown in part of the documentation, so omit this optional field.
            "temperature": "0",
        }
        if normalized_lang:
            data["language"] = normalized_lang
        payload: Any = None
        for attempt in range(1, _GROQ_MAX_REQUEST_ATTEMPTS + 1):
            try:
                response = httpx.post(
                    url,
                    headers={"Authorization": f"Bearer {key}"},
                    data=data,
                    files={"file": (f"{audio_path.stem}-part-{index}.flac", audio_bytes, "audio/flac")},
                    timeout=timeout,
                )
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status = int(exc.response.status_code)
                detail = (exc.response.text or "").strip().replace("\n", " ")[:500]
                retryable = _groq_status_is_retryable(status)
                if retryable and attempt < _GROQ_MAX_REQUEST_ATTEMPTS:
                    delay = _groq_retry_delay(exc.response, attempt)
                    logger.warning(
                        "Groq Whisper chunk %d/%d attempt %d/%d failed with HTTP %d; retrying in %.1fs",
                        index,
                        len(windows),
                        attempt,
                        _GROQ_MAX_REQUEST_ATTEMPTS,
                        status,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                if status == 413:
                    detail = f"Groq audio chunk is still too large; lower the chunk limit. {detail}".strip()
                elif status == 524:
                    detail = (
                        "Cloudflare 524: Groq did not finish this audio chunk before its upstream gateway timeout; "
                        "the chunk was retried automatically. "
                        f"{detail}"
                    ).strip()
                raise RuntimeError(
                    f"Groq Whisper API failed on chunk {index}/{len(windows)} after {attempt} attempt(s) "
                    f"(status={status}): {detail}"
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < _GROQ_MAX_REQUEST_ATTEMPTS:
                    delay = _groq_retry_delay(None, attempt)
                    logger.warning(
                        "Groq Whisper chunk %d/%d attempt %d/%d failed with %s; retrying in %.1fs",
                        index,
                        len(windows),
                        attempt,
                        _GROQ_MAX_REQUEST_ATTEMPTS,
                        type(exc).__name__,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Groq Whisper API request failed on chunk {index}/{len(windows)} after {attempt} attempt(s): {exc}"
                ) from exc
            except ValueError as exc:
                if attempt < _GROQ_MAX_REQUEST_ATTEMPTS:
                    delay = _groq_retry_delay(None, attempt)
                    logger.warning(
                        "Groq Whisper chunk %d/%d attempt %d/%d returned invalid JSON; retrying in %.1fs",
                        index,
                        len(windows),
                        attempt,
                        _GROQ_MAX_REQUEST_ATTEMPTS,
                        delay,
                    )
                    time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"Groq Whisper API returned invalid JSON on chunk {index}/{len(windows)} "
                    f"after {attempt} attempt(s)"
                ) from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Groq Whisper API response must be an object")
        chunk_segments = _segments_from_groq_result(payload, fallback_duration=duration)
        completed[index] = chunk_segments
        _save_groq_checkpoint(checkpoint_path, identity=identity, windows=windows, completed=completed)
        results.append((offset, chunk_segments))
    return _merge_groq_segments(results)


def _cloudflare_wav_chunks(audio_path: Path, *, chunk_seconds: float = 30.0) -> list[tuple[float, float, bytes]]:
    try:
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            compression_type = source.getcomptype()
            compression_name = source.getcompname()
            if channels <= 0 or sample_width <= 0 or sample_rate <= 0:
                raise ValueError("invalid WAV format")
            frames_per_chunk = max(1, int(sample_rate * max(1.0, chunk_seconds)))
            chunks: list[tuple[float, float, bytes]] = []
            frame_offset = 0
            while True:
                frames = source.readframes(frames_per_chunk)
                if not frames:
                    break
                frame_count = len(frames) // (channels * sample_width)
                duration = frame_count / sample_rate
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as target:
                    target.setnchannels(channels)
                    target.setsampwidth(sample_width)
                    target.setframerate(sample_rate)
                    target.setcomptype(compression_type, compression_name)
                    target.writeframes(frames)
                chunks.append((frame_offset / sample_rate, duration, buffer.getvalue()))
                frame_offset += frame_count
            if chunks:
                return chunks
    except (EOFError, OSError, ValueError, wave.Error):
        pass
    return [(0.0, 0.0, audio_path.read_bytes())]


def _segments_from_cloudflare_result(result: dict[str, Any], *, fallback_duration: float) -> list[Segment]:
    out: list[Segment] = []
    segments_raw = result.get("segments")
    if isinstance(segments_raw, list):
        for item in segments_raw:
            if not isinstance(item, dict):
                continue
            text = _normalize_asr_text(str(item.get("text") or ""))
            if not text:
                continue
            try:
                start = max(0.0, float(item.get("start") or 0.0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                continue
            out.append(Segment(start=start, end=end, text=text))
    if out:
        return out

    # The original @cf/openai/whisper model exposes word timestamps instead
    # of segments. Keep a bounded speech span rather than stretching text to
    # the full file duration.
    words = result.get("words")
    if isinstance(words, list):
        timed_words: list[tuple[float, float, str]] = []
        for item in words:
            if not isinstance(item, dict):
                continue
            word = _normalize_asr_text(str(item.get("word") or ""))
            if not word:
                continue
            try:
                start = max(0.0, float(item.get("start") or 0.0))
                end = max(start, float(item.get("end") or start))
            except (TypeError, ValueError):
                continue
            timed_words.append((start, end, word))
        if timed_words:
            return [
                Segment(
                    start=timed_words[0][0],
                    end=timed_words[-1][1],
                    text=_normalize_asr_text(" ".join(word for _, _, word in timed_words)),
                )
            ]

    text = _normalize_asr_text(str(result.get("text") or ""))
    if not text:
        return []
    return [Segment(start=0.0, end=max(0.0, fallback_duration), text=text)]


def transcribe_cloudflare_workers_ai(
    audio_path: Path,
    *,
    account_id: str,
    api_key: str,
    model_name: str = "@cf/openai/whisper-large-v3-turbo",
    language: str = "auto",
    timeout_seconds: float = 180.0,
) -> list[Segment]:
    """Transcribe audio through Cloudflare Workers AI's native /ai/run API."""
    account = str(account_id or "").strip()
    key = str(api_key or "").strip()
    model = str(model_name or "").strip()
    if not account:
        raise RuntimeError("Cloudflare Workers AI account ID is not set")
    if not key:
        raise RuntimeError("Cloudflare Workers AI API key is not set")
    if not model:
        raise RuntimeError("Cloudflare Workers AI model is not set")
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in account):
        raise RuntimeError("Cloudflare Workers AI account ID contains invalid characters")
    if not model.startswith("@cf/") or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@/_.-" for ch in model
    ):
        raise RuntimeError("Cloudflare Workers AI model must be a valid @cf/ model ID")

    lang = str(language or "").strip()
    timeout = max(1.0, min(600.0, float(timeout_seconds)))
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
    out: list[Segment] = []
    chunks = _cloudflare_wav_chunks(audio_path)
    for chunk_index, (offset, duration, audio_bytes) in enumerate(chunks, start=1):
        payload: dict[str, Any] = {
            "audio": base64.b64encode(audio_bytes).decode("ascii"),
            "task": "transcribe",
        }
        if lang and lang.lower() != "auto":
            payload["language"] = lang
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            raw_payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = (exc.response.text or "").strip().replace("\n", " ")[:500]
            raise RuntimeError(
                f"Cloudflare Workers AI request failed on chunk {chunk_index}/{len(chunks)} "
                f"(status={exc.response.status_code}): {detail}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"Cloudflare Workers AI request failed on chunk {chunk_index}/{len(chunks)}: {exc}"
            ) from exc
        except ValueError as exc:
            raise RuntimeError("Cloudflare Workers AI returned invalid JSON") from exc

        if not isinstance(raw_payload, dict):
            raise RuntimeError("Cloudflare Workers AI response must be an object")
        if raw_payload.get("success") is False:
            errors = raw_payload.get("errors")
            detail = json.dumps(errors, ensure_ascii=False)[:500] if errors else "unknown Cloudflare error"
            raise RuntimeError(f"Cloudflare Workers AI request failed: {detail}")
        result = raw_payload.get("result")
        result = result if isinstance(result, dict) else raw_payload
        chunk_segments = _segments_from_cloudflare_result(result, fallback_duration=duration)
        out.extend(
            Segment(
                start=segment.start + offset,
                end=segment.end + offset,
                text=segment.text,
                confidence=segment.confidence,
                secondary_text=segment.secondary_text,
            )
            for segment in chunk_segments
        )
    return out


@dataclass(frozen=True)
class _OpenVinoChunk:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class _OpenVinoSpeechSpan:
    start_sample: int
    end_sample: int

    @property
    def duration_seconds(self) -> float:
        return max(0, self.end_sample - self.start_sample) / float(_ASR_SAMPLE_RATE)


def _validate_asr_wav(source: wave.Wave_read) -> None:
    channels = int(source.getnchannels() or 0)
    sample_rate = int(source.getframerate() or 0)
    sample_width = int(source.getsampwidth() or 0)
    if channels != 1:
        raise RuntimeError(f"ASR expects mono WAV, got channels={channels}")
    if sample_rate != _ASR_SAMPLE_RATE:
        raise RuntimeError(f"ASR expects 16k WAV, got sample_rate={sample_rate}")
    if sample_width not in {1, 2, 4}:
        raise RuntimeError(f"unsupported WAV sample width for ASR: {sample_width} bytes")


def _wav_duration_seconds(audio_path: Path) -> float:
    with wave.open(str(audio_path), "rb") as source:
        sample_rate = int(source.getframerate() or 0)
        return source.getnframes() / float(sample_rate) if sample_rate > 0 else 0.0


def _pcm_samples(raw: bytes, sample_width: int) -> Iterator[float]:
    """Decode one PCM buffer without allocating a Python object per sample."""
    if sample_width == 1:
        for sample in raw:
            yield (sample - 128) / 128.0
        return
    if sample_width not in {2, 4}:
        raise RuntimeError(f"unsupported WAV sample width for ASR: {sample_width} bytes")
    ints = array("h" if sample_width == 2 else "i")
    ints.frombytes(raw)
    if sys.byteorder != "little":
        ints.byteswap()
    scale = 32768.0 if sample_width == 2 else 2147483648.0
    for sample in ints:
        yield sample / scale


def _iter_wav_windows_as_float_mono_16k(audio_path: Path) -> Iterator[tuple[array, float]]:
    """Yield a single packed float32 window at a time, with absolute offsets."""
    with wave.open(str(audio_path), "rb") as source:
        _validate_asr_wav(source)
        total_frames = source.getnframes()
        window_frames = max(1, int(_LOCAL_ASR_WINDOW_SECONDS * _ASR_SAMPLE_RATE))
        overlap_frames = min(window_frames - 1, max(0, int(_LOCAL_ASR_OVERLAP_SECONDS * _ASR_SAMPLE_RATE)))
        step_frames = window_frames - overlap_frames
        for start_frame in range(0, total_frames, step_frames):
            source.setpos(start_frame)
            count = min(window_frames, total_frames - start_frame)
            raw = source.readframes(count)
            samples = array("f", _pcm_samples(raw, source.getsampwidth()))
            del raw
            actual_count = len(samples)
            if not actual_count:
                break
            yield samples, start_frame / float(_ASR_SAMPLE_RATE)
            del samples
            if actual_count < count or start_frame + count >= total_frames:
                break


def _audio_signal_stats(audio_data: Iterable[float]) -> tuple[int, float, float, float]:
    sample_count = 0
    peak = 0.0
    sum_squares = 0.0
    active = 0

    for raw_sample in audio_data:
        sample = abs(_as_float(raw_sample) or 0.0)
        sample_count += 1
        if sample > peak:
            peak = sample
        sum_squares += sample * sample
        if sample >= _ASR_SILENCE_ACTIVE_THRESHOLD:
            active += 1

    if sample_count <= 0:
        return 0, 0.0, 0.0, 0.0

    rms = (sum_squares / float(sample_count)) ** 0.5
    active_ratio = active / float(sample_count)
    return sample_count, peak, rms, active_ratio


def _audio_is_effectively_silent(audio_data: Iterable[float]) -> bool:
    sample_count, peak, rms, active_ratio = _audio_signal_stats(audio_data)
    if sample_count <= 0:
        return True
    return (
        peak <= _ASR_SILENCE_PEAK_THRESHOLD
        and rms <= _ASR_SILENCE_RMS_THRESHOLD
        and active_ratio <= _ASR_SILENCE_ACTIVE_RATIO_THRESHOLD
    )


def _audio_path_is_effectively_silent(audio_path: Path) -> bool:
    try:
        with wave.open(str(audio_path), "rb") as source:
            _validate_asr_wav(source)
            sample_count = 0
            sum_squares = 0.0
            active = 0
            while raw := source.readframes(_ASR_PCM_READ_FRAMES):
                for raw_sample in _pcm_samples(raw, source.getsampwidth()):
                    sample = abs(raw_sample)
                    # A peak cannot decrease; RMS and active ratio can, so
                    # those statistics must cover the complete recording.
                    if sample > _ASR_SILENCE_PEAK_THRESHOLD:
                        return False
                    sample_count += 1
                    sum_squares += sample * sample
                    if sample >= _ASR_SILENCE_ACTIVE_THRESHOLD:
                        active += 1
    except Exception as e:
        logger.debug("skipping WAV silence probe for %s: %s: %s", audio_path, type(e).__name__, e)
        return False
    if sample_count <= 0:
        return True
    return (
        (sum_squares / float(sample_count)) ** 0.5 <= _ASR_SILENCE_RMS_THRESHOLD
        and active / float(sample_count) <= _ASR_SILENCE_ACTIVE_RATIO_THRESHOLD
    )


def _read_wav_window_as_float_mono_16k(
    audio_path: Path,
    *,
    offset: float,
    duration: float,
) -> array:
    """Read one bounded WAV window without loading the complete source audio."""
    with wave.open(str(audio_path), "rb") as wf:
        _validate_asr_wav(wf)
        sample_rate = int(wf.getframerate() or 0)
        sample_width = int(wf.getsampwidth() or 0)
        total_frames = int(wf.getnframes() or 0)
        start_frame = max(0, min(total_frames, int(max(0.0, float(offset)) * sample_rate)))
        frame_count = max(0, min(total_frames - start_frame, int(max(0.0, float(duration)) * sample_rate)))
        wf.setpos(start_frame)
        raw = wf.readframes(frame_count)

    return array("f", _pcm_samples(raw, sample_width))


def _groq_window_has_speech(
    audio_path: Path,
    *,
    offset: float,
    duration: float,
    threshold: float,
) -> bool | None:
    """Return False for a confirmed non-speech window, None to upload safely."""
    try:
        audio_data = _read_wav_window_as_float_mono_16k(
            audio_path,
            offset=offset,
            duration=duration,
        )
    except Exception as exc:
        logger.warning(
            "Groq Whisper VAD could not read window offset=%.2fs duration=%.2fs (%s); uploading it normally",
            offset,
            duration,
            type(exc).__name__,
        )
        return None
    if _audio_is_effectively_silent(audio_data):
        return False
    spans = _detect_silero_speech_spans(audio_data, threshold=threshold)
    if spans is None:
        logger.warning(
            "Groq Whisper Silero VAD is unavailable for window offset=%.2fs; uploading it normally",
            offset,
        )
        return None
    return bool(spans)


def _detect_silero_speech_spans(
    audio_data: Sequence[float],
    *,
    threshold: float = _OPENVINO_VAD_THRESHOLD,
) -> list[_OpenVinoSpeechSpan] | None:
    """Return Silero VAD speech spans, or None when VAD is unavailable.

    ``None`` deliberately falls back to transcribing the complete window so a
    partial ASR installation does not turn real speech into an empty subtitle.
    An empty list means VAD ran successfully and found no human speech.
    """
    try:
        import numpy as np  # type: ignore
        from faster_whisper.vad import VadOptions, get_speech_timestamps  # type: ignore
    except Exception as e:  # pragma: no cover - depends on optional ASR packages
        logger.warning(
            "Silero VAD is unavailable (%s); falling back to window transcription",
            type(e).__name__,
        )
        return None

    try:
        samples = np.asarray(audio_data, dtype=np.float32)
        raw_spans = get_speech_timestamps(
            samples,
            vad_options=VadOptions(
                threshold=max(0.01, min(0.99, float(threshold))),
                min_speech_duration_ms=_OPENVINO_VAD_MIN_SPEECH_MS,
                max_speech_duration_s=_OPENVINO_VAD_MAX_SPEECH_SECONDS,
                min_silence_duration_ms=_OPENVINO_VAD_MIN_SILENCE_MS,
                speech_pad_ms=_OPENVINO_VAD_SPEECH_PAD_MS,
            ),
            sampling_rate=_ASR_SAMPLE_RATE,
        )
    except Exception as e:  # pragma: no cover - runtime/model dependent
        logger.warning(
            "Silero VAD failed for %d samples (%s); falling back to window transcription",
            len(audio_data),
            type(e).__name__,
        )
        return None

    spans: list[_OpenVinoSpeechSpan] = []
    sample_count = len(audio_data)
    for raw_span in raw_spans:
        if not isinstance(raw_span, dict):
            continue
        start = max(0, min(sample_count, int(raw_span.get("start") or 0)))
        end = max(start, min(sample_count, int(raw_span.get("end") or 0)))
        if end > start:
            spans.append(_OpenVinoSpeechSpan(start_sample=start, end_sample=end))
    return spans


def _offset_openvino_chunks(chunks: Iterable[_OpenVinoChunk], *, offset_seconds: float) -> list[_OpenVinoChunk]:
    offset = max(0.0, float(offset_seconds))
    return [
        _OpenVinoChunk(
            start=max(0.0, offset + float(chunk.start)),
            end=max(offset + float(chunk.start), offset + float(chunk.end)),
            text=chunk.text,
        )
        for chunk in chunks
    ]


def _dedupe_overlapping_asr_segments(segments: Iterable[Segment]) -> list[Segment]:
    """Reconcile captions caused by padded VAD spans overlapping at an edge."""
    return reconcile_overlapping_asr_segments(segments)


def _normalize_openvino_language(language: str) -> str | None:
    lang = str(language or "").strip()
    if not lang or lang.lower() == "auto":
        return None
    if lang.startswith("<|") and lang.endswith("|>"):
        return lang
    return f"<|{lang.lower()}|>"


def _get_openvino_pipeline(model_path: str, device: str) -> Any:
    try:
        from openvino_genai import WhisperPipeline  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("openvino-genai is not installed. Rebuild with INSTALL_ASR=1.") from e

    key = (str(model_path), str(device))
    with _OPENVINO_PIPELINE_CACHE_LOCK:
        pipeline = _OPENVINO_PIPELINE_CACHE.get(key)
        if pipeline is None:
            # Release the previous cached model before allocating its replacement.
            # A worker must not retain every model/device combination it has used.
            _OPENVINO_PIPELINE_CACHE.clear()
            pipeline = WhisperPipeline(model_path, device=device)
            _OPENVINO_PIPELINE_CACHE[key] = pipeline
    return pipeline


def _openvino_result_chunks(result: Any, *, audio_duration: float) -> list[_OpenVinoChunk]:
    chunks_raw = getattr(result, "chunks", None)
    chunks_out: list[_OpenVinoChunk] = []
    if chunks_raw is not None and not isinstance(chunks_raw, (str, bytes)):
        for chunk in chunks_raw:
            text = _normalize_asr_text(getattr(chunk, "text", ""))
            if not text:
                continue
            start = _as_float(getattr(chunk, "start_ts", None))
            if start is None:
                start = _as_float(getattr(chunk, "start", None))
            end = _as_float(getattr(chunk, "end_ts", None))
            if end is None:
                end = _as_float(getattr(chunk, "end", None))
            chunks_out.append(
                _OpenVinoChunk(
                    start=float(start or 0.0),
                    end=float(end if end is not None else audio_duration),
                    text=text,
                )
            )
    if chunks_out:
        return chunks_out

    text_single = _normalize_asr_text(getattr(result, "text", ""))
    if text_single:
        return [_OpenVinoChunk(start=0.0, end=max(audio_duration, 0.0), text=text_single)]

    texts = getattr(result, "texts", None)
    if isinstance(texts, list):
        joined = _normalize_asr_text(" ".join(str(item or "") for item in texts))
        if joined:
            logger.warning("OpenVINO ASR returned decoded text without timestamps; falling back to a single segment")
            return [_OpenVinoChunk(start=0.0, end=max(audio_duration, 0.0), text=joined)]
    return []


def _iter_openvino_speech_windows(
    audio_path: Path,
    *,
    vad_enabled: bool,
    vad_threshold: float,
) -> Iterator[tuple[array, float]]:
    for audio_data, window_offset in _iter_wav_windows_as_float_mono_16k(audio_path):
        if not _audio_is_effectively_silent(audio_data):
            spans = _detect_silero_speech_spans(audio_data, threshold=vad_threshold) if vad_enabled else None
            if spans is None:
                yield audio_data, window_offset
            else:
                logger.debug(
                    "openvino-whisper VAD selected %d spans at offset %.2fs for %s",
                    len(spans), window_offset, audio_path,
                )
                for span in spans:
                    start = max(0, min(len(audio_data), span.start_sample))
                    end = max(start, min(len(audio_data), span.end_sample))
                    if end > start:
                        # Materialize only the span being transcribed; never a
                        # list of all audio slices selected by VAD.
                        yield audio_data[start:end], window_offset + start / float(_ASR_SAMPLE_RATE)
        del audio_data


def _openvino_generation_options(
    pipeline: Any,
    *,
    language: str,
    num_beams: int,
    max_new_tokens: int,
) -> tuple[Any, dict[str, Any]]:
    generation_config = None
    if hasattr(pipeline, "get_generation_config"):
        try:
            generation_config = pipeline.get_generation_config()
        except Exception:
            generation_config = None

    generate_kwargs: dict[str, Any] = {}
    lang_token = _normalize_openvino_language(language)

    if generation_config is not None:
        if hasattr(generation_config, "return_timestamps"):
            generation_config.return_timestamps = True
        else:
            generate_kwargs["return_timestamps"] = True
        if hasattr(generation_config, "task"):
            try:
                generation_config.task = "transcribe"
            except Exception:
                pass
        if lang_token is not None:
            if hasattr(generation_config, "language"):
                generation_config.language = lang_token
            else:
                generate_kwargs["language"] = lang_token
        if hasattr(generation_config, "num_beams"):
            generation_config.num_beams = max(1, int(num_beams))
        else:
            generate_kwargs["num_beams"] = max(1, int(num_beams))
        if hasattr(generation_config, "max_new_tokens"):
            generation_config.max_new_tokens = max(1, int(max_new_tokens))
        else:
            generate_kwargs["max_new_tokens"] = max(1, int(max_new_tokens))
    else:
        generate_kwargs = {
            "return_timestamps": True,
            "num_beams": max(1, int(num_beams)),
            "max_new_tokens": max(1, int(max_new_tokens)),
        }
        if lang_token is not None:
            generate_kwargs["language"] = lang_token

    return generation_config, generate_kwargs


def transcribe_openvino_whisper(
    audio_path: Path,
    model_name: str,
    language: str = "auto",
    device: str = "GPU",
    num_beams: int = 1,
    max_new_tokens: int = 448,
    vad_enabled: bool = True,
    vad_threshold: float = _OPENVINO_VAD_THRESHOLD,
) -> list[Segment]:
    model_name = str(model_name or "").strip()
    if not model_name:
        raise RuntimeError("OpenVINO ASR requires a converted Whisper model path")

    pipeline = None
    generation_config = None
    generate_kwargs: dict[str, Any] = {}
    chunks: list[_OpenVinoChunk] = []
    for speech_audio, offset_seconds in _iter_openvino_speech_windows(
        audio_path, vad_enabled=vad_enabled, vad_threshold=vad_threshold,
    ):
        if pipeline is None:
            pipeline = _get_openvino_pipeline(model_name, str(device or "GPU").strip() or "GPU")
            generation_config, generate_kwargs = _openvino_generation_options(
                pipeline, language=language, num_beams=num_beams, max_new_tokens=max_new_tokens,
            )
        # OpenVINO GenAI's std::vector<float> binding accepts packed float
        # sequences such as array('f'), without a Python float list conversion.
        if generation_config is not None:
            result = pipeline.generate(speech_audio, generation_config=generation_config, **generate_kwargs)
        else:
            result = pipeline.generate(speech_audio, **generate_kwargs)
        local_duration = len(speech_audio) / float(_ASR_SAMPLE_RATE)
        chunks.extend(
            _offset_openvino_chunks(
                _openvino_result_chunks(result, audio_duration=local_duration),
                offset_seconds=offset_seconds,
            )
        )
        del result, speech_audio

    out = _dedupe_overlapping_asr_segments(_filter_faster_whisper_segments(chunks))
    if chunks:
        logger.info("openvino-whisper kept %d/%d segments for %s", len(out), len(chunks), audio_path)
    return out


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _segment_letters_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("L"))


def _segment_digits_count(text: str) -> int:
    return sum(1 for ch in text if ch.isdigit())


def _segment_punct_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith(("P", "S")))


def _normalize_asr_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _looks_like_asr_garbage(text: str) -> bool:
    normalized = _normalize_asr_text(text)
    compact = normalized.replace(" ", "")
    if not compact:
        return True

    letters = _segment_letters_count(compact)
    digits = _segment_digits_count(compact)
    punct = _segment_punct_count(compact)

    if letters == 0 and digits == 0:
        return True
    if re.fullmatch(r"(?:\d+[.,:;!?-]*){2,}", compact):
        return True
    if letters == 0 and digits > 0 and punct >= 2:
        return True
    if len(compact) >= 4 and len(set(compact.lower())) == 1:
        return True
    if letters == 0 and digits <= 1 and punct >= max(3, len(compact) - 1):
        return True
    return False


def _segment_is_probably_non_speech(raw_seg: Any, text: str) -> bool:
    if _looks_like_asr_garbage(text):
        return True

    avg_logprob = _as_float(getattr(raw_seg, "avg_logprob", None))
    no_speech_prob = _as_float(getattr(raw_seg, "no_speech_prob", None))
    compression_ratio = _as_float(getattr(raw_seg, "compression_ratio", None))
    letters = _segment_letters_count(text)
    start = _as_float(getattr(raw_seg, "start", None)) or 0.0
    end = _as_float(getattr(raw_seg, "end", None)) or 0.0
    duration = max(0.0, end - start)

    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob >= _FW_NO_SPEECH_THRESHOLD
        and avg_logprob <= _FW_LOG_PROB_THRESHOLD
    ):
        return True
    if compression_ratio is not None and compression_ratio >= _FW_COMPRESSION_RATIO_THRESHOLD and letters <= 2:
        return True
    if avg_logprob is not None and avg_logprob <= -1.2 and letters <= 2 and duration <= 2.0:
        return True
    if no_speech_prob is not None and no_speech_prob >= 0.8 and letters <= 3:
        return True
    return False


def _segment_confidence(raw_seg: Any) -> float | None:
    no_speech_prob = _as_float(getattr(raw_seg, "no_speech_prob", None))
    if no_speech_prob is None:
        return None
    return round(max(0.0, min(1.0, 1.0 - no_speech_prob)), 4)


def _has_credible_speech(segments: list[tuple[Any, str]]) -> bool:
    strong = 0
    total_letters = 0

    for raw_seg, text in segments:
        letters = _segment_letters_count(text)
        total_letters += letters
        avg_logprob = _as_float(getattr(raw_seg, "avg_logprob", None))
        no_speech_prob = _as_float(getattr(raw_seg, "no_speech_prob", None))
        confident = (avg_logprob is None or avg_logprob > -0.7) and (no_speech_prob is None or no_speech_prob < 0.5)
        if letters >= 2 and confident:
            strong += 1

    if strong > 0:
        return True
    return total_letters >= 8


def _filter_faster_whisper_segments(raw_segments: Iterable[Any]) -> list[Segment]:
    kept: list[tuple[Any, str]] = []
    dropped = 0

    for raw_seg in raw_segments:
        text = _normalize_asr_text(getattr(raw_seg, "text", ""))
        if not text or _segment_is_probably_non_speech(raw_seg, text):
            dropped += 1
            continue
        kept.append((raw_seg, text))

    if kept and not _has_credible_speech(kept):
        logger.info("dropping all %d ASR segments as non-credible speech", len(kept))
        return []

    out: list[Segment] = []
    for raw_seg, text in kept:
        out.append(
            Segment(
                start=float(getattr(raw_seg, "start", 0.0) or 0.0),
                end=float(getattr(raw_seg, "end", 0.0) or 0.0),
                text=text,
                confidence=_segment_confidence(raw_seg),
            )
        )

    if dropped:
        logger.info("filtered %d non-speech ASR segments", dropped)
    return out


def translate_segments_mock(segments: Iterable[Segment], target_lang: str) -> list[Segment]:
    prefix = f"（{target_lang}译）"
    out: list[Segment] = []
    for seg in segments:
        out.append(Segment(start=seg.start, end=seg.end, text=prefix + seg.text, confidence=seg.confidence))
    return out


def translate_segments_openai_with_summary(
    segments: Iterable[Segment],
    target_lang: str,
    style: str,
    api_key: str | None = None,
    base_url: str = "",
    model: str = "",
    temperature: float = 0.2,
    timeout_seconds: float = 60.0,
    batch_size: int = 50,
    enable_summary: bool = True,
    glossary: dict[str, str] | None = None,
    rag_context_provider: Callable[[list[Segment], int, str], dict[str, Any] | None] | None = None,
    resume_from: Iterable[Segment] | None = None,
    initial_summary: str = "",
    on_batch_done: Callable[[list[Segment], str, int], None] | None = None,
    ai_service: AIService | None = None,
    enable_thinking: bool = False,
    on_thinking_delta: Callable[[str, int, int], None] | None = None,
    on_batch_start: Callable[[list[Segment], int, str], Any] | None = None,
    rag_context_provider_with_context: Callable[[list[Segment], int, str, Any], dict[str, Any] | None] | None = None,
    on_batch_done_with_context: Callable[[Any, list[Segment], str, int], None] | None = None,
    on_batch_error: Callable[[Any, Exception], None] | None = None,
    on_thinking_delta_with_context: Callable[[str, int, int, Any], None] | None = None,
) -> tuple[list[Segment], str]:
    segs = list(segments)
    if not segs:
        return [], ""
    if ai_service is None and not api_key:
        raise RuntimeError("OpenAI API key is not set")
    resumed = list(resume_from or [])
    if len(resumed) > len(segs):
        raise ValueError("translation resume checkpoint is longer than the source segments")
    cfg: OpenAIChatConfig | None = None
    if ai_service is None:
        cfg = OpenAIChatConfig(
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=float(temperature),
            timeout_seconds=float(timeout_seconds),
        )

    tgt = (target_lang or "zh").strip() or "zh"
    tone = (style or "").strip() or "口语自然"
    batch_size = max(1, int(batch_size))
    system_prompt = "You are a professional subtitle translator. Return ONLY valid JSON (no markdown, no code fences, no extra text)."

    def _client() -> httpx.Client:
        assert cfg is not None
        return create_openai_http_client(cfg.timeout_seconds)

    class _PartialBatchTranslationError(RuntimeError):
        def __init__(self, message: str, *, translated_prefix: list[Segment]) -> None:
            super().__init__(message)
            self.translated_prefix = translated_prefix

    def _translate_batch(
        client: httpx.Client | None,
        batch: list[Segment],
        *,
        start_idx: int,
        summary: str,
        batch_context: Any,
    ) -> tuple[list[Segment], str]:
        blocks = [{"idx": start_idx + i + 1, "text": s.text} for i, s in enumerate(batch)]
        payload_in: dict[str, Any] = {"target_lang": tgt, "style": tone, "blocks": blocks}
        if enable_summary:
            payload_in["summary"] = summary
        if glossary:
            payload_in["glossary"] = glossary
        if rag_context_provider_with_context is not None:
            rag_context = rag_context_provider_with_context(batch, start_idx, summary, batch_context)
            if rag_context:
                payload_in["rag_context"] = rag_context
        elif rag_context_provider is not None:
            rag_context = rag_context_provider(batch, start_idx, summary)
            if rag_context:
                payload_in["rag_context"] = rag_context

        def _thinking_callback(delta: str) -> None:
            if on_thinking_delta_with_context is not None:
                on_thinking_delta_with_context(delta, start_idx + 1, len(batch), batch_context)
            elif on_thinking_delta is not None:
                on_thinking_delta(delta, start_idx + 1, len(batch))

        if ai_service is not None:
            data = ai_service.translate_subtitle_batch(
                blocks=blocks,
                target_lang=tgt,
                style=tone,
                summary=summary,
                enable_summary=enable_summary,
                glossary=glossary,
                rag_context=payload_in.get("rag_context") if isinstance(payload_in.get("rag_context"), dict) else None,
                network_retries=3,
                enable_thinking=enable_thinking,
                on_thinking_delta=_thinking_callback
                if on_thinking_delta_with_context is not None or on_thinking_delta is not None
                else None,
            )
        else:
            assert cfg is not None
            assert client is not None
            user_prompt = (
                "你将收到一批字幕 block。请按 block 为单位翻译。\n"
                "要求：\n"
                "- 保留每个 block 的 idx 不变；不得增删 block，不得改变顺序；\n"
                "- 只翻译 text 字段；同一 block 内多行先合并理解再翻译；\n"
                "- 术语、人名保持一致；数字/单位尽量保留原格式；\n"
                "- 如果输入包含 rag_context，请优先参考其中的 term_cards/knowledge_cards 来理解专有名词、梗、作品设定和技术背景；\n"
                "- term_cards 中的 translation 是推荐译法，除非明显不符合当前上下文，否则保持一致；\n"
                "- rag_context 来自主 agent 对当前 block 的本地 RAG/词典预检和必要研究；如果其中已有与当前 block 和 summary 贴切的译法或解释，直接据此翻译，不要假设还必须继续搜索；\n"
                "- 输出必须是 JSON 对象，且必须包含 translations 数组；不要输出任何解释。\n"
                f"- 目标语言：{tgt}\n"
                f"- 风格：{tone}\n\n"
                "如果输入里带 summary，请在翻译时参考它保持前后一致，并输出 updated_summary（<= 500 字符）。\n\n"
                "输入 JSON：\n"
                f"{json.dumps(payload_in, ensure_ascii=False)}\n\n"
                "输出 JSON 结构（必须严格遵守）：\n"
                '{ "updated_summary": "...", "translations": [ {"idx": 1, "text": "..."}, ... ] }'
            )
            request_kwargs = {
                "config": cfg,
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "client": client,
                "format_retry_notice": "注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
                "format_retries": 2,
                "network_retries": 3,
            }
            if enable_thinking:
                data = request_openai_json_object_with_thinking(
                    **request_kwargs,
                    on_thinking_delta=_thinking_callback
                    if on_thinking_delta_with_context is not None or on_thinking_delta is not None
                    else None,
                )
            else:
                data = request_openai_json_object(**request_kwargs)

        translations = data.get("translations")
        if not isinstance(translations, list):
            raise RuntimeError("OpenAI output missing 'translations' list")

        mapping: dict[int, str] = {}
        for item in translations:
            if not isinstance(item, dict):
                continue
            if "idx" not in item or "text" not in item:
                continue
            try:
                idx = int(item["idx"])
            except Exception:
                continue
            mapping[idx] = str(item["text"])

        expected = [b["idx"] for b in blocks]
        missing = [i for i in expected if i not in mapping]
        if missing:
            partial_prefix: list[Segment] = []
            for i, orig in enumerate(batch):
                idx = start_idx + i + 1
                translated_text = mapping.get(idx)
                if translated_text is None:
                    break
                partial_prefix.append(
                    Segment(
                        start=orig.start,
                        end=orig.end,
                        text=translated_text.strip(),
                        confidence=orig.confidence,
                    )
                )
            if partial_prefix:
                raise _PartialBatchTranslationError(
                    f"OpenAI output missing translations for idx: {missing[:5]}",
                    translated_prefix=partial_prefix,
                )
            raise RuntimeError(f"OpenAI output missing translations for idx: {missing[:5]}")

        updated_summary = summary
        if enable_summary and isinstance(data.get("updated_summary"), str):
            updated_summary = str(data.get("updated_summary")).strip()[:500]

        out_batch: list[Segment] = []
        for i, orig in enumerate(batch):
            idx = start_idx + i + 1
            out_batch.append(
                Segment(
                    start=orig.start,
                    end=orig.end,
                    text=mapping[idx].strip(),
                    confidence=orig.confidence,
                )
            )
        return out_batch, updated_summary

    summary = str(initial_summary or "").strip()[:500] if enable_summary else ""
    out: list[Segment] = list(resumed)
    cur_batch_size = batch_size
    idx = len(out)
    client_context = _client() if ai_service is None else nullcontext(None)
    with client_context as client:
        while idx < len(segs):
            size = min(cur_batch_size, len(segs) - idx)
            batch = segs[idx : idx + size]
            batch_context = on_batch_start(batch, idx, summary) if on_batch_start is not None else None
            try:
                translated, summary = _translate_batch(
                    client,
                    batch,
                    start_idx=idx,
                    summary=summary,
                    batch_context=batch_context,
                )
                out.extend(translated)
                idx += size
                if on_batch_done is not None:
                    on_batch_done(translated, summary, idx)
                if on_batch_done_with_context is not None:
                    on_batch_done_with_context(batch_context, translated, summary, idx)
            except _PartialBatchTranslationError as e:
                if not e.translated_prefix:
                    if on_batch_error is not None:
                        on_batch_error(batch_context, e)
                    raise
                out.extend(e.translated_prefix)
                idx += len(e.translated_prefix)
                if on_batch_done is not None:
                    on_batch_done(e.translated_prefix, summary, idx)
                if on_batch_done_with_context is not None:
                    on_batch_done_with_context(batch_context, e.translated_prefix, summary, idx)
                if cur_batch_size > 1:
                    cur_batch_size = max(1, cur_batch_size // 2)
                    continue
                if on_batch_error is not None:
                    on_batch_error(batch_context, e)
                raise RuntimeError(str(e)) from e
            except httpx.TimeoutException as e:
                if on_batch_error is not None:
                    on_batch_error(batch_context, e)
                if cur_batch_size <= 1:
                    raise
                cur_batch_size = max(1, cur_batch_size // 2)
            except httpx.TransportError as e:
                if on_batch_error is not None:
                    on_batch_error(batch_context, e)
                if cur_batch_size <= 1:
                    raise
                cur_batch_size = max(1, cur_batch_size // 2)
            except Exception as e:
                if on_batch_error is not None:
                    on_batch_error(batch_context, e)
                raise

    return out, summary


def translate_segments_openai(
    segments: Iterable[Segment],
    target_lang: str,
    style: str,
    api_key: str | None,
    base_url: str,
    model: str,
    temperature: float = 0.2,
    timeout_seconds: float = 60.0,
    batch_size: int = 50,
    enable_summary: bool = True,
    glossary: dict[str, str] | None = None,
    rag_context_provider: Callable[[list[Segment], int, str], dict[str, Any] | None] | None = None,
) -> list[Segment]:
    out, _summary = translate_segments_openai_with_summary(
        segments,
        target_lang=target_lang,
        style=style,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        batch_size=batch_size,
        enable_summary=enable_summary,
        glossary=glossary,
        rag_context_provider=rag_context_provider,
    )
    return out
def _srt_ts(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def segments_to_srt(segments: Iterable[Segment]) -> str:
    lines: list[str] = []
    for idx, seg in enumerate(segments, start=1):
        lines.append(str(idx))
        lines.append(f"{_srt_ts(seg.start)} --> {_srt_ts(seg.end)}")
        body = seg.text.strip()
        secondary = str(seg.secondary_text or "").strip()
        if secondary:
            body = f"{body}\n{secondary}" if body else secondary
        lines.append(body)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


_SRT_TIME_RE = re.compile(r"(?P<start>\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2},\d{3})")


def _parse_srt_ts(ts: str) -> float:
    h_s, m_s, rest = (ts or "").strip().split(":", 2)
    s_s, ms_s = rest.split(",", 1)
    h = int(h_s)
    m = int(m_s)
    s = int(s_s)
    ms = int(ms_s)
    return float(h * 3600 + m * 60 + s) + float(ms) / 1000.0


def srt_to_segments(srt_text: str) -> list[Segment]:
    text = (srt_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    blocks = re.split(r"\n{2,}", text)
    out: list[Segment] = []
    for block in blocks:
        lines = [ln.rstrip() for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue

        m = None
        time_idx = -1
        for i, ln in enumerate(lines[:3]):
            m = _SRT_TIME_RE.search(ln)
            if m:
                time_idx = i
                break
        if not m or time_idx < 0:
            continue

        try:
            start = _parse_srt_ts(m.group("start"))
            end = _parse_srt_ts(m.group("end"))
        except Exception:
            continue

        body = "\n".join(lines[time_idx + 1 :]).strip()
        if not body:
            continue
        out.append(Segment(start=start, end=end, text=body))

    return out


def _ass_ts(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def probe_video_resolution(ffmpeg_path: str, video_path: Path) -> tuple[int, int]:
    ffmpeg_cmd = str(ffmpeg_path or "").strip() or "ffmpeg"
    ffmpeg_bin = Path(ffmpeg_cmd)
    ffprobe_name = "ffprobe" + ffmpeg_bin.suffix if ffmpeg_bin.suffix else "ffprobe"
    candidates = [str(ffmpeg_bin.with_name(ffprobe_name)), shutil.which("ffprobe") or "ffprobe"]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [
                    candidate,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(video_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(proc.stdout or "{}")
            streams = data.get("streams")
            if not isinstance(streams, list) or not streams:
                continue
            stream = streams[0] if isinstance(streams[0], dict) else {}
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            continue

    return 1920, 1080


def _pixel_format_bit_depth(pixel_format: str) -> int:
    value = str(pixel_format or "").strip().lower()
    if not value:
        return 8
    match = re.search(r"(?:^p0|p)(9|10|12|14|16)(?:le|be)?$", value)
    if match:
        return int(match.group(1))
    match = re.search(r"^(?:gray|ya)(9|10|12|14|16)(?:le|be)?$", value)
    if match:
        return int(match.group(1))
    if re.match(r"^(?:rgb|bgr)48(?:le|be)?$", value):
        return 16
    if re.match(r"^(?:rgba|bgra)64(?:le|be)?$", value):
        return 16
    return 8


def probe_video_bit_depth(ffmpeg_path: str, video_path: Path) -> int:
    """Return the source video component depth, defaulting safely to 8-bit."""
    ffmpeg_cmd = str(ffmpeg_path or "").strip() or "ffmpeg"
    ffmpeg_bin = Path(ffmpeg_cmd)
    ffprobe_name = "ffprobe" + ffmpeg_bin.suffix if ffmpeg_bin.suffix else "ffprobe"
    candidates = dict.fromkeys((str(ffmpeg_bin.with_name(ffprobe_name)), shutil.which("ffprobe") or "ffprobe"))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [
                    candidate,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=pix_fmt,bits_per_raw_sample",
                    "-of",
                    "json",
                    str(video_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            data = json.loads(proc.stdout or "{}")
            streams = data.get("streams")
            if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
                continue
            stream = streams[0]
            try:
                raw_depth = int(str(stream.get("bits_per_raw_sample") or "").strip())
            except (TypeError, ValueError):
                raw_depth = 0
            pixel_format_depth = _pixel_format_bit_depth(str(stream.get("pix_fmt") or ""))
            if raw_depth > 0:
                return max(raw_depth, pixel_format_depth)
            return pixel_format_depth
        except Exception:
            continue

    return 8


def _char_width_units(ch: str) -> float:
    if not ch:
        return 0.0
    if ch.isspace():
        return 0.28
    east_asian = unicodedata.east_asian_width(ch)
    if east_asian in {"W", "F"}:
        return 0.9
    if ord(ch) < 128:
        if ch in "ilI.,'`!|:;":
            return 0.3
        if ch in "MW@#%&":
            return 0.84
        return 0.56
    if unicodedata.category(ch).startswith("P"):
        return 0.42
    return 0.68


def _text_width_units(text: str) -> float:
    return sum(_char_width_units(ch) for ch in text)


def _split_long_token(token: str, max_units: float) -> list[str]:
    pieces: list[str] = []
    current = ""
    current_units = 0.0
    limit = max(1.0, float(max_units or 1.0))
    for ch in token:
        ch_units = _char_width_units(ch)
        if current and current_units + ch_units > limit:
            pieces.append(current)
            current = ch
            current_units = ch_units
            continue
        current += ch
        current_units += ch_units
    if current:
        pieces.append(current)
    return pieces


def _wrap_ass_line(text: str, max_units: float) -> list[str]:
    normalized = re.sub(r"[ \t\f\v]+", " ", str(text or "").strip())
    if not normalized:
        return []

    lines: list[str] = []
    current = ""
    current_units = 0.0

    for token in re.findall(r"\S+|\s+", normalized):
        if token.isspace():
            if not current or current.endswith(" "):
                continue
            space_units = _char_width_units(" ")
            if current_units + space_units <= max_units:
                current += " "
                current_units += space_units
            else:
                lines.append(current.rstrip())
                current = ""
                current_units = 0.0
            continue

        token_units = _text_width_units(token)
        if current and current_units + token_units <= max_units:
            current += token
            current_units += token_units
            continue

        if current:
            lines.append(current.rstrip())
            current = ""
            current_units = 0.0

        if token_units <= max_units:
            current = token
            current_units = token_units
            continue

        pieces = _split_long_token(token, max_units)
        if pieces:
            lines.extend(pieces[:-1])
            current = pieces[-1]
            current_units = _text_width_units(current)

    if current.strip():
        lines.append(current.rstrip())

    return [line for line in lines if line]


def _prepare_ass_lines(text: str, max_units: float) -> list[str]:
    out: list[str] = []
    for raw_line in str(text or "").replace("\r", "").split("\n"):
        wrapped = _wrap_ass_line(raw_line, max_units)
        out.extend(wrapped)
    return [line for line in out if line]


def _wrap_ass_text(
    text: str,
    max_units: float,
    *,
    secondary_text: str | None = None,
    secondary_max_units: float | None = None,
    secondary_style_name: str = "Secondary",
) -> str:
    primary_lines = _prepare_ass_lines(text, max_units)
    secondary_lines = _prepare_ass_lines(secondary_text or "", secondary_max_units or max_units)

    if not secondary_lines:
        return "\\N".join(primary_lines)

    out: list[str] = []
    for line in primary_lines:
        out.append(f"{{\\rDefault}}{line}")
    for line in secondary_lines:
        out.append(f"{{\\r{secondary_style_name}}}{line}")
    return "\\N".join(out)


def segments_to_ass(
    segments: Iterable[Segment],
    style_name: str = "clean_white",
    *,
    play_res_x: int = 1920,
    play_res_y: int = 1080,
    secondary_line_scale: float | None = None,
    primary_font_scale_percent: int | float = 100,
    secondary_font_scale_percent: int | float = 100,
) -> str:
    if style_name not in {"clean_white"}:
        style_name = "clean_white"

    play_res_x = max(320, int(play_res_x or 1920))
    play_res_y = max(320, int(play_res_y or 1080))
    portrait = play_res_y > play_res_x
    margin_x = max(36, int(round(play_res_x * (0.055 if portrait else 0.052))))
    margin_v = max(48, int(round(play_res_y * (0.06 if portrait else 0.055))))
    font_basis = play_res_x if portrait else min(play_res_x, play_res_y)
    base_font_size = max(32, int(round(font_basis * (0.052 if portrait else 0.054))))
    try:
        primary_scale = max(0.25, float(primary_font_scale_percent) / 100.0)
    except Exception:
        primary_scale = 1.0
    try:
        secondary_scale = max(0.25, float(secondary_font_scale_percent) / 100.0)
    except Exception:
        secondary_scale = 1.0

    font_size = max(12, int(round(base_font_size * primary_scale)))
    secondary_font_size = None
    if secondary_line_scale is not None:
        secondary_base_font_size = max(22, int(round(base_font_size * float(secondary_line_scale))))
        secondary_font_size = max(10, int(round(secondary_base_font_size * secondary_scale)))
    outline = max(1, int(round(font_size * 0.035)))
    secondary_outline = max(1, int(round((secondary_font_size or font_size) * 0.035)))
    max_line_units = max(10.0, ((play_res_x - margin_x * 2) / max(font_size, 1)) * 1.0)
    secondary_max_line_units = max_line_units
    if secondary_font_size is not None:
        secondary_max_line_units = max(10.0, ((play_res_x - margin_x * 2) / max(secondary_font_size, 1)) * 1.0)

    # Use a CJK-capable font by default so burn-in works in minimal containers.
    style_default = (
        f"Style: Default,Noto Sans CJK SC,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{outline},0,2,{margin_x},{margin_x},{margin_v},1"
    )
    style_secondary = None
    if secondary_font_size is not None:
        style_secondary = (
            f"Style: Secondary,Noto Sans CJK SC,{secondary_font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
            f"0,0,0,0,100,100,0,0,1,{secondary_outline},0,2,{margin_x},{margin_x},{margin_v},1"
        )

    events: list[str] = []
    for seg in segments:
        text = _wrap_ass_text(
            seg.text,
            max_line_units,
            secondary_text=seg.secondary_text,
            secondary_max_units=secondary_max_line_units,
        ).strip()
        if not text:
            continue
        events.append(f"Dialogue: 0,{_ass_ts(seg.start)},{_ass_ts(seg.end)},Default,,0,0,0,,{text}")

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {play_res_x}",
            f"PlayResY: {play_res_y}",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            style_default,
            *( [style_secondary] if style_secondary else [] ),
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            *events,
            "",
        ]
    )


def _intel_vaapi_device_args(device: str) -> list[str]:
    return [
        "-init_hw_device",
        f"vaapi=va:{device}",
        "-filter_hw_device",
        "va",
    ]


def _intel_vaapi_decode_args() -> list[str]:
    return [
        "-hwaccel",
        "vaapi",
        "-hwaccel_device",
        "va",
        "-hwaccel_output_format",
        "vaapi",
    ]


def _vaapi_hardware_decode_available(
    ffmpeg_path: str,
    video_path: Path,
    *,
    device: str,
    vaapi_pixel_format: str,
    software_pixel_format: str,
    log_path: Path | None,
    live_upload_cb: Callable[[], None] | None,
) -> bool:
    """Decode one frame through VAAPI before committing to a full render."""
    cmd = [
        ffmpeg_path,
        "-hide_banner",
        "-v",
        "error",
        *_intel_vaapi_device_args(device),
        *_intel_vaapi_decode_args(),
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"scale_vaapi=format={vaapi_pixel_format},hwdownload,format={software_pixel_format}",
        "-f",
        "null",
        "-",
    ]
    try:
        _run_logged(cmd, log_path=log_path, live_upload_cb=live_upload_cb)
    except (OSError, subprocess.CalledProcessError) as exc:
        message = (
            "VAAPI hardware decode preflight failed; falling back to software decode with VAAPI encode "
            f"({type(exc).__name__})"
        )
        logger.warning("%s: %s", message, video_path)
        _append_processing_log_note(log_path, message)
        return False
    _append_processing_log_note(
        log_path,
        f"VAAPI hardware decode preflight succeeded: format={software_pixel_format}",
    )
    return True


def render_burn_in(
    ffmpeg_path: str,
    video_path: Path,
    ass_path: Path,
    output_path: Path,
    *,
    video_codec: str = "av1",
    use_intel_gpu: bool = False,
    intel_gpu_render_device: str = "/dev/dri/renderD128",
    preset: str | int | None = None,
    crf: int | None = None,
    log_path: Path | None = None,
    live_upload_cb: Callable[[], None] | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    codec = str(video_codec or "").strip().lower() or "av1"
    ass_filter = f"ass={str(ass_path).replace(':', r'\:')}"
    cmd = [ffmpeg_path, "-y"]
    intel_h264_preset_quality = {
        "placebo": 1,
        "veryslow": 1,
        "slower": 2,
        "slow": 3,
        "medium": 4,
        "fast": 5,
        "faster": 6,
        "veryfast": 7,
        "superfast": 8,
        "ultrafast": 8,
    }

    def _intel_av1_quality(val: str | int | None) -> int | None:
        preset_s = str(val or "").strip().lower()
        if not preset_s:
            return None
        if preset_s in intel_h264_preset_quality:
            return intel_h264_preset_quality[preset_s]
        try:
            preset_n = int(preset_s)
        except Exception:
            return None
        preset_n = max(0, min(13, preset_n))
        return 1 + round((preset_n * 7) / 13)

    if use_intel_gpu:
        if codec not in {"h264", "avc", "av1"}:
            raise ValueError("Intel GPU burn-in currently supports only h264/av1")
        device = str(intel_gpu_render_device or "").strip() or "/dev/dri/renderD128"
        if not Path(device).exists():
            raise FileNotFoundError(f"Intel GPU render device not found: {device}")
        if codec in {"h264", "avc"}:
            if not _ffmpeg_supports_encoder(ffmpeg_path, "h264_vaapi"):
                raise RuntimeError(
                    "Current ffmpeg build does not support h264_vaapi; rebuild the image with VAAPI support or disable Intel GPU burn-in."
                )
            effective_qp = 23 if crf is None else max(0, min(51, int(crf)))
            video_args = ["-c:v", "h264_vaapi", "-rc_mode", "CQP", "-qp", str(effective_qp)]
            quality = intel_h264_preset_quality.get(str(preset or "").strip().lower())
        else:
            if not _ffmpeg_supports_encoder(ffmpeg_path, "av1_vaapi"):
                raise RuntimeError(
                    "Current ffmpeg build does not support av1_vaapi; switch video_codec to h264 for Intel GPU burn-in, or disable Intel GPU and keep CPU AV1."
                )
            effective_global_quality = 24 if crf is None else max(0, min(63, int(crf)))
            video_args = ["-c:v", "av1_vaapi", "-rc_mode", "CQP", "-global_quality", str(effective_global_quality)]
            quality = _intel_av1_quality(preset)
        if quality is not None:
            video_args.extend(["-quality", str(quality)])
        source_bit_depth = probe_video_bit_depth(ffmpeg_path, video_path)
        preserve_10bit = codec == "av1" and source_bit_depth > 8
        target_bit_depth = 10 if preserve_10bit else 8
        vaapi_pixel_format = "p010" if preserve_10bit else "nv12"
        software_pixel_format = "p010le" if preserve_10bit else "nv12"
        cmd.extend(_intel_vaapi_device_args(device))
        hardware_decode = _vaapi_hardware_decode_available(
            ffmpeg_path,
            video_path,
            device=device,
            vaapi_pixel_format=vaapi_pixel_format,
            software_pixel_format=software_pixel_format,
            log_path=log_path,
            live_upload_cb=live_upload_cb,
        )
        if hardware_decode:
            cmd.extend(_intel_vaapi_decode_args())
            filter_arg = (
                f"scale_vaapi=format={vaapi_pixel_format},"
                f"hwdownload,format={software_pixel_format},"
                f"{ass_filter},format={software_pixel_format},hwupload"
            )
            decode_mode = "vaapi"
        else:
            filter_arg = f"{ass_filter},format={software_pixel_format},hwupload"
            decode_mode = "software-fallback"
        bit_depth_note = (
            f"Intel render pipeline: decode={decode_mode} source_bit_depth={source_bit_depth} "
            f"output_bit_depth={target_bit_depth} software_format={software_pixel_format}"
        )
        if codec in {"h264", "avc"} and source_bit_depth > 8:
            bit_depth_note += " (h264_vaapi output is limited to the 8-bit NV12 path)"
        logger.info(bit_depth_note)
        _append_processing_log_note(log_path, bit_depth_note)
    elif codec in {"h264", "avc"}:
        effective_crf = 18 if crf is None else max(0, min(51, int(crf)))
        allowed_presets = {
            "ultrafast",
            "superfast",
            "veryfast",
            "faster",
            "fast",
            "medium",
            "slow",
            "slower",
            "veryslow",
            "placebo",
        }
        preset_s = str(preset or "").strip().lower()
        effective_preset = preset_s if preset_s in allowed_presets else "veryfast"
        video_args = ["-c:v", "libx264", "-preset", effective_preset, "-crf", str(effective_crf)]
        filter_arg = ass_filter
    else:
        # AV1 default: SVT-AV1, balanced preset and constant quality.
        # Note: preset range is 0..13 (lower = slower/better).
        effective_crf = 24 if crf is None else max(0, min(63, int(crf)))
        preset_n: int | None = None
        try:
            preset_s = str(preset).strip() if preset is not None else ""
            preset_n = int(preset_s) if preset_s else None
        except Exception:
            preset_n = None
        effective_preset_n = 4 if preset_n is None else max(0, min(13, preset_n))
        video_args = ["-c:v", "libsvtav1", "-preset", str(effective_preset_n), "-crf", str(effective_crf)]
        filter_arg = ass_filter

    cmd.extend(
        [
            "-i",
            str(video_path),
            "-vf",
            filter_arg,
            *video_args,
            "-c:a",
            "copy",
            str(output_path),
        ]
    )
    _run_logged(cmd, log_path=log_path, live_upload_cb=live_upload_cb)


def mux_soft_sub(
    ffmpeg_path: str,
    video_path: Path,
    srt_path: Path,
    output_path: Path,
    *,
    log_path: Path | None = None,
    live_upload_cb: Callable[[], None] | None = None,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(srt_path),
        "-map",
        "0",
        "-map",
        "1",
        "-c:v",
        "copy",
        "-c:a",
        "copy",
        "-c:s",
        "srt",
        "-metadata:s:s:0",
        "language=chi",
        str(output_path),
    ]
    _run_logged(cmd, log_path=log_path, live_upload_cb=live_upload_cb)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
