from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from celery import Celery
from sqlalchemy.orm import Session

from videoroll.config import get_subtitle_settings
from videoroll.db.base import Base
from videoroll.db.models import (
    Asset,
    AssetKind,
    Subtitle,
    SubtitleFormat,
    SubtitleJob,
    SubtitleJobStatus,
    Task,
    TaskStatus,
)
from videoroll.db.session import get_engine, get_sessionmaker
from videoroll.storage.s3 import S3Store
from videoroll.utils.hashing import sha256_file
from videoroll.apps.subtitle_service.processing import (
    Segment,
    extract_audio,
    generate_bilibili_tags_openai,
    mux_soft_sub,
    render_burn_in,
    segments_to_ass,
    segments_to_srt,
    transcribe_faster_whisper,
    transcribe_mock,
    translate_segments_openai,
    translate_segments_openai_with_summary,
    translate_segments_mock,
    write_json,
)
from videoroll.apps.subtitle_service.asr_settings_store import get_asr_settings
from videoroll.apps.subtitle_service.auto_profile_store import get_auto_profile
from videoroll.apps.subtitle_service.bilibili_tags_store import set_task_bilibili_tags
from videoroll.apps.subtitle_service.translate_settings_store import get_translate_settings
from videoroll.apps.bilibili_publisher.publish_settings_store import get_bilibili_publish_settings
from videoroll.utils.hf_hub import configure_hf_hub_proxy


settings = get_subtitle_settings()
celery_app = Celery("subtitle_service", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

_SIZE_TO_REPO: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
}


def _resolve_faster_whisper_model(model_name: str, model_dir: Path, *, proxy: str | None = None) -> str:
    model_name = (model_name or "").strip()
    if not model_name:
        return model_name

    # If user provided a local path, use it directly.
    p = Path(model_name)
    if p.exists():
        return str(p)

    # Prefer our persisted models dir so downloads survive container rebuilds.
    repo_id = _SIZE_TO_REPO.get(model_name, model_name)
    local_name = model_name if model_name in _SIZE_TO_REPO else model_name.replace("/", "--")
    dest = model_dir / local_name
    if dest.exists():
        return str(dest)

    try:
        from huggingface_hub import snapshot_download  # type: ignore
    except Exception:
        # Fall back to faster-whisper's own downloader/cache.
        return repo_id

    configure_hf_hub_proxy(proxy)

    model_dir.mkdir(parents=True, exist_ok=True)

    tmp = dest.with_name(dest.name + ".downloading")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        try:
            snapshot_download(repo_id=repo_id, local_dir=str(tmp))
        except TypeError:
            snapshot_download(repo_id=repo_id, local_dir=str(tmp))
        shutil.rmtree(dest, ignore_errors=True)
        tmp.replace(dest)
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(
            f"failed to download whisper model '{repo_id}' into '{dest}'. "
            "Open Settings → ASR/Whisper to download it first, or set asr.model to an existing path. "
            f"detail={type(e).__name__}: {e}"
        ) from e

    return str(dest)


def _db() -> Session:
    SessionLocal = get_sessionmaker(settings.database_url)
    return SessionLocal()


def _ensure_db() -> None:
    engine = get_engine(settings.database_url)
    Base.metadata.create_all(engine)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uF900-\uFAFF]")


def _has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _clamp_text(text: str, max_len: int) -> str:
    s = (text or "").strip()
    if len(s) <= max_len:
        return s
    if max_len <= 1:
        return s[:max_len]
    return s[: max_len - 1] + "…"


def _build_bilibili_desc(youtube_desc: str, source_url: str) -> str:
    src = (source_url or "").strip()
    tail = f"\n\n原视频：{src}" if src else ""
    max_len = 2000
    if not tail:
        return _clamp_text((youtube_desc or "").strip(), max_len)
    if len(tail) >= max_len:
        return _clamp_text(tail, max_len)
    base = (youtube_desc or "").strip()
    avail = max_len - len(tail)
    if len(base) > avail:
        base = _clamp_text(base, avail)
    out = (base + tail).strip() if base else f"原视频：{src}"
    return _clamp_text(out, max_len)


def _translate_title_openai(
    title: str,
    *,
    target_lang: str,
    style: str,
    translate_settings: dict[str, Any],
) -> str:
    if not title.strip():
        return title
    if not translate_settings.get("openai_api_key"):
        return title
    translated = translate_segments_openai(
        [Segment(start=0.0, end=1.0, text=title)],
        target_lang=target_lang,
        style=style,
        api_key=translate_settings.get("openai_api_key"),
        base_url=translate_settings.get("openai_base_url"),
        model=translate_settings.get("openai_model"),
        temperature=float(translate_settings.get("openai_temperature") or 0.2),
        timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 180.0),
        batch_size=1,
        enable_summary=False,
    )
    if not translated:
        return title
    return str(translated[0].text or "").strip() or title


def _read_s3_bytes(store: S3Store, key: str) -> bytes:
    obj = store.get_object(key)
    body = obj.get("Body")
    if not body:
        return b""
    try:
        return body.read() or b""
    finally:
        try:
            body.close()
        except Exception:
            pass


def _latest_youtube_title(db: Session, store: S3Store, task_id: uuid.UUID) -> str:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return ""
    try:
        raw = _read_s3_bytes(store, asset.storage_key)
        info = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return ""
    if not isinstance(info, dict):
        return ""
    title = info.get("title") or info.get("fulltitle") or info.get("alt_title") or ""
    return str(title or "").strip()


def _segments_text_excerpt(segments: list[Segment], max_chars: int = 7000) -> str:
    text = "\n".join((s.text or "").strip() for s in segments if (s.text or "").strip())
    text = text.strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    half = max(1, max_chars // 2)
    return (text[:half].rstrip() + "\n…\n" + text[-half:].lstrip()).strip()


_EN_STOP = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "of",
    "in",
    "on",
    "for",
    "with",
    "this",
    "that",
    "is",
    "are",
    "be",
    "as",
    "it",
    "we",
    "you",
    "i",
}


def _fallback_bilibili_tags(*, title: str, summary: str, transcript: str, n: int = 6) -> list[str]:
    blob = "\n".join([title or "", summary or "", transcript or ""])
    blob = blob.strip()
    out: list[str] = []
    seen: set[str] = set()

    def _add(tag: str) -> None:
        s = (tag or "").strip().lstrip("#").lstrip("＃")
        s = "".join(s.split())
        if not s:
            return
        if s.lower() == "videoroll":
            return
        if len(s) > 20:
            s = s[:20]
        k = s.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(s)

    # Prefer CJK chunks as tags.
    for m in re.finditer(r"[\u4e00-\u9fff]{2,12}", blob):
        _add(m.group(0))
        if len(out) >= n:
            return out[:n]

    # Then English-ish words from title/summary.
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9+-]{2,15}", blob):
        w = m.group(0)
        if w.lower() in _EN_STOP:
            continue
        _add(w)
        if len(out) >= n:
            return out[:n]

    # Generic fallback.
    for t in ["熟肉", "字幕", "翻译", "科技", "教程", "YouTube", "搬运", "科普"]:
        _add(t)
        if len(out) >= n:
            return out[:n]

    return out[:n]


@celery_app.task(name="subtitle_service.process_job")
def process_job(job_id: str) -> dict[str, str]:
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    jid = uuid.UUID(job_id)
    db = _db()
    try:
        job = db.get(SubtitleJob, jid)
        if not job:
            return {"status": "error", "detail": "job not found"}

        task = db.get(Task, job.task_id)
        if not task:
            job.status = SubtitleJobStatus.failed
            job.error_message = "task not found"
            db.add(job)
            db.commit()
            return {"status": "error", "detail": "task not found"}

        job.status = SubtitleJobStatus.running
        job.progress = 1
        db.add(job)
        db.commit()

        req = job.request_json
        input_key = (req.get("input") or {}).get("key")
        if not input_key:
            raise ValueError("missing input.key")

        work_root = Path(settings.work_dir) / "subtitle" / str(job.id)
        work_root.mkdir(parents=True, exist_ok=True)

        video_path = work_root / "input.mp4"
        audio_path = work_root / "audio.wav"
        segments_path = work_root / "segments.json"
        srt_path = work_root / "subtitle_zh.srt"
        ass_path = work_root / "subtitle_zh.ass"

        store.download_file(input_key, video_path)

        extract_audio(settings.ffmpeg_path, video_path, audio_path)
        audio_key = f"work/{task.id}/audio.wav"
        store.upload_file(audio_path, audio_key, content_type="audio/wav")
        db.add(
            Asset(
                task_id=task.id,
                kind=AssetKind.audio_wav,
                storage_key=audio_key,
                sha256=sha256_file(audio_path),
                size_bytes=audio_path.stat().st_size,
            )
        )
        task.status = TaskStatus.audio_extracted
        db.add(task)

        job.progress = 25
        db.add(job)
        db.commit()

        asr_cfg = req.get("asr") or {}
        asr_defaults = get_asr_settings(db, settings)
        requested_engine = (asr_cfg.get("engine") or "auto").strip()
        requested_language = (asr_cfg.get("language") or "auto").strip()
        requested_model = (asr_cfg.get("model") or "").strip() or None

        engine = asr_defaults["default_engine"] if requested_engine in {"", "auto"} else requested_engine
        language = asr_defaults["default_language"] if requested_language in {"", "auto"} else requested_language
        model_name = requested_model or asr_defaults["default_model"]

        if engine == "mock":
            segments = transcribe_mock(audio_path)
        elif engine == "faster-whisper":
            proxy = str(asr_defaults.get("model_download_proxy") or "").strip() or None
            model_name = _resolve_faster_whisper_model(model_name, Path(settings.whisper_model_dir), proxy=proxy)
            segments = transcribe_faster_whisper(
                audio_path,
                model_name=model_name,
                language=language,
                device=settings.whisper_device,
                compute_type=settings.whisper_compute_type,
            )
        else:
            raise ValueError(f"unsupported ASR engine: {engine}")
        segments_json = [seg.__dict__ for seg in segments]
        write_json(segments_path, segments_json)
        segments_key = f"sub/{task.id}/segments.json"
        store.upload_file(segments_path, segments_key, content_type="application/json")
        db.add(Asset(task_id=task.id, kind=AssetKind.segments_json, storage_key=segments_key, size_bytes=segments_path.stat().st_size))

        task.status = TaskStatus.asr_done
        db.add(task)

        job.progress = 60
        db.add(job)
        db.commit()

        translate_cfg = req.get("translate") or {}
        translate_enabled = bool(translate_cfg.get("enabled"))
        target_lang = (translate_cfg.get("target_lang") or "zh").strip() or "zh"
        provider = (translate_cfg.get("provider") or "mock").strip() or "mock"
        bilingual = bool(translate_cfg.get("bilingual"))
        segments_out = segments
        translation_summary = ""
        if translate_enabled:
            translate_settings = get_translate_settings(db, settings)
            style = (translate_cfg.get("style") or translate_settings["default_style"]).strip() or translate_settings["default_style"]
            batch_size = int(translate_cfg.get("batch_size") or translate_settings["default_batch_size"])
            enable_summary_val = translate_cfg.get("enable_summary")
            enable_summary = translate_settings["default_enable_summary"] if enable_summary_val is None else bool(enable_summary_val)
            if provider == "mock":
                segments_out = translate_segments_mock(segments, target_lang=target_lang)
            elif provider in {"noop", "none"}:
                segments_out = segments
            elif provider == "openai":
                segments_out, translation_summary = translate_segments_openai_with_summary(
                    segments,
                    target_lang=target_lang,
                    style=style,
                    api_key=translate_settings["openai_api_key"],
                    base_url=translate_settings["openai_base_url"],
                    model=translate_settings["openai_model"],
                    temperature=translate_settings["openai_temperature"],
                    timeout_seconds=translate_settings["openai_timeout_seconds"],
                    batch_size=batch_size,
                    enable_summary=enable_summary,
                )
            else:
                raise ValueError(f"unsupported translate provider: {provider}")
            task.status = TaskStatus.translated
            db.add(task)

            # Best-effort: generate Bilibili tags from translation summary + transcript excerpt.
            try:
                title_hint = _latest_youtube_title(db, store, task.id)
                transcript_excerpt = _segments_text_excerpt(segments_out, max_chars=7000)
                tags: list[str] = []
                if provider == "openai":
                    try:
                        tags = generate_bilibili_tags_openai(
                            title=title_hint,
                            summary=translation_summary,
                            transcript=transcript_excerpt,
                            api_key=translate_settings.get("openai_api_key"),
                            base_url=translate_settings.get("openai_base_url"),
                            model=translate_settings.get("openai_model"),
                            temperature=float(translate_settings.get("openai_temperature") or 0.2),
                            timeout_seconds=float(translate_settings.get("openai_timeout_seconds") or 180.0),
                            n_tags=6,
                        )
                    except Exception:
                        tags = []
                if len(tags) < 6:
                    tags = _fallback_bilibili_tags(title=title_hint, summary=translation_summary, transcript=transcript_excerpt, n=6)
                if tags:
                    set_task_bilibili_tags(db, str(task.id), tags=tags[:6], title=title_hint, summary=translation_summary)
            except Exception:
                pass

            if bilingual:
                merged: list[Segment] = []
                for src_seg, zh_seg in zip(segments, segments_out):
                    merged.append(
                        Segment(
                            start=zh_seg.start,
                            end=zh_seg.end,
                            text=f"{zh_seg.text}\n{src_seg.text}",
                            confidence=zh_seg.confidence,
                        )
                    )
                segments_out = merged

        srt_text = segments_to_srt(segments_out)
        srt_path.write_text(srt_text, encoding="utf-8")
        srt_key = f"sub/{task.id}/subtitle_zh.srt"
        store.upload_file(srt_path, srt_key, content_type="text/plain")
        db.add(Asset(task_id=task.id, kind=AssetKind.subtitle_srt, storage_key=srt_key, sha256=sha256_file(srt_path), size_bytes=srt_path.stat().st_size))
        db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.srt, language="zh", storage_key=srt_key))

        want_ass = "ass" in (req.get("output") or {}).get("formats", [])
        if want_ass:
            ass_text = segments_to_ass(segments_out, style_name=((req.get("output") or {}).get("render") or {}).get("ass_style", "clean_white"))
            ass_path.write_text(ass_text, encoding="utf-8")
            ass_key = f"sub/{task.id}/subtitle_zh.ass"
            store.upload_file(ass_path, ass_key, content_type="text/plain")
            db.add(Asset(task_id=task.id, kind=AssetKind.subtitle_ass, storage_key=ass_key, sha256=sha256_file(ass_path), size_bytes=ass_path.stat().st_size))
            db.add(Subtitle(task_id=task.id, version=1, format=SubtitleFormat.ass, language="zh", storage_key=ass_key))

        task.status = TaskStatus.subtitle_ready
        db.add(task)
        job.progress = 80
        db.add(job)
        db.commit()

        render_cfg = ((req.get("output") or {}).get("render") or {})
        burn_in = bool(render_cfg.get("burn_in"))
        soft_sub = bool(render_cfg.get("soft_sub"))

        if burn_in:
            if not ass_path.exists():
                ass_text = segments_to_ass(segments_out, style_name=render_cfg.get("ass_style", "clean_white"))
                ass_path.write_text(ass_text, encoding="utf-8")
            out_video = work_root / "video_burnin.mp4"
            render_burn_in(settings.ffmpeg_path, video_path, ass_path, out_video)
            final_key = f"final/{task.id}/video_burnin.mp4"
            store.upload_file(out_video, final_key, content_type="video/mp4")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=sha256_file(out_video),
                    size_bytes=out_video.stat().st_size,
                )
            )

        if soft_sub:
            out_video = work_root / "video_softsub.mkv"
            mux_soft_sub(settings.ffmpeg_path, video_path, srt_path, out_video)
            final_key = f"final/{task.id}/video_softsub.mkv"
            store.upload_file(out_video, final_key, content_type="video/x-matroska")
            db.add(
                Asset(
                    task_id=task.id,
                    kind=AssetKind.video_final,
                    storage_key=final_key,
                    sha256=sha256_file(out_video),
                    size_bytes=out_video.stat().st_size,
                )
            )

        if burn_in or soft_sub:
            task.status = TaskStatus.rendered
            db.add(task)

        job.status = SubtitleJobStatus.succeeded
        job.progress = 100
        db.add(job)
        db.commit()
        return {"status": "ok"}
    except Exception as e:
        job = db.get(SubtitleJob, uuid.UUID(job_id))
        if job:
            job.status = SubtitleJobStatus.failed
            job.error_message = str(e)
            db.add(job)
        db.commit()
        return {"status": "error", "detail": str(e)}
    finally:
        db.close()


@celery_app.task(name="subtitle_service.auto_youtube_pipeline")
def auto_youtube_pipeline(task_id: str) -> dict[str, str]:
    """
    One-click pipeline:
      - download YouTube video (+ metadata + cover)
      - generate translated subtitles
      - burn-in (and/or soft-sub) according to auto profile
      - publish to bilibili (optional, according to auto profile)
    """
    _ensure_db()
    store = S3Store(settings)
    store.ensure_bucket()

    orch_base = "http://localhost:8000"

    db = _db()
    try:
        tid = uuid.UUID(task_id)
        task = db.get(Task, tid)
        if not task:
            raise RuntimeError("task not found")
        if task.source_type.value != "youtube":
            raise RuntimeError("task is not a youtube source")

        profile = get_auto_profile(db)

        # Download YouTube video + cover + metadata (idempotent).
        with httpx.Client(timeout=None) as client:
            resp = client.post(f"{orch_base}/tasks/{tid}/actions/youtube_download")
            resp.raise_for_status()
            yt = resp.json() if resp.content else {}

        yt_meta = yt.get("metadata") if isinstance(yt, dict) else {}
        if not isinstance(yt_meta, dict):
            yt_meta = {}
        yt_title = str(yt_meta.get("title") or "").strip()
        yt_desc = str(yt_meta.get("description") or "")
        webpage_url = str(yt_meta.get("webpage_url") or task.source_url or "").strip()

        video_key = None
        if isinstance(yt, dict):
            va = yt.get("video_asset")
            if isinstance(va, dict):
                video_key = str(va.get("storage_key") or "").strip() or None
        if not video_key:
            latest_video = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_raw)
                .order_by(Asset.created_at.desc())
                .first()
            )
            video_key = latest_video.storage_key if latest_video else None
        if not video_key:
            raise RuntimeError("no raw video asset found after youtube_download")

        cover_key = None
        if isinstance(yt, dict):
            ca = yt.get("cover_asset")
            if isinstance(ca, dict):
                cover_key = str(ca.get("storage_key") or "").strip() or None
        if not cover_key and profile.get("publish_use_youtube_cover"):
            latest_cover = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.cover_image)
                .order_by(Asset.created_at.desc())
                .first()
            )
            cover_key = latest_cover.storage_key if latest_cover else None

        # Render subtitles (skip if a final video already exists).
        final_asset = (
            db.query(Asset)
            .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_final)
            .order_by(Asset.created_at.desc())
            .first()
        )
        if not final_asset and (profile.get("burn_in") or profile.get("soft_sub")):
            req = {
                "task_id": str(tid),
                "input": {"type": "s3", "key": video_key},
                "asr": {
                    "engine": profile.get("asr_engine") or "auto",
                    "language": profile.get("asr_language") or "auto",
                    "model": profile.get("asr_model"),
                },
                "translate": {
                    "enabled": bool(profile.get("translate_enabled")),
                    "target_lang": profile.get("target_lang") or "zh",
                    "provider": profile.get("translate_provider") or "openai",
                    "style": profile.get("translate_style") or "口语自然",
                    "enable_summary": bool(profile.get("translate_enable_summary")),
                    "bilingual": bool(profile.get("bilingual")),
                },
                "output": {
                    "formats": profile.get("formats") or ["srt", "ass"],
                    "render": {
                        "burn_in": bool(profile.get("burn_in")),
                        "soft_sub": bool(profile.get("soft_sub")),
                        "ass_style": profile.get("ass_style") or "clean_white",
                    },
                },
                "output_prefix": f"sub/{tid}/",
            }

            job = SubtitleJob(task_id=tid, request_json=req, status=SubtitleJobStatus.queued, progress=0)
            db.add(job)
            db.commit()
            db.refresh(job)

            result = process_job(str(job.id))
            if result.get("status") != "ok":
                raise RuntimeError(str(result.get("detail") or "subtitle job failed"))

            final_asset = (
                db.query(Asset)
                .filter(Asset.task_id == tid, Asset.kind == AssetKind.video_final)
                .order_by(Asset.created_at.desc())
                .first()
            )

        if profile.get("auto_publish"):
            task = db.get(Task, tid)
            if task and task.status == TaskStatus.published:
                return {"status": "ok", "task_id": str(tid), "detail": "already published"}

            if not final_asset:
                raise RuntimeError("no final video asset found; enable burn_in/soft_sub in auto profile")

            meta_model = get_bilibili_publish_settings(db)["default_meta"]
            meta = meta_model.model_dump() if hasattr(meta_model, "model_dump") else dict(meta_model)

            translate_settings = get_translate_settings(db, settings)
            title_out = yt_title or str(meta.get("title") or "").strip() or "未命名"
            if profile.get("publish_translate_title") and title_out and not _has_cjk(title_out):
                provider = str(profile.get("translate_provider") or translate_settings.get("default_provider") or "").strip() or "openai"
                if provider == "openai":
                    try:
                        title_out = _translate_title_openai(
                            title_out,
                            target_lang=str(profile.get("target_lang") or translate_settings.get("default_target_lang") or "zh"),
                            style=str(profile.get("translate_style") or translate_settings.get("default_style") or "口语自然"),
                            translate_settings=translate_settings,
                        )
                    except Exception:
                        pass

            prefix = str(profile.get("publish_title_prefix") or "").strip()
            if prefix and not title_out.startswith(prefix):
                title_out = prefix + title_out
            meta["title"] = _clamp_text(title_out, 80) or _clamp_text(yt_title, 80) or "未命名"
            meta["desc"] = _build_bilibili_desc(yt_desc, webpage_url)
            meta["copyright"] = 2
            meta["source"] = webpage_url

            publish_payload = {
                "account_id": None,
                "video_key": final_asset.storage_key,
                "cover_key": cover_key,
                "meta": meta,
            }

            with httpx.Client(timeout=30.0) as client:
                resp = client.post(f"{orch_base}/tasks/{tid}/actions/publish", json=publish_payload)
                resp.raise_for_status()

        return {"status": "ok", "task_id": str(tid)}
    except Exception as e:
        task = db.get(Task, uuid.UUID(task_id))
        if task:
            task.status = TaskStatus.failed
            task.error_message = str(e)
            db.add(task)
            db.commit()
        raise
    finally:
        db.close()
