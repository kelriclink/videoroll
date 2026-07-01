from __future__ import annotations

import inspect
import json
import logging
import re
import shutil
import subprocess
import threading
import time
import unicodedata
import wave
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from videoroll.ai.client import OpenAIChatConfig, create_openai_http_client, request_openai_json_object

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
_OPENVINO_PIPELINE_CACHE: dict[tuple[str, str], Any] = {}
_OPENVINO_PIPELINE_CACHE_LOCK = threading.Lock()


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

    try:
        seg_iter, _info = model.transcribe(str(audio_path), **transcribe_kwargs)
    except ValueError as e:
        # Some faster-whisper versions can raise on language detection if VAD removes all speech.
        if "empty sequence" in str(e).lower():
            logger.info("faster-whisper returned no speech after VAD for %s", audio_path)
            return []
        raise

    raw_segments = list(seg_iter)
    out = _filter_faster_whisper_segments(raw_segments)
    if raw_segments:
        logger.info(
            "faster-whisper kept %d/%d segments for %s",
            len(out),
            len(raw_segments),
            audio_path,
        )
    return out


@dataclass(frozen=True)
class _OpenVinoChunk:
    start: float
    end: float
    text: str


def _read_wav_as_float_mono_16k(audio_path: Path) -> tuple[list[float], float]:
    with wave.open(str(audio_path), "rb") as wf:
        channels = int(wf.getnchannels() or 0)
        sample_rate = int(wf.getframerate() or 0)
        sample_width = int(wf.getsampwidth() or 0)
        frame_count = int(wf.getnframes() or 0)
        raw = wf.readframes(frame_count)

    if channels != 1:
        raise RuntimeError(f"OpenVINO ASR expects mono WAV, got channels={channels}")
    if sample_rate != 16000:
        raise RuntimeError(f"OpenVINO ASR expects 16k WAV, got sample_rate={sample_rate}")

    if sample_width == 2:
        import array

        ints = array.array("h")
        ints.frombytes(raw)
        data = [max(-1.0, min(1.0, sample / 32768.0)) for sample in ints]
    elif sample_width == 1:
        data = [((byte - 128) / 128.0) for byte in raw]
    elif sample_width == 4:
        import array

        ints = array.array("i")
        ints.frombytes(raw)
        data = [max(-1.0, min(1.0, sample / 2147483648.0)) for sample in ints]
    else:
        raise RuntimeError(f"unsupported WAV sample width for OpenVINO ASR: {sample_width} bytes")

    duration = (len(data) / float(sample_rate)) if sample_rate > 0 else 0.0
    return data, duration


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
        audio_data, _duration = _read_wav_as_float_mono_16k(audio_path)
    except Exception as e:
        logger.debug("skipping WAV silence probe for %s: %s: %s", audio_path, type(e).__name__, e)
        return False
    return _audio_is_effectively_silent(audio_data)


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


def transcribe_openvino_whisper(
    audio_path: Path,
    model_name: str,
    language: str = "auto",
    device: str = "GPU",
    num_beams: int = 1,
    max_new_tokens: int = 448,
) -> list[Segment]:
    model_name = str(model_name or "").strip()
    if not model_name:
        raise RuntimeError("OpenVINO ASR requires a converted Whisper model path")

    audio_data, audio_duration = _read_wav_as_float_mono_16k(audio_path)
    if not audio_data:
        return []
    if _audio_is_effectively_silent(audio_data):
        logger.info("skipping openvino-whisper for effectively silent audio %s", audio_path)
        return []

    pipeline = _get_openvino_pipeline(model_name, str(device or "GPU").strip() or "GPU")
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

    if generation_config is not None:
        result = pipeline.generate(audio_data, generation_config=generation_config, **generate_kwargs)
    else:
        result = pipeline.generate(audio_data, **generate_kwargs)

    chunks = _openvino_result_chunks(result, audio_duration=audio_duration)
    out = _filter_faster_whisper_segments(chunks)
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
    api_key: str | None,
    base_url: str,
    model: str,
    temperature: float = 0.2,
    timeout_seconds: float = 60.0,
    batch_size: int = 50,
    enable_summary: bool = True,
    glossary: dict[str, str] | None = None,
    rag_context_provider: Callable[[list[Segment], int, str], dict[str, Any] | None] | None = None,
    resume_from: Iterable[Segment] | None = None,
    initial_summary: str = "",
    on_batch_done: Callable[[list[Segment], str, int], None] | None = None,
) -> tuple[list[Segment], str]:
    segs = list(segments)
    if not segs:
        return [], ""
    if not api_key:
        raise RuntimeError("OpenAI API key is not set")
    resumed = list(resume_from or [])
    if len(resumed) > len(segs):
        raise ValueError("translation resume checkpoint is longer than the source segments")
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
        return create_openai_http_client(cfg.timeout_seconds)

    class _PartialBatchTranslationError(RuntimeError):
        def __init__(self, message: str, *, translated_prefix: list[Segment]) -> None:
            super().__init__(message)
            self.translated_prefix = translated_prefix

    def _translate_batch(client: httpx.Client, batch: list[Segment], *, start_idx: int, summary: str) -> tuple[list[Segment], str]:
        blocks = [{"idx": start_idx + i + 1, "text": s.text} for i, s in enumerate(batch)]
        payload_in: dict[str, Any] = {"target_lang": tgt, "style": tone, "blocks": blocks}
        if enable_summary:
            payload_in["summary"] = summary
        if glossary:
            payload_in["glossary"] = glossary
        if rag_context_provider is not None:
            rag_context = rag_context_provider(batch, start_idx, summary)
            if rag_context:
                payload_in["rag_context"] = rag_context

        user_prompt = (
            "你将收到一批字幕 block。请按 block 为单位翻译。\n"
            "要求：\n"
            "- 保留每个 block 的 idx 不变；不得增删 block，不得改变顺序；\n"
            "- 只翻译 text 字段；同一 block 内多行先合并理解再翻译；\n"
            "- 术语、人名保持一致；数字/单位尽量保留原格式；\n"
            "- 如果输入包含 rag_context，请优先参考其中的 term_cards/knowledge_cards 来理解专有名词、梗、作品设定和技术背景；\n"
            "- term_cards 中的 translation 是推荐译法，除非明显不符合当前上下文，否则保持一致；\n"
            "- 输出必须是 JSON 对象，且必须包含 translations 数组；不要输出任何解释。\n"
            f"- 目标语言：{tgt}\n"
            f"- 风格：{tone}\n\n"
            "如果输入里带 summary，请在翻译时参考它保持前后一致，并输出 updated_summary（<= 500 字符）。\n\n"
            "输入 JSON：\n"
            f"{json.dumps(payload_in, ensure_ascii=False)}\n\n"
            "输出 JSON 结构（必须严格遵守）：\n"
            '{ "updated_summary": "...", "translations": [ {"idx": 1, "text": "..."}, ... ] }'
        )

        data = request_openai_json_object(
            config=cfg,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            client=client,
            format_retry_notice="注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。",
            format_retries=2,
            network_retries=3,
        )

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
    with _client() as client:
        while idx < len(segs):
            size = min(cur_batch_size, len(segs) - idx)
            batch = segs[idx : idx + size]
            try:
                translated, summary = _translate_batch(client, batch, start_idx=idx, summary=summary)
                out.extend(translated)
                idx += size
                if on_batch_done is not None:
                    on_batch_done(translated, summary, idx)
            except _PartialBatchTranslationError as e:
                if not e.translated_prefix:
                    raise
                out.extend(e.translated_prefix)
                idx += len(e.translated_prefix)
                if on_batch_done is not None:
                    on_batch_done(e.translated_prefix, summary, idx)
                if cur_batch_size > 1:
                    cur_batch_size = max(1, cur_batch_size // 2)
                    continue
                raise RuntimeError(str(e)) from e
            except httpx.TimeoutException:
                if cur_batch_size <= 1:
                    raise
                cur_batch_size = max(1, cur_batch_size // 2)
            except httpx.TransportError:
                if cur_batch_size <= 1:
                    raise
                cur_batch_size = max(1, cur_batch_size // 2)

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
        cmd.extend(["-vaapi_device", device])
        filter_arg = f"{ass_filter},format=nv12,hwupload"
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
