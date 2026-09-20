from __future__ import annotations

import re
import unicodedata
import uuid
from difflib import SequenceMatcher
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.processing import Segment


_TM_NAMESPACE = uuid.UUID("35a14aca-ebce-46a0-96a1-bae6b30d83e1")


def normalize_translation_memory_source(value: str) -> str:
    clean = unicodedata.normalize("NFKC", str(value or "")).casefold()
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:4000]


def _safe_uuid(value: str | None) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    try:
        return str(uuid.UUID(clean))
    except ValueError:
        return None


def remember_translation_pairs(
    db: Session,
    *,
    source_segments: Iterable[Segment],
    translated_segments: Iterable[Segment],
    target_lang: str,
    task_id: str | None,
    subtitle_job_id: str | None,
    domain: str = "",
) -> int:
    source_rows = list(source_segments)
    target_rows = list(translated_segments)
    count = min(len(source_rows), len(target_rows))
    if count <= 0:
        return 0
    clean_task_id = _safe_uuid(task_id)
    clean_job_id = _safe_uuid(subtitle_job_id)
    written = 0
    for source_segment, translated_segment in zip(source_rows[:count], target_rows[:count], strict=True):
        source_text = str(source_segment.text or "").strip()
        target_text = str(translated_segment.text or "").strip()
        source_norm = normalize_translation_memory_source(source_text)
        if not source_norm or not target_text:
            continue
        identity = "|".join(
            [
                "videoroll-translation-memory",
                clean_task_id or "",
                str(target_lang or "zh").strip() or "zh",
                source_norm,
            ]
        )
        memory_id = str(uuid.uuid5(_TM_NAMESPACE, identity))
        db.execute(
            text(
                """
                INSERT INTO translation_memory_entries (
                    id, source_text, source_norm, target_text, target_lang,
                    domain, task_id, subtitle_job_id, source_kind, status,
                    quality_score, usage_count, created_at, updated_at
                )
                VALUES (
                    CAST(:id AS uuid), :source_text, :source_norm, :target_text, :target_lang,
                    :domain, CAST(:task_id AS uuid), CAST(:subtitle_job_id AS uuid), 'machine', 'machine',
                    0.0, 0, now(), now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    target_text = EXCLUDED.target_text,
                    domain = EXCLUDED.domain,
                    subtitle_job_id = EXCLUDED.subtitle_job_id,
                    updated_at = now()
                """
            ),
            {
                "id": memory_id,
                "source_text": source_text[:8000],
                "source_norm": source_norm,
                "target_text": target_text[:8000],
                "target_lang": str(target_lang or "zh").strip()[:16] or "zh",
                "domain": str(domain or "")[:500],
                "task_id": clean_task_id,
                "subtitle_job_id": clean_job_id,
            },
        )
        written += 1
    return written


def recall_translation_examples(
    db: Session,
    *,
    source_segments: list[Segment],
    start_idx: int,
    target_lang: str,
    task_id: str | None,
    domain: str = "",
    per_block: int = 2,
    total_limit: int = 6,
) -> list[dict[str, Any]]:
    if not source_segments:
        return []
    clean_task_id = _safe_uuid(task_id)
    params: dict[str, Any] = {
        "target_lang": str(target_lang or "zh").strip()[:16] or "zh",
        "task_id": clean_task_id,
        "domain": str(domain or "")[:500],
        "candidate_limit": 400,
    }
    # Machine-generated memory is only trusted inside the same task. Cross-task
    # reuse is reserved for entries that a future review flow marks approved.
    rows = db.execute(
        text(
            """
            SELECT id, source_text, source_norm, target_text, domain, task_id, status,
                   CASE WHEN task_id = CAST(:task_id AS uuid) THEN 1 ELSE 0 END AS same_task
            FROM translation_memory_entries
            WHERE target_lang = :target_lang
              AND (
                    (:task_id IS NOT NULL AND task_id = CAST(:task_id AS uuid))
                    OR status = 'approved'
                  )
              AND (
                    (:task_id IS NOT NULL AND task_id = CAST(:task_id AS uuid))
                    OR :domain = ''
                    OR domain = ''
                    OR domain = :domain
                  )
            ORDER BY
                CASE WHEN task_id = CAST(:task_id AS uuid) THEN 1 ELSE 0 END DESC,
                CASE WHEN status = 'approved' THEN 1 ELSE 0 END DESC,
                updated_at DESC
            LIMIT :candidate_limit
            """
        ),
        params,
    ).all()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        mapping = row._mapping if hasattr(row, "_mapping") else row
        candidates.append(
            {
                "source": str(mapping["source_text"] or ""),
                "source_norm": str(mapping["source_norm"] or ""),
                "target": str(mapping["target_text"] or ""),
                "same_task": bool(mapping["same_task"]),
                "status": str(mapping["status"] or ""),
            }
        )

    ranked: list[tuple[float, int, dict[str, Any]]] = []
    for offset, segment in enumerate(source_segments):
        source_norm = normalize_translation_memory_source(str(segment.text or ""))
        if not source_norm:
            continue
        block_idx = int(start_idx) + offset + 1
        local: list[tuple[float, dict[str, Any]]] = []
        for candidate in candidates:
            candidate_norm = candidate["source_norm"]
            if not candidate_norm:
                continue
            ratio = SequenceMatcher(None, source_norm, candidate_norm, autojunk=False).ratio()
            if len(source_norm) < 12:
                threshold = 0.85 if candidate["same_task"] else 0.92
            else:
                threshold = 0.58 if candidate["same_task"] else 0.78
            if ratio < threshold:
                continue
            local.append((ratio, candidate))
        local.sort(key=lambda pair: (-pair[0], 0 if pair[1]["same_task"] else 1, len(pair[1]["source"])))
        for ratio, candidate in local[: max(1, int(per_block))]:
            ranked.append(
                (
                    ratio,
                    block_idx,
                    {
                        "source": candidate["source"][:1200],
                        "target": candidate["target"][:1200],
                        "similarity": round(ratio, 4),
                        "scope": "same_task" if candidate["same_task"] else "approved_global",
                        "applies_to_blocks": [block_idx],
                    },
                )
            )

    ranked.sort(key=lambda item: (-item[0], item[1]))
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for _ratio, block_idx, example in ranked:
        key = (
            normalize_translation_memory_source(example["source"]),
            str(example["target"]),
            block_idx,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(example)
        if len(out) >= max(1, int(total_limit)):
            break
    return out
