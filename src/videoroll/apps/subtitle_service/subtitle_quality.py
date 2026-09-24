from __future__ import annotations

import re
from typing import Any, Iterable

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.subtitle_readability import (
    derive_readability_profile,
    display_width_units,
    subtitle_cps,
)
from videoroll.apps.subtitle_service.translation_context import (
    derive_translation_scene_profile,
    sanitize_translation_context_memory,
)


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, float(quantile))) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _contains_term(text: str, term: str) -> bool:
    haystack = _norm(text)
    needle = _norm(term)
    if not haystack or not needle:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9_.+\- ]*", needle):
        return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", haystack) is not None
    return needle in haystack


def analyze_subtitle_quality(
    *,
    source_segments: Iterable[Segment],
    translated_segments: Iterable[Segment],
    final_segments: Iterable[Segment],
    target_lang: str,
    context_memory: dict[str, object] | None = None,
) -> dict[str, Any]:
    source = list(source_segments)
    translated = list(translated_segments)
    final = list(final_segments)
    readability_profile = derive_readability_profile(translated or final, target_lang=target_lang)
    scene_profile = derive_translation_scene_profile(source)

    cps_values = [subtitle_cps(item) for item in final]
    durations = [max(0.0, float(item.end) - float(item.start)) for item in final]
    gaps = [
        float(current.start) - float(previous.end)
        for previous, current in zip(final, final[1:])
    ]
    target_cps = float(readability_profile["target_cps"])
    max_line_units = float(readability_profile["max_line_units"])

    issues: list[dict[str, Any]] = []
    for index, item in enumerate(final, start=1):
        duration = max(0.0, float(item.end) - float(item.start))
        cps = subtitle_cps(item)
        lines = str(item.text or "").splitlines() or [""]
        widest = max((display_width_units(line) for line in lines), default=0.0)
        if cps > target_cps * 1.15:
            issues.append({"idx": index, "type": "high_cps", "severity": "warning", "value": round(cps, 2)})
        if duration < 0.65:
            issues.append({"idx": index, "type": "short_display", "severity": "warning", "value": round(duration, 3)})
        if len(lines) > 2:
            issues.append({"idx": index, "type": "too_many_lines", "severity": "warning", "value": len(lines)})
        if widest > max_line_units * 1.15:
            issues.append({"idx": index, "type": "line_too_wide", "severity": "warning", "value": round(widest, 2)})

    overlap_count = 0
    tight_gap_count = 0
    for index, gap in enumerate(gaps, start=2):
        if gap < -0.01:
            overlap_count += 1
            issues.append({"idx": index, "type": "timeline_overlap", "severity": "error", "value": round(gap, 3)})
        elif 0 <= gap < 0.05:
            tight_gap_count += 1

    memory = sanitize_translation_context_memory(context_memory)
    terminology_missing = 0
    if len(source) == len(translated):
        for idx, (source_item, target_item) in enumerate(zip(source, translated), start=1):
            for term in memory.get("terminology") or []:
                if not isinstance(term, dict):
                    continue
                source_term = str(term.get("source") or "")
                target_term = str(term.get("target") or "")
                if (
                    source_term
                    and target_term
                    and _contains_term(source_item.text, source_term)
                    and not _contains_term(target_item.text, target_term)
                ):
                    terminology_missing += 1
                    issues.append(
                        {
                            "idx": idx,
                            "type": "terminology_inconsistent",
                            "severity": "warning",
                            "source": source_term,
                            "expected": target_term,
                        }
                    )

    identical_count = 0
    if len(source) == len(translated):
        identical_count = sum(
            1
            for source_item, target_item in zip(source, translated)
            if _norm(source_item.text) and _norm(source_item.text) == _norm(target_item.text)
        )

    warning_count = sum(1 for item in issues if item.get("severity") == "warning")
    error_count = sum(1 for item in issues if item.get("severity") == "error")
    score = max(0, 100 - error_count * 12 - warning_count * 2)
    return {
        "version": 1,
        "score": score,
        "segment_count": len(final),
        "source_segment_count": len(source),
        "translation_segment_count": len(translated),
        "metrics": {
            "cps_p50": round(_percentile(cps_values, 0.50), 2),
            "cps_p95": round(_percentile(cps_values, 0.95), 2),
            "cps_max": round(max(cps_values, default=0.0), 2),
            "duration_p50": round(_percentile(durations, 0.50), 3),
            "short_display_count": sum(1 for value in durations if value < 0.65),
            "high_cps_count": sum(1 for value in cps_values if value > target_cps * 1.15),
            "overlap_count": overlap_count,
            "tight_gap_count": tight_gap_count,
            "identical_source_target_count": identical_count,
            "terminology_inconsistent_count": terminology_missing,
        },
        "adaptive_profile": {
            "readability": readability_profile,
            "scene": scene_profile,
        },
        "context": {
            "characters": len(memory.get("characters") or []),
            "terminology": len(memory.get("terminology") or []),
            "ambiguities": len(memory.get("ambiguities") or []),
        },
        "issues": issues[:200],
    }
