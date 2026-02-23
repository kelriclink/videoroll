from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from videoroll.utils.openai_compat import build_openai_chat_completions_url


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    confidence: float | None = None


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def extract_audio(ffmpeg_path: str, video_path: Path, audio_path: Path) -> None:
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
    _run(cmd)


def transcribe_mock(_audio_path: Path) -> list[Segment]:
    return [
        Segment(
            start=0.0,
            end=5.0,
            text="（示例字幕：当前使用 mock ASR。设置 SUBTITLE_ASR_ENGINE=faster-whisper 并安装可选依赖以获得真实转写。）",
            confidence=1.0,
        )
    ]


def transcribe_faster_whisper(
    audio_path: Path,
    model_name: str,
    language: str = "auto",
    device: str = "cpu",
    compute_type: str = "int8",
) -> list[Segment]:
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except Exception as e:  # pragma: no cover
        raise RuntimeError("faster-whisper is not installed. Rebuild with INSTALL_ASR=1.") from e

    lang = None if language in {"", "auto", None} else language
    model = WhisperModel(model_name, device=device, compute_type=compute_type)
    seg_iter, _info = model.transcribe(str(audio_path), language=lang)
    out: list[Segment] = []
    for seg in seg_iter:
        out.append(Segment(start=float(seg.start), end=float(seg.end), text=str(seg.text).strip()))
    return out


def translate_segments_mock(segments: Iterable[Segment], target_lang: str) -> list[Segment]:
    prefix = f"（{target_lang}译）"
    out: list[Segment] = []
    for seg in segments:
        out.append(Segment(start=seg.start, end=seg.end, text=prefix + seg.text, confidence=seg.confidence))
    return out


def _strip_code_fence(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = t.splitlines()
        # ```json
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        # trailing ```
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


def _openai_extract_content(resp_json: dict[str, Any]) -> str:
    try:
        return str(resp_json["choices"][0]["message"]["content"])
    except Exception as e:
        raise RuntimeError(f"unexpected OpenAI response shape: {resp_json}") from e


def _resp_snippet(resp: httpx.Response, limit: int = 200) -> str:
    try:
        text = resp.text or ""
    except Exception:
        return ""
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


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
) -> tuple[list[Segment], str]:
    segs = list(segments)
    if not segs:
        return [], ""
    if not api_key:
        raise RuntimeError("OpenAI API key is not set")

    tgt = (target_lang or "zh").strip() or "zh"
    tone = (style or "").strip() or "口语自然"
    batch_size = max(1, int(batch_size))

    url = build_openai_chat_completions_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}

    system_prompt = (
        "You are a professional subtitle translator. "
        "Return ONLY valid JSON (no markdown, no code fences, no extra text)."
    )

    def _sleep_backoff(attempt: int) -> None:
        # Jittered exponential backoff: 1s, 2s, 4s (capped) + small jitter.
        base = min(8.0, float(2**attempt))
        time.sleep(base + random.random() * 0.25)

    def _client() -> httpx.Client:
        t = float(timeout_seconds)
        timeout = httpx.Timeout(t, connect=min(10.0, t), read=t, write=t, pool=t)
        return httpx.Client(timeout=timeout)

    def _translate_batch(client: httpx.Client, batch: list[Segment], *, start_idx: int, summary: str) -> tuple[list[Segment], str]:
        blocks = [{"idx": start_idx + i + 1, "text": s.text} for i, s in enumerate(batch)]
        payload_in: dict[str, Any] = {"target_lang": tgt, "style": tone, "blocks": blocks}
        if enable_summary:
            payload_in["summary"] = summary
        if glossary:
            payload_in["glossary"] = glossary

        user_prompt = (
            "你将收到一批字幕 block。请按 block 为单位翻译。\n"
            "要求：\n"
            "- 保留每个 block 的 idx 不变；不得增删 block，不得改变顺序；\n"
            "- 只翻译 text 字段；同一 block 内多行先合并理解再翻译；\n"
            "- 术语、人名保持一致；数字/单位尽量保留原格式；\n"
            "- 输出必须是 JSON 对象，且必须包含 translations 数组；不要输出任何解释。\n"
            f"- 目标语言：{tgt}\n"
            f"- 风格：{tone}\n\n"
            "如果输入里带 summary，请在翻译时参考它保持前后一致，并输出 updated_summary（<= 500 字符）。\n\n"
            "输入 JSON：\n"
            f"{json.dumps(payload_in, ensure_ascii=False)}\n\n"
            "输出 JSON 结构（必须严格遵守）：\n"
            '{ "updated_summary": "...", "translations": [ {"idx": 1, "text": "..."}, ... ] }'
        )

        req: dict[str, Any] = {
            "model": model,
            "temperature": float(temperature),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        last_err: Exception | None = None
        for format_attempt in range(2):
            if format_attempt > 0:
                req["messages"][-1]["content"] = user_prompt + "\n\n注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。"

            for net_attempt in range(3):
                try:
                    resp = client.post(url, headers=headers, json=req)
                    try:
                        resp.raise_for_status()
                    except httpx.HTTPStatusError as e:
                        status = resp.status_code
                        # Retry transient failures.
                        if status in {429, 500, 502, 503, 504} and net_attempt < 2:
                            retry_after = (resp.headers.get("retry-after") or "").strip()
                            if retry_after:
                                try:
                                    time.sleep(min(30.0, float(retry_after)))
                                except Exception:
                                    _sleep_backoff(net_attempt)
                            else:
                                _sleep_backoff(net_attempt)
                            continue

                        ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                        snippet = _resp_snippet(resp)
                        raise RuntimeError(
                            f"OpenAI request failed (status={resp.status_code}, content-type={ct}, url={url}). {snippet}"
                        ) from e

                    try:
                        resp_json = resp.json()
                    except Exception as e:
                        ct = (resp.headers.get("content-type") or "").split(";")[0].strip()
                        hint = " (check openai_base_url; most providers require it to end with /v1)" if "text/html" in ct else ""
                        raise RuntimeError(
                            f"OpenAI endpoint did not return JSON (status={resp.status_code}, content-type={ct}, url={url}){hint}."
                        ) from e

                    content = _openai_extract_content(resp_json)
                    data = json.loads(_strip_code_fence(content))
                    if not isinstance(data, dict):
                        raise RuntimeError("OpenAI output is not a JSON object")

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
                except httpx.TimeoutException as e:
                    last_err = e
                    if net_attempt < 2:
                        _sleep_backoff(net_attempt)
                        continue
                    break
                except httpx.TransportError as e:
                    last_err = e
                    if net_attempt < 2:
                        _sleep_backoff(net_attempt)
                        continue
                    break
                except Exception as e:
                    last_err = e
                    break

        if last_err:
            raise last_err
        raise RuntimeError("OpenAI translate failed")

    summary = ""
    out: list[Segment] = []
    cur_batch_size = batch_size
    idx = 0
    with _client() as client:
        while idx < len(segs):
            size = min(cur_batch_size, len(segs) - idx)
            batch = segs[idx : idx + size]
            try:
                translated, summary = _translate_batch(client, batch, start_idx=idx, summary=summary)
                out.extend(translated)
                idx += size
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
    )
    return out


def generate_bilibili_tags_openai(
    *,
    title: str,
    summary: str,
    transcript: str,
    api_key: str | None,
    base_url: str,
    model: str,
    temperature: float = 0.2,
    timeout_seconds: float = 60.0,
    n_tags: int = 6,
) -> list[str]:
    if not api_key:
        raise RuntimeError("OpenAI API key is not set")

    url = build_openai_chat_completions_url(base_url)
    headers = {"Authorization": f"Bearer {api_key}"}

    def _sleep_backoff(attempt: int) -> None:
        base = min(8.0, float(2**attempt))
        time.sleep(base + random.random() * 0.25)

    system_prompt = "You are a professional video SEO assistant. Return ONLY valid JSON."

    t = float(timeout_seconds)
    timeout = httpx.Timeout(t, connect=min(10.0, t), read=t, write=t, pool=t)

    title = (title or "").strip()
    summary = (summary or "").strip()
    transcript = (transcript or "").strip()
    n = max(1, int(n_tags))

    user_prompt = (
        "请为 Bilibili 投稿生成视频标签（tags）。\n"
        f"- 只生成 {n} 个标签（不要多也不要少）\n"
        "- 不要包含 'videoroll'\n"
        "- 标签语言优先中文，必要时可保留常用英文缩写\n"
        "- 每个标签尽量短（建议 2~12 字），不要带 # 号，不要带空格或标点\n"
        "- 标签尽量覆盖：主题/领域/核心对象/关键技术/结果或亮点\n\n"
        f"标题：{title}\n\n"
        f"摘要（如有）：{summary}\n\n"
        f"字幕全文片段（可能截断）：\n{transcript}\n\n"
        '输出 JSON（不要 Markdown / 不要解释）：{"tags":["tag1","tag2",...]}'
    )

    req: dict[str, Any] = {
        "model": model,
        "temperature": float(temperature),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }

    def _clean_tags(tags: list[Any]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in tags:
            s = str(item or "").strip().lstrip("#").lstrip("＃")
            s = "".join(s.split())
            if not s:
                continue
            if s.lower() == "videoroll":
                continue
            if len(s) > 20:
                s = s[:20]
            k = s.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(s)
        return out

    last_err: Exception | None = None
    with httpx.Client(timeout=timeout) as client:
        for format_attempt in range(2):
            if format_attempt > 0:
                req["messages"][-1]["content"] = user_prompt + "\n\n注意：上一次输出不符合 JSON/结构要求，请严格按 JSON 输出。"
            for net_attempt in range(3):
                try:
                    resp = client.post(url, headers=headers, json=req)
                    resp.raise_for_status()

                    resp_json = resp.json()
                    content = _openai_extract_content(resp_json)
                    data = json.loads(_strip_code_fence(content))
                    if not isinstance(data, dict):
                        raise RuntimeError("OpenAI output is not a JSON object")
                    tags = data.get("tags")
                    if not isinstance(tags, list):
                        raise RuntimeError("OpenAI output missing 'tags' list")

                    out = _clean_tags(tags)
                    if len(out) < n:
                        raise RuntimeError(f"OpenAI output has too few tags (want={n}, got={len(out)})")
                    return out[:n]
                except httpx.TimeoutException as e:
                    last_err = e
                    if net_attempt < 2:
                        _sleep_backoff(net_attempt)
                        continue
                    break
                except httpx.TransportError as e:
                    last_err = e
                    if net_attempt < 2:
                        _sleep_backoff(net_attempt)
                        continue
                    break
                except Exception as e:
                    last_err = e
                    break

    if last_err:
        raise last_err
    raise RuntimeError("OpenAI tag generation failed")


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
        lines.append(seg.text.strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def _ass_ts(seconds: float) -> str:
    cs = max(0, int(round(seconds * 100)))
    h, rem = divmod(cs, 360_000)
    m, rem = divmod(rem, 6_000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def segments_to_ass(segments: Iterable[Segment], style_name: str = "clean_white") -> str:
    if style_name not in {"clean_white"}:
        style_name = "clean_white"

    # Use a CJK-capable font by default so burn-in works in minimal containers.
    style_default = (
        "Style: Default,Noto Sans CJK SC,67,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "0,0,0,0,100,100,0,0,1,2,0,2,50,50,50,1"
    )

    events: list[str] = []
    for seg in segments:
        text = seg.text.replace("\n", "\\N").replace("\r", "").strip()
        events.append(f"Dialogue: 0,{_ass_ts(seg.start)},{_ass_ts(seg.end)},Default,,0,0,0,,{text}")

    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            "PlayResX: 1920",
            "PlayResY: 1080",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            style_default,
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
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    codec = str(video_codec or "").strip().lower() or "av1"

    if codec in {"h264", "avc"}:
        video_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
    else:
        # AV1 default: SVT-AV1, balanced preset and constant quality.
        # Note: preset range is 0..13 (lower = slower/better).
        video_args = ["-c:v", "libsvtav1", "-preset", "4", "-crf", "24"]

    cmd = [
        ffmpeg_path,
        "-y",
        "-i",
        str(video_path),
        "-vf",
        f"ass={ass_path}",
        *video_args,
        "-c:a",
        "copy",
        str(output_path),
    ]
    _run(cmd)


def mux_soft_sub(ffmpeg_path: str, video_path: Path, srt_path: Path, output_path: Path) -> None:
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
    _run(cmd)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
