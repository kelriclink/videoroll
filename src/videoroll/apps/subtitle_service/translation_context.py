from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from videoroll.apps.subtitle_service.processing import Segment


_TERMINAL_RE = re.compile(r"[.!?。！？]+[\"'”’）)\]]*$")
_WEAK_RE = re.compile(r"[,，、;；:：]+[\"'”’）)\]]*$")
_CONTEXT_VERSION = 1
_MAX_CHARACTERS = 32
_MAX_TERMS = 64
_MAX_AMBIGUITIES = 32


def _bounded_text(value: object, limit: int) -> str:
    return _clean_text(str(value or ""))[: max(0, int(limit))]


def _normalize_key(value: object) -> str:
    return _bounded_text(value, 240).casefold()


def _sanitize_character(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    name = _bounded_text(value.get("name"), 120)
    target_name = _bounded_text(value.get("target_name"), 120)
    if not name and not target_name:
        return None
    aliases = []
    for alias in value.get("aliases") or []:
        clean = _bounded_text(alias, 120)
        if clean and clean not in aliases:
            aliases.append(clean)
        if len(aliases) >= 8:
            break
    item: dict[str, object] = {
        "name": name or target_name,
        "target_name": target_name,
        "role": _bounded_text(value.get("role"), 240),
        "notes": _bounded_text(value.get("notes"), 500),
    }
    if aliases:
        item["aliases"] = aliases
    return item


def _sanitize_term(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    source = _bounded_text(value.get("source"), 160)
    target = _bounded_text(value.get("target"), 160)
    if not source or not target:
        return None
    return {
        "source": source,
        "target": target,
        "meaning": _bounded_text(value.get("meaning"), 500),
    }


def _sanitize_ambiguity(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    term = _bounded_text(value.get("term"), 160)
    resolution = _bounded_text(value.get("resolution"), 500)
    if not term or not resolution:
        return None
    return {"term": term, "resolution": resolution}


def sanitize_translation_context_memory(value: object) -> dict[str, object]:
    """Normalize persisted/model-produced context into a small stable schema."""
    source = value if isinstance(value, dict) else {}
    memory: dict[str, object] = {
        "version": _CONTEXT_VERSION,
        "topic": _bounded_text(source.get("topic"), 500),
        "style_notes": _bounded_text(source.get("style_notes"), 500),
        "characters": [],
        "terminology": [],
        "ambiguities": [],
    }

    characters: list[dict[str, object]] = []
    for raw in source.get("characters") or []:
        item = _sanitize_character(raw)
        if item is not None:
            characters.append(item)
        if len(characters) >= _MAX_CHARACTERS:
            break
    memory["characters"] = characters

    terms: list[dict[str, str]] = []
    for raw in source.get("terminology") or []:
        item = _sanitize_term(raw)
        if item is not None:
            terms.append(item)
        if len(terms) >= _MAX_TERMS:
            break
    memory["terminology"] = terms

    ambiguities: list[dict[str, str]] = []
    for raw in source.get("ambiguities") or []:
        item = _sanitize_ambiguity(raw)
        if item is not None:
            ambiguities.append(item)
        if len(ambiguities) >= _MAX_AMBIGUITIES:
            break
    memory["ambiguities"] = ambiguities

    recent_scene = source.get("recent_scene")
    if isinstance(recent_scene, dict):
        try:
            scene_id = max(1, int(recent_scene.get("scene_id") or 1))
        except (TypeError, ValueError):
            scene_id = 1
        summary = _bounded_text(recent_scene.get("summary"), 800)
        if summary:
            memory["recent_scene"] = {"scene_id": scene_id, "summary": summary}
    return memory


def _merge_named_records(
    existing: list[dict[str, object]],
    incoming: list[dict[str, object]],
    *,
    key_fields: tuple[str, ...],
    limit: int,
) -> list[dict[str, object]]:
    out = [dict(item) for item in existing]
    positions: dict[str, int] = {}
    for index, item in enumerate(out):
        key = next((_normalize_key(item.get(field)) for field in key_fields if _normalize_key(item.get(field))), "")
        if key:
            positions[key] = index

    for item in incoming:
        key = next((_normalize_key(item.get(field)) for field in key_fields if _normalize_key(item.get(field))), "")
        if not key:
            continue
        if key in positions:
            current = out[positions[key]]
            for field, value in item.items():
                if value not in ("", None, []):
                    current[field] = value
        elif len(out) < limit:
            positions[key] = len(out)
            out.append(dict(item))
    return out[:limit]


def _source_mentions(source_text: str, candidate: str) -> bool:
    source = _clean_text(source_text).casefold()
    needle = _clean_text(candidate).casefold()
    if not source or not needle:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9_.+\- ]*", needle):
        pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
        return re.search(pattern, source) is not None
    return needle in source


def ground_translation_context_update(
    update: object,
    *,
    source_texts: Iterable[str],
) -> dict[str, object]:
    """Drop model-learned entities that are not evidenced by the current source.

    Topic/style notes and scene summaries may describe the batch globally. Named
    entities, terms and ambiguities become durable cross-batch memory only when
    their source-side spelling or alias actually occurs in the source batch.
    """
    if not isinstance(update, dict):
        return {}
    texts = [_clean_text(text) for text in source_texts if _clean_text(text)]
    grounded: dict[str, object] = {
        key: update.get(key)
        for key in ("topic", "style_notes", "scene_summary", "recent_scene")
        if update.get(key) not in (None, "", [], {})
    }

    characters: list[object] = []
    for raw in update.get("characters") or []:
        if not isinstance(raw, dict):
            continue
        names = [raw.get("name"), *(raw.get("aliases") or [])]
        if any(
            _source_mentions(text, str(name or ""))
            for text in texts
            for name in names
            if str(name or "").strip()
        ):
            characters.append(raw)
    if characters:
        grounded["characters"] = characters

    terminology: list[object] = []
    for raw in update.get("terminology") or []:
        if not isinstance(raw, dict):
            continue
        source = str(raw.get("source") or "").strip()
        if source and any(_source_mentions(text, source) for text in texts):
            terminology.append(raw)
    if terminology:
        grounded["terminology"] = terminology

    ambiguities: list[object] = []
    for raw in update.get("ambiguities") or []:
        if not isinstance(raw, dict):
            continue
        term = str(raw.get("term") or "").strip()
        if term and any(_source_mentions(text, term) for text in texts):
            ambiguities.append(raw)
    if ambiguities:
        grounded["ambiguities"] = ambiguities
    return grounded


def merge_translation_context_memory(
    memory: object,
    update: object,
    *,
    scene_id: int | None = None,
) -> dict[str, object]:
    """Merge a model context delta without allowing unbounded memory growth."""
    base = sanitize_translation_context_memory(memory)
    delta = sanitize_translation_context_memory(update)

    if delta.get("topic"):
        base["topic"] = delta["topic"]
    if delta.get("style_notes"):
        base["style_notes"] = delta["style_notes"]

    base["characters"] = _merge_named_records(
        list(base.get("characters") or []),
        list(delta.get("characters") or []),
        key_fields=("name", "target_name"),
        limit=_MAX_CHARACTERS,
    )
    base["terminology"] = _merge_named_records(
        list(base.get("terminology") or []),
        list(delta.get("terminology") or []),
        key_fields=("source",),
        limit=_MAX_TERMS,
    )
    base["ambiguities"] = _merge_named_records(
        list(base.get("ambiguities") or []),
        list(delta.get("ambiguities") or []),
        key_fields=("term",),
        limit=_MAX_AMBIGUITIES,
    )

    recent_scene = delta.get("recent_scene")
    if isinstance(recent_scene, dict) and recent_scene.get("summary"):
        base["recent_scene"] = recent_scene
    elif scene_id is not None:
        raw_summary = ""
        if isinstance(update, dict):
            raw_summary = _bounded_text(update.get("scene_summary"), 800)
        if raw_summary:
            base["recent_scene"] = {
                "scene_id": max(1, int(scene_id)),
                "summary": raw_summary,
            }
    return sanitize_translation_context_memory(base)


def context_memory_is_empty(memory: object) -> bool:
    value = sanitize_translation_context_memory(memory)
    return not any(
        [
            value.get("topic"),
            value.get("style_notes"),
            value.get("characters"),
            value.get("terminology"),
            value.get("ambiguities"),
            value.get("recent_scene"),
        ]
    )


@dataclass(frozen=True, slots=True)
class TranslationScene:
    scene_id: int
    start_index: int
    end_index: int
    start: float
    end: float


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _has_terminal_punctuation(text: str) -> bool:
    return bool(_TERMINAL_RE.search(_clean_text(text)))


def _has_weak_punctuation(text: str) -> bool:
    return bool(_WEAK_RE.search(_clean_text(text)))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, float(quantile))) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def derive_translation_scene_profile(segments: Iterable[Segment]) -> dict[str, float]:
    """Derive pause thresholds from the video's actual ASR cadence."""
    items = list(segments)
    gaps = [
        max(0.0, float(current.start) - float(previous.end))
        for previous, current in zip(items, items[1:])
    ]
    useful = [gap for gap in gaps if 0.08 <= gap <= 8.0]
    if len(useful) < 4:
        return {"soft_gap_seconds": 1.2, "hard_gap_seconds": 2.5}

    p75 = _percentile(useful, 0.75)
    p90 = _percentile(useful, 0.90)
    soft = max(0.7, min(1.6, p75))
    hard = max(soft + 0.75, max(2.0, min(4.0, p90 * 1.35)))
    return {
        "soft_gap_seconds": round(soft, 3),
        "hard_gap_seconds": round(hard, 3),
    }


def build_translation_scenes(
    segments: Iterable[Segment],
    *,
    hard_gap_seconds: float = 2.5,
    soft_gap_seconds: float = 1.2,
    max_scene_segments: int = 24,
    max_scene_seconds: float = 90.0,
) -> list[TranslationScene]:
    """Build deterministic source scenes without changing subtitle indices.

    Hard pauses always open a new scene. Softer pauses only split an already
    meaningful scene when the preceding caption also has a linguistic ending.
    Absolute size/time caps keep very long monologues bounded.
    """
    items = list(segments)
    if not items:
        return []

    hard_gap = max(0.5, float(hard_gap_seconds))
    soft_gap = max(0.2, min(hard_gap, float(soft_gap_seconds)))
    max_segments = max(2, int(max_scene_segments))
    max_seconds = max(5.0, float(max_scene_seconds))

    scenes: list[TranslationScene] = []
    scene_start = 0

    def emit(end_index: int) -> None:
        nonlocal scene_start
        if end_index <= scene_start:
            return
        first = items[scene_start]
        last = items[end_index - 1]
        scenes.append(
            TranslationScene(
                scene_id=len(scenes) + 1,
                start_index=scene_start,
                end_index=end_index,
                start=max(0.0, float(first.start)),
                end=max(float(first.start), float(last.end)),
            )
        )
        scene_start = end_index

    for index in range(1, len(items)):
        previous = items[index - 1]
        current = items[index]
        gap = max(0.0, float(current.start) - float(previous.end))
        scene_count = index - scene_start
        scene_duration = max(0.0, float(previous.end) - float(items[scene_start].start))

        hard_boundary = gap >= hard_gap
        soft_boundary = (
            gap >= soft_gap
            and scene_count >= 4
            and _has_terminal_punctuation(previous.text)
        )
        bounded = scene_count >= max_segments or scene_duration >= max_seconds

        if hard_boundary or soft_boundary or bounded:
            emit(index)

    emit(len(items))
    return scenes


def scene_for_segment_index(
    scenes: Iterable[TranslationScene],
    index: int,
) -> TranslationScene | None:
    for scene in scenes:
        if scene.start_index <= index < scene.end_index:
            return scene
    return None


def _batch_boundary_score(previous: Segment, following: Segment | None) -> float:
    text = _clean_text(previous.text)
    score = 0.0
    if _has_terminal_punctuation(text):
        score += 65.0
    elif _has_weak_punctuation(text):
        score += 28.0

    if following is not None:
        gap = max(0.0, float(following.start) - float(previous.end))
        if gap >= 2.5:
            score += 100.0
        elif gap >= 1.2:
            score += 70.0
        elif gap >= 0.6:
            score += 35.0
    return score


def choose_translation_batch_end(
    segments: list[Segment],
    *,
    start_index: int,
    max_batch_size: int,
    scenes: list[TranslationScene],
) -> int:
    """Choose a natural batch boundary while respecting scene boundaries."""
    if start_index >= len(segments):
        return len(segments)

    max_size = max(1, int(max_batch_size))
    scene = scene_for_segment_index(scenes, start_index)
    scene_end = scene.end_index if scene is not None else len(segments)
    hard_end = min(len(segments), scene_end, start_index + max_size)
    if hard_end <= start_index + 1 or hard_end == scene_end:
        return hard_end

    span = hard_end - start_index
    minimum_size = max(1, int(round(span * 0.6)))
    earliest_end = min(hard_end, start_index + minimum_size)

    best: tuple[float, int] | None = None
    for end_index in range(earliest_end, hard_end + 1):
        previous = segments[end_index - 1]
        following = segments[end_index] if end_index < len(segments) else None
        score = _batch_boundary_score(previous, following)
        # Prefer later boundaries when linguistic/acoustic evidence is equal.
        score += (end_index - start_index) * 0.75
        if best is None or (score, end_index) > best:
            best = (score, end_index)

    if best is not None and best[0] >= 45.0:
        return best[1]
    return hard_end


def build_translation_context_state(
    segments: list[Segment],
    *,
    scenes: list[TranslationScene],
    start_index: int,
    end_index: int,
    running_summary: str = "",
    context_memory: dict[str, object] | None = None,
    scene_profile: dict[str, float] | None = None,
    previous_lines: int = 3,
    next_lines: int = 2,
) -> dict[str, object]:
    """Build a compact, JSON-safe context window for one translation batch."""
    scene = scene_for_segment_index(scenes, start_index)
    prev_start = max(0, start_index - max(0, int(previous_lines)))
    next_end = min(len(segments), end_index + max(0, int(next_lines)))

    previous = [
        {"idx": index + 1, "text": _clean_text(segments[index].text)}
        for index in range(prev_start, start_index)
        if _clean_text(segments[index].text)
    ]
    following = [
        {"idx": index + 1, "text": _clean_text(segments[index].text)}
        for index in range(end_index, next_end)
        if _clean_text(segments[index].text)
    ]

    state: dict[str, object] = {
        "batch": {
            "segment_start": start_index + 1,
            "segment_end": end_index,
        },
        "previous_source": previous,
        "next_source": following,
    }
    summary = str(running_summary or "").strip()
    if summary:
        state["running_summary"] = summary[:1000]
    memory = sanitize_translation_context_memory(context_memory)
    if not context_memory_is_empty(memory):
        state["memory"] = memory
    if scene_profile:
        state["adaptive_scene_profile"] = {
            "soft_gap_seconds": round(float(scene_profile.get("soft_gap_seconds") or 0.0), 3),
            "hard_gap_seconds": round(float(scene_profile.get("hard_gap_seconds") or 0.0), 3),
        }

    if scene is not None:
        state["scene"] = {
            "scene_id": scene.scene_id,
            "segment_start": scene.start_index + 1,
            "segment_end": scene.end_index,
            "time_start": round(scene.start, 3),
            "time_end": round(scene.end, 3),
            "batch_offset": start_index - scene.start_index,
            "batch_count": end_index - start_index,
        }
    return state
