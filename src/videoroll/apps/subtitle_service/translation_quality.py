from __future__ import annotations

import re
import unicodedata
from collections import Counter
from typing import Any, Iterable, Mapping


_NUMBER_RE = re.compile(r"(?<!\d)[-+]?\d+(?:[.,]\d+)*(?!\d)")


def _norm_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _term_applies(source_text: str, term: str, aliases: Iterable[str] = ()) -> bool:
    haystack = _norm_text(source_text)
    if not haystack:
        return False
    for candidate in [term, *aliases]:
        needle = _norm_text(candidate)
        if needle and needle in haystack:
            return True
    return False


def _block_ids_for_term(blocks: list[dict[str, Any]], term: str, aliases: Iterable[str] = ()) -> list[int]:
    out: list[int] = []
    for block in blocks:
        try:
            idx = int(block.get("idx"))
        except (TypeError, ValueError):
            continue
        if _term_applies(str(block.get("text") or ""), term, aliases):
            out.append(idx)
    return out


def build_translation_plan(
    *,
    blocks: list[dict[str, Any]],
    glossary: Mapping[str, str] | None = None,
    rag_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Turn research output into explicit translation-time constraints.

    The plan intentionally keeps machine-researched terms softer than an
    explicit user glossary. This prevents a high-confidence but wrong research
    result from becoming a blind string-replacement rule.
    """

    constraints: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, tuple[int, ...]]] = set()

    def add_constraint(
        *,
        source: str,
        target: str,
        mode: str,
        aliases: Iterable[str] = (),
        meaning: str = "",
        confidence: float | None = None,
        origin: str,
    ) -> None:
        clean_source = str(source or "").strip()
        clean_target = str(target or "").strip()
        alias_list = [str(value or "").strip() for value in aliases if str(value or "").strip()]
        if not clean_source or not clean_target:
            return
        block_ids = _block_ids_for_term(blocks, clean_source, alias_list)
        if not block_ids:
            return
        key = (_norm_text(clean_source), _norm_text(clean_target), mode, tuple(block_ids))
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {
            "source": clean_source[:240],
            "target": clean_target[:240],
            "mode": mode,
            "applies_to_blocks": block_ids,
            "origin": origin,
        }
        if alias_list:
            item["aliases"] = alias_list[:8]
        clean_meaning = str(meaning or "").strip()
        if clean_meaning:
            item["meaning"] = clean_meaning[:1000]
        if confidence is not None:
            try:
                item["confidence"] = round(max(0.0, min(1.0, float(confidence))), 4)
            except (TypeError, ValueError, OverflowError):
                pass
        constraints.append(item)

    for source, target in (glossary or {}).items():
        add_constraint(
            source=str(source),
            target=str(target),
            mode="hard",
            origin="user_glossary",
        )

    context = rag_context if isinstance(rag_context, Mapping) else {}
    for raw_card in context.get("term_cards") or []:
        if not isinstance(raw_card, Mapping):
            continue
        status = str(raw_card.get("status") or "").strip().lower()
        declared_mode = str(raw_card.get("constraint_mode") or "").strip().lower()
        if declared_mode in {"hard", "preferred", "contextual"}:
            mode = declared_mode
        elif status == "context_only":
            mode = "contextual"
        else:
            mode = "preferred"
        add_constraint(
            source=str(raw_card.get("term") or ""),
            target=str(raw_card.get("translation") or ""),
            mode=mode,
            aliases=raw_card.get("aliases") if isinstance(raw_card.get("aliases"), list) else (),
            meaning=str(raw_card.get("description") or ""),
            confidence=raw_card.get("confidence"),
            origin="rag_term",
        )

    examples: list[dict[str, Any]] = []
    for raw_example in context.get("translation_examples") or []:
        if not isinstance(raw_example, Mapping):
            continue
        source = str(raw_example.get("source") or "").strip()
        target = str(raw_example.get("target") or "").strip()
        if not source or not target:
            continue
        applies = raw_example.get("applies_to_blocks")
        block_ids: list[int] = []
        if isinstance(applies, list):
            for value in applies:
                try:
                    block_ids.append(int(value))
                except (TypeError, ValueError):
                    continue
        if not block_ids:
            continue
        try:
            similarity = round(max(0.0, min(1.0, float(raw_example.get("similarity") or 0.0))), 4)
        except (TypeError, ValueError, OverflowError):
            similarity = 0.0
        examples.append(
            {
                "source": source[:1200],
                "target": target[:1200],
                "similarity": similarity,
                "scope": str(raw_example.get("scope") or "")[:32],
                "applies_to_blocks": sorted(set(block_ids)),
            }
        )

    constraints.sort(
        key=lambda item: (
            {"hard": 0, "preferred": 1, "contextual": 2}.get(str(item.get("mode")), 3),
            min(item.get("applies_to_blocks") or [10**9]),
            str(item.get("source") or "").casefold(),
        )
    )
    examples.sort(key=lambda item: (-float(item.get("similarity") or 0.0), min(item["applies_to_blocks"])))
    return {
        "constraints": constraints[:64],
        "translation_examples": examples[:8],
    }


def _canonical_number(value: str) -> str:
    raw = str(value or "").strip().replace(" ", "")
    if not raw:
        return ""
    sign = ""
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    if not raw:
        return ""

    if "." in raw and "," in raw:
        last_dot = raw.rfind(".")
        last_comma = raw.rfind(",")
        decimal = "." if last_dot > last_comma else ","
        thousands = "," if decimal == "." else "."
        raw = raw.replace(thousands, "")
        raw = raw.replace(decimal, ".")
    elif "." in raw or "," in raw:
        separator = "." if "." in raw else ","
        parts = raw.split(separator)
        if len(parts) > 2 and all(len(part) == 3 for part in parts[1:]):
            raw = "".join(parts)
        elif len(parts) == 2 and len(parts[1]) == 3 and parts[0] not in {"0", ""}:
            raw = "".join(parts)
        else:
            raw = ".".join(parts)

    if "." in raw:
        integer, fraction = raw.split(".", 1)
        integer = integer.lstrip("0") or "0"
        fraction = fraction.rstrip("0")
        raw = integer if not fraction else f"{integer}.{fraction}"
    else:
        raw = raw.lstrip("0") or "0"
    return f"{sign}{raw}"


def _numbers(value: str) -> Counter[str]:
    return Counter(
        canonical
        for match in _NUMBER_RE.findall(str(value or ""))
        if (canonical := _canonical_number(match))
    )


def validate_translation_mapping(
    *,
    blocks: list[dict[str, Any]],
    translations: Mapping[int, str],
    translation_plan: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    block_by_idx: dict[int, dict[str, Any]] = {}
    for block in blocks:
        try:
            block_by_idx[int(block.get("idx"))] = block
        except (TypeError, ValueError):
            continue

    for idx, block in block_by_idx.items():
        source_text = str(block.get("text") or "")
        target_text = str(translations.get(idx) or "").strip()
        if not target_text:
            issues.append(
                {
                    "idx": idx,
                    "type": "empty_translation",
                    "severity": "error",
                    "message": "translation is empty",
                }
            )
            continue
        source_numbers = _numbers(source_text)
        target_numbers = _numbers(target_text)
        missing_numbers = list((source_numbers - target_numbers).elements())
        if missing_numbers:
            issues.append(
                {
                    "idx": idx,
                    "type": "number_mismatch",
                    "severity": "error",
                    "expected": missing_numbers[:12],
                    "message": "one or more source numbers are missing or changed",
                }
            )

    plan = translation_plan if isinstance(translation_plan, Mapping) else {}
    for constraint in plan.get("constraints") or []:
        if not isinstance(constraint, Mapping):
            continue
        mode = str(constraint.get("mode") or "")
        if mode not in {"hard", "preferred"}:
            continue
        source = str(constraint.get("source") or "").strip()
        expected = str(constraint.get("target") or "").strip()
        if not source or not expected:
            continue
        severity = "error" if mode == "hard" else "warning"
        issue_type = "hard_term_missing" if mode == "hard" else "preferred_term_missing"
        for raw_idx in constraint.get("applies_to_blocks") or []:
            try:
                idx = int(raw_idx)
            except (TypeError, ValueError):
                continue
            target_text = str(translations.get(idx) or "")
            if target_text and _norm_text(expected) in _norm_text(target_text):
                continue
            issues.append(
                {
                    "idx": idx,
                    "type": issue_type,
                    "severity": severity,
                    "source_span": source[:240],
                    "expected": expected[:240],
                    "message": (
                        "required glossary term is missing"
                        if mode == "hard"
                        else "preferred researched term was not used"
                    ),
                }
            )
    return issues


def blocking_translation_issues(issues: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(issue) for issue in issues if str(issue.get("severity") or "") == "error"]
