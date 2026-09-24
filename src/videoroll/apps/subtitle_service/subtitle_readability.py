from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

from videoroll.apps.subtitle_service.processing import Segment


_STRONG_PUNCTUATION = frozenset(".!?。！？")
_WEAK_PUNCTUATION = frozenset(",，、;；:：")
_DEFAULT_MAX_LINE_UNITS = 24.0
_DEFAULT_TARGET_CPS = 20.0
_DEFAULT_MIN_DISPLAY_SECONDS = 0.9
_DEFAULT_MAX_GAP_EXTENSION_SECONDS = 0.75
_MIN_SPLIT_DISPLAY_SECONDS = 0.55


@dataclass(frozen=True, slots=True)
class ReadabilityStats:
    input_segments: int
    output_segments: int
    wrapped_segments: int
    split_segments: int
    extended_segments: int
    max_cps_before: float
    max_cps_after: float


@dataclass(frozen=True, slots=True)
class ReadabilityResult:
    segments: list[Segment]
    stats: ReadabilityStats


def _normalize_display_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\r", " ").replace("\n", " ")).strip()


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
    return sum(_char_width_units(ch) for ch in str(text or ""))


def display_width_units(text: str) -> float:
    return _text_width_units(text)


def _reading_units(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _segment_cps(segment: Segment) -> float:
    duration = max(0.25, float(segment.end) - float(segment.start))
    return _reading_units(segment.text) / duration


def subtitle_cps(segment: Segment) -> float:
    return _segment_cps(segment)


def derive_readability_profile(
    segments: Iterable[Segment],
    *,
    target_lang: str,
) -> dict[str, float]:
    items = list(segments)
    language = str(target_lang or "").strip().lower().replace("_", "-")
    if language.startswith(("zh", "ja", "ko")):
        base_cps = 18.0
        max_line_units = 23.0
    else:
        base_cps = 17.0
        max_line_units = 28.0

    cps_values = sorted(_segment_cps(item) for item in items if float(item.end) > float(item.start))
    median_cps = cps_values[len(cps_values) // 2] if cps_values else base_cps
    if median_cps > base_cps * 1.35:
        target_cps = min(20.0, base_cps + 2.0)
    elif median_cps < base_cps * 0.65:
        target_cps = max(14.0, base_cps - 1.5)
    else:
        target_cps = base_cps

    short_ratio = (
        sum(1 for item in items if float(item.end) - float(item.start) < 0.8) / len(items)
        if items
        else 0.0
    )
    min_display_seconds = 1.0 if short_ratio > 0.20 else 0.9
    return {
        "target_cps": round(target_cps, 2),
        "max_line_units": round(max_line_units, 2),
        "min_display_seconds": round(min_display_seconds, 2),
        "max_gap_extension_seconds": 0.75,
    }


def _candidate_break_positions(text: str) -> list[tuple[int, float]]:
    candidates: list[tuple[int, float]] = []
    for index, ch in enumerate(text[:-1], start=1):
        score = 0.0
        if ch in _STRONG_PUNCTUATION:
            score = 100.0
        elif ch in _WEAK_PUNCTUATION:
            score = 55.0
        elif ch.isspace():
            score = 22.0
        if score:
            candidates.append((index, score))
    return candidates


def _hard_break_position(text: str, max_units: float) -> int:
    used = 0.0
    for index, ch in enumerate(text, start=1):
        next_used = used + _char_width_units(ch)
        if index > 1 and next_used > max_units:
            return index - 1
        used = next_used
    return len(text)


def _best_break_position(text: str, max_units: float) -> int:
    if _text_width_units(text) <= max_units:
        return len(text)

    target = max_units * 0.9
    best: tuple[float, int] | None = None
    for position, punctuation_score in _candidate_break_positions(text):
        left = text[:position].rstrip()
        if not left:
            continue
        width = _text_width_units(left)
        if width > max_units * 1.08:
            continue
        closeness = max(0.0, 30.0 - abs(target - width) * 2.0)
        score = punctuation_score + closeness
        candidate = (score, position)
        if best is None or candidate > best:
            best = candidate
    if best is not None:
        return best[1]
    return max(1, _hard_break_position(text, max_units))


def _split_text_for_events(text: str, max_event_units: float) -> list[str]:
    normalized = _normalize_display_text(text)
    if not normalized:
        return []
    if _text_width_units(normalized) <= max_event_units:
        return [normalized]

    out: list[str] = []
    remaining = normalized
    while remaining and _text_width_units(remaining) > max_event_units:
        position = _best_break_position(remaining, max_event_units)
        if position <= 0 or position >= len(remaining):
            position = max(1, _hard_break_position(remaining, max_event_units))
        left = remaining[:position].strip()
        right = remaining[position:].strip()
        if not left or not right:
            break
        out.append(left)
        remaining = right
    if remaining:
        out.append(remaining)
    return out or [normalized]


def _balanced_two_line_wrap(text: str, max_line_units: float) -> str:
    normalized = _normalize_display_text(text)
    if not normalized or _text_width_units(normalized) <= max_line_units:
        return normalized

    total_width = _text_width_units(normalized)
    best: tuple[float, int] | None = None
    for position, punctuation_score in _candidate_break_positions(normalized):
        left = normalized[:position].strip()
        right = normalized[position:].strip()
        if not left or not right:
            continue
        left_width = _text_width_units(left)
        right_width = _text_width_units(right)
        if max(left_width, right_width) > max_line_units * 1.15:
            continue
        balance_penalty = abs(left_width - right_width)
        score = punctuation_score - balance_penalty * 2.2
        candidate = (score, position)
        if best is None or candidate > best:
            best = candidate

    if best is None:
        target = total_width / 2.0
        used = 0.0
        position = 1
        for index, ch in enumerate(normalized, start=1):
            used += _char_width_units(ch)
            position = index
            if used >= target:
                break
        # Prefer a nearby whitespace/punctuation boundary when possible.
        nearby = [
            candidate_position
            for candidate_position, _score in _candidate_break_positions(normalized)
            if abs(candidate_position - position) <= max(3, len(normalized) // 8)
        ]
        if nearby:
            position = min(nearby, key=lambda item: abs(item - position))
    else:
        position = best[1]

    left = normalized[:position].strip()
    right = normalized[position:].strip()
    if not left or not right:
        return normalized
    return f"{left}\n{right}"


def _extend_short_events(
    segments: list[Segment],
    *,
    target_cps: float,
    min_display_seconds: float,
    max_extension_seconds: float,
) -> tuple[list[Segment], int]:
    if not segments:
        return [], 0
    out: list[Segment] = []
    changed = 0
    for index, segment in enumerate(segments):
        start = max(0.0, float(segment.start))
        end = max(start, float(segment.end))
        duration = max(0.0, end - start)
        desired = max(
            float(min_display_seconds),
            _reading_units(segment.text) / max(1.0, float(target_cps)),
        )
        if desired > duration and index + 1 < len(segments):
            next_start = max(end, float(segments[index + 1].start))
            available_end = max(end, next_start - 0.05)
            new_end = min(
                available_end,
                end + max(0.0, float(max_extension_seconds)),
                start + desired,
            )
            if new_end > end + 0.01:
                end = new_end
                changed += 1
        out.append(
            Segment(
                start=start,
                end=end,
                text=segment.text,
                confidence=segment.confidence,
                secondary_text=segment.secondary_text,
            )
        )
    return out, changed


def _split_segment_for_readability(
    segment: Segment,
    *,
    max_line_units: float,
) -> list[Segment]:
    text = _normalize_display_text(segment.text)
    if not text:
        return []
    chunks = _split_text_for_events(text, max_line_units * 2.0)
    duration = max(0.0, float(segment.end) - float(segment.start))
    if len(chunks) <= 1 or duration / len(chunks) < _MIN_SPLIT_DISPLAY_SECONDS:
        return [
            Segment(
                start=segment.start,
                end=segment.end,
                text=_balanced_two_line_wrap(text, max_line_units),
                confidence=segment.confidence,
                secondary_text=segment.secondary_text,
            )
        ]

    weights = [max(1.0, _text_width_units(chunk)) for chunk in chunks]
    total_weight = sum(weights)
    cursor = float(segment.start)
    out: list[Segment] = []
    elapsed_weight = 0.0
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        elapsed_weight += weight
        end = (
            float(segment.end)
            if index == len(chunks) - 1
            else float(segment.start) + duration * (elapsed_weight / total_weight)
        )
        out.append(
            Segment(
                start=cursor,
                end=max(cursor, end),
                text=_balanced_two_line_wrap(chunk, max_line_units),
                confidence=segment.confidence,
            )
        )
        cursor = end
    return out


def optimize_subtitle_readability(
    segments: Iterable[Segment],
    *,
    bilingual: bool = False,
    max_line_units: float = _DEFAULT_MAX_LINE_UNITS,
    target_cps: float = _DEFAULT_TARGET_CPS,
    min_display_seconds: float = _DEFAULT_MIN_DISPLAY_SECONDS,
    max_gap_extension_seconds: float = _DEFAULT_MAX_GAP_EXTENSION_SECONDS,
) -> ReadabilityResult:
    source = [
        Segment(
            start=max(0.0, float(item.start)),
            end=max(max(0.0, float(item.start)), float(item.end)),
            text=_normalize_display_text(item.text),
            confidence=item.confidence,
            secondary_text=_normalize_display_text(item.secondary_text or "") or None,
        )
        for item in sorted(segments, key=lambda item: (float(item.start), float(item.end)))
        if _normalize_display_text(item.text)
    ]
    if not source:
        stats = ReadabilityStats(0, 0, 0, 0, 0, 0.0, 0.0)
        return ReadabilityResult([], stats)

    max_cps_before = max((_segment_cps(item) for item in source), default=0.0)
    extended, extended_count = _extend_short_events(
        source,
        target_cps=target_cps,
        min_display_seconds=min_display_seconds,
        max_extension_seconds=max_gap_extension_seconds,
    )

    out: list[Segment] = []
    wrapped_count = 0
    split_count = 0
    secondary_line_units = max_line_units / 0.68

    for segment in extended:
        if bilingual:
            primary = _balanced_two_line_wrap(segment.text, max_line_units)
            secondary = (
                _balanced_two_line_wrap(segment.secondary_text or "", secondary_line_units)
                if segment.secondary_text
                else None
            )
            if primary != segment.text or secondary != segment.secondary_text:
                wrapped_count += 1
            out.append(
                Segment(
                    start=segment.start,
                    end=segment.end,
                    text=primary,
                    confidence=segment.confidence,
                    secondary_text=secondary,
                )
            )
            continue

        split = _split_segment_for_readability(segment, max_line_units=max_line_units)
        if len(split) > 1:
            split_count += 1
        if any(item.text != segment.text for item in split):
            wrapped_count += 1
        out.extend(split)

    max_cps_after = max((_segment_cps(item) for item in out), default=0.0)
    stats = ReadabilityStats(
        input_segments=len(source),
        output_segments=len(out),
        wrapped_segments=wrapped_count,
        split_segments=split_count,
        extended_segments=extended_count,
        max_cps_before=round(max_cps_before, 2),
        max_cps_after=round(max_cps_after, 2),
    )
    return ReadabilityResult(out, stats)
