from __future__ import annotations

import json
import re
import uuid
from typing import Callable

from sqlalchemy.orm import Session

from videoroll.ai.service import AIService
from videoroll.apps.publish_meta_draft import build_task_publish_meta_draft
from videoroll.apps.subtitle_service.bilibili_tags_store import set_task_bilibili_tags
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.task_title_store import set_task_titles
from videoroll.db.models import Asset, AssetKind, Task
from videoroll.storage.filesystem import FileStore


LogLine = Callable[[str], None]
_CJK_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
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


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _read_storage_bytes(store: FileStore, key: str) -> bytes:
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


def latest_youtube_title(db: Session, store: FileStore, task_id: uuid.UUID) -> str:
    asset = (
        db.query(Asset)
        .filter(Asset.task_id == task_id, Asset.kind == AssetKind.metadata_json)
        .order_by(Asset.created_at.desc())
        .first()
    )
    if not asset:
        return ""
    try:
        raw = _read_storage_bytes(store, asset.storage_key)
        info = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        return ""
    if not isinstance(info, dict):
        return ""
    title = info.get("title") or info.get("fulltitle") or info.get("alt_title") or ""
    return str(title or "").strip()


def segments_text_excerpt(segments: list[Segment], max_chars: int = 7000) -> str:
    text = "\n".join((segment.text or "").strip() for segment in segments if (segment.text or "").strip()).strip()
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    half = max(1, max_chars // 2)
    return (text[:half].rstrip() + "\n…\n" + text[-half:].lstrip()).strip()


def fallback_bilibili_tags(*, title: str, summary: str, transcript: str, n: int = 6) -> list[str]:
    blob = "\n".join([title or "", summary or "", transcript or ""]).strip()
    out: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        clean = (tag or "").strip().lstrip("#").lstrip("＃")
        clean = "".join(clean.split())
        if not clean:
            return
        if clean.lower() == "videoroll":
            return
        if len(clean) > 20:
            clean = clean[:20]
        key = clean.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(clean)

    for match in re.finditer(r"[一-鿿]{2,12}", blob):
        add(match.group(0))
        if len(out) >= n:
            return out[:n]

    for match in re.finditer(r"[A-Za-z][A-Za-z0-9+-]{2,15}", blob):
        word = match.group(0)
        if word.lower() in _EN_STOP:
            continue
        add(word)
        if len(out) >= n:
            return out[:n]

    for tag in ["熟肉", "字幕", "翻译", "科技", "教程", "YouTube", "搬运", "科普"]:
        add(tag)
        if len(out) >= n:
            return out[:n]
    return out[:n]


def merge_bilingual_segments(
    source_segments: list[Segment],
    translated_segments: list[Segment],
) -> list[Segment]:
    return [
        Segment(
            start=translated.start,
            end=translated.end,
            text=translated.text,
            confidence=translated.confidence,
            secondary_text=str(source.text or "").strip() or None,
        )
        for source, translated in zip(source_segments, translated_segments)
    ]


def _rollback_best_effort(db: Session, *, operation: str, error: Exception, log: LogLine) -> None:
    try:
        db.rollback()
    except Exception as rollback_error:
        log(
            f"{operation} failed: {type(error).__name__}: {error}; "
            f"rollback also failed: {type(rollback_error).__name__}: {rollback_error}"
        )
        return
    log(f"{operation} skipped: {type(error).__name__}: {error}")


def postprocess_translation(
    *,
    db: Session,
    store: FileStore,
    task: Task,
    request_json: dict[str, object],
    source_segments: list[Segment],
    translated_segments: list[Segment],
    provider: str,
    target_lang: str,
    style: str,
    summary: str,
    bilingual: bool,
    ai_service: AIService,
    log: LogLine,
) -> list[Segment]:
    # Best-effort title enrichment for UI/downloads.
    try:
        source_title = latest_youtube_title(db, store, task.id)
        if source_title:
            translated_title = source_title
            if provider == "openai" and not has_cjk(source_title):
                try:
                    translated_title = ai_service.translate_title(
                        source_title,
                        target_lang=target_lang,
                        style=style,
                        summary=summary,
                        retry_count=4,
                    )
                except Exception:
                    translated_title = source_title
            set_task_titles(
                db,
                str(task.id),
                source_title=source_title,
                translated_title=translated_title,
            )
    except Exception as error:
        _rollback_best_effort(
            db,
            operation="title metadata update",
            error=error,
            log=log,
        )

    # Best-effort Bilibili tags.
    try:
        title_hint = latest_youtube_title(db, store, task.id)
        transcript_excerpt = segments_text_excerpt(translated_segments, max_chars=7000)
        tags: list[str] = []
        if provider == "openai":
            try:
                tags = ai_service.generate_bilibili_tags(
                    title=title_hint,
                    summary=summary,
                    transcript=transcript_excerpt,
                    n_tags=6,
                )
            except Exception:
                tags = []
        if len(tags) < 6:
            tags = fallback_bilibili_tags(
                title=title_hint,
                summary=summary,
                transcript=transcript_excerpt,
                n=6,
            )
        if tags:
            set_task_bilibili_tags(
                db,
                str(task.id),
                tags=tags[:6],
                title=title_hint,
                summary=summary,
            )
    except Exception as error:
        _rollback_best_effort(
            db,
            operation="bilibili tag update",
            error=error,
            log=log,
        )

    # Refresh publish metadata after summary-aware title translation.
    try:
        after_render = request_json.get("after_render")
        after_render_cfg = after_render if isinstance(after_render, dict) else {}
        if after_render_cfg.get("publish"):
            final_publish_meta = build_task_publish_meta_draft(task, db=db, store=store, mode="source")
            store.put_bytes(
                json.dumps(final_publish_meta, ensure_ascii=False, indent=2).encode("utf-8"),
                f"meta/{task.id}/publish_meta.json",
                content_type="application/json",
            )
            log("publish meta refreshed after summary-aware title translation")
    except Exception as error:
        _rollback_best_effort(
            db,
            operation="publish meta refresh",
            error=error,
            log=log,
        )

    if bilingual:
        return merge_bilingual_segments(source_segments, translated_segments)
    return translated_segments
