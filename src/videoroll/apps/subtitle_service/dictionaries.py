from __future__ import annotations

import csv
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from defusedxml import ElementTree
from sqlalchemy import text
from sqlalchemy.orm import Session


_TERM_SPLIT_RE = re.compile(r"[\s_\-:/|]+")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_CEDICT_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/\s*$")

_TERM_KEYS = [
    "term",
    "word",
    "headword",
    "source",
    "source_term",
    "source text",
    "source_text",
    "expression",
    "simplified",
    "traditional",
    "kanji",
]
_TRANSLATION_KEYS = [
    "translation",
    "translations",
    "target",
    "target_term",
    "target text",
    "target_text",
    "zh",
    "chinese",
    "meaning_zh",
]
_DEFINITION_KEYS = ["definition", "definitions", "meaning", "gloss", "glosses", "content", "description"]
_POS_KEYS = ["pos", "part_of_speech", "part of speech"]
_TAG_KEYS = ["tag", "tags", "labels", "subject", "category"]
_ALIAS_KEYS = ["alias", "aliases", "variants", "exchange"]
_DOMAIN_KEYS = ["domain", "field", "topic"]

_DICTIONARY_IMPORT_PRESETS: dict[str, dict[str, Any]] = {
    "ecdict": {
        "key": "ecdict",
        "label": "ECDICT",
        "name": "ECDICT",
        "slug": "ecdict",
        "description": "Free English to Chinese dictionary database.",
        "source_lang": "en",
        "target_lang": "zh",
        "format_name": "ecdict",
        "license": "MIT",
        "license_url": "https://github.com/skywind3000/ECDICT/blob/master/LICENSE",
        "source_url": "https://github.com/skywind3000/ECDICT",
        "version": "",
        "domain": "",
        "priority": 80,
        "recommended_full_import": True,
        "recommended_max_entries": 0,
    }
}


@dataclass(frozen=True)
class DictionaryEntryDraft:
    term: str
    translations: list[str]
    source_lang: str = ""
    target_lang: str = "zh"
    pos: str = ""
    definition: str = ""
    domain: str = ""
    tags: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    examples: list[dict[str, str]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    quality: float = 0.5


def normalize_dictionary_term(term: str) -> str:
    return _TERM_SPLIT_RE.sub(" ", str(term or "").strip().lower()).strip()


def normalize_dictionary_slug(value: str) -> str:
    clean = _SLUG_RE.sub("-", str(value or "").strip().lower()).strip("-._")
    return clean[:96] or f"dictionary-{uuid.uuid4().hex[:12]}"


def dictionary_import_presets() -> list[dict[str, Any]]:
    return [dict(item) for item in _DICTIONARY_IMPORT_PRESETS.values()]


def get_dictionary_import_preset(name: str) -> dict[str, Any] | None:
    key = normalize_dictionary_slug(name)
    preset = _DICTIONARY_IMPORT_PRESETS.get(key)
    return dict(preset) if preset else None


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _json_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _compact_items(items: Iterable[Any], *, limit: int = 24, item_limit: int = 240) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = " ".join(str(item or "").strip().split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:item_limit])
        if len(out) >= limit:
            break
    return out


def _split_text_items(value: Any) -> list[str]:
    if isinstance(value, list):
        return _compact_items(value)
    raw = str(value or "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return _compact_items(parsed)
    raw = raw.replace("\\r\\n", "\n").replace("\\n", "\n")
    return _compact_items(re.split(r"\r?\n|;|；|\|", raw))


def _lower_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(k or "").strip().lower(): v for k, v in row.items()}


def _first(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _quality_from_row(row: dict[str, Any]) -> float:
    score = 0.55
    for key in ["collins", "oxford"]:
        try:
            if float(row.get(key) or 0) > 0:
                score += 0.12
        except Exception:
            pass
    try:
        frequency = float(row.get("frq") or row.get("frequency") or 0)
        if frequency > 0:
            score += 0.08
    except Exception:
        pass
    return max(0.0, min(1.0, score))


def _parse_delimited(
    path: Path,
    *,
    delimiter: str,
    default_source_lang: str,
    default_target_lang: str,
    default_domain: str,
) -> Iterator[DictionaryEntryDraft]:
    with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        try:
            has_header = csv.Sniffer().has_header(sample)
        except Exception:
            has_header = True

        if has_header:
            reader: Iterable[dict[str, Any]] = csv.DictReader(fh, delimiter=delimiter)
            for raw_row in reader:
                row = _lower_row(raw_row)
                term = _first(row, _TERM_KEYS)
                translations = _split_text_items(_first(row, _TRANSLATION_KEYS))
                definition = _first(row, _DEFINITION_KEYS)
                if not translations and definition and default_target_lang.lower().startswith("en"):
                    translations = _split_text_items(definition)
                if not term or not translations:
                    continue
                domain = _first(row, _DOMAIN_KEYS) or default_domain
                yield DictionaryEntryDraft(
                    term=term,
                    translations=translations,
                    source_lang=str(row.get("source_lang") or row.get("from") or default_source_lang or "").strip(),
                    target_lang=str(row.get("target_lang") or row.get("to") or default_target_lang or "zh").strip() or "zh",
                    pos=_first(row, _POS_KEYS),
                    definition=definition,
                    domain=domain,
                    tags=_split_text_items(_first(row, _TAG_KEYS)),
                    aliases=_split_text_items(_first(row, _ALIAS_KEYS)),
                    metadata={k: v for k, v in row.items() if k not in {"word", "term", "translation", "definition"}},
                    quality=_quality_from_row(row),
                )
            return

        reader = csv.reader(fh, delimiter=delimiter)
        for row in reader:
            if len(row) < 2:
                continue
            term = str(row[0] or "").strip()
            translations = _split_text_items(row[1])
            if not term or not translations:
                continue
            yield DictionaryEntryDraft(
                term=term,
                translations=translations,
                source_lang=default_source_lang,
                target_lang=default_target_lang or "zh",
                domain=default_domain,
                quality=0.6,
            )


def _parse_cc_cedict(
    path: Path,
    *,
    default_source_lang: str,
    default_target_lang: str,
    default_domain: str,
) -> Iterator[DictionaryEntryDraft]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            match = _CEDICT_RE.match(clean)
            if not match:
                continue
            traditional, simplified, pinyin, gloss_blob = match.groups()
            translations = _compact_items(gloss_blob.split("/"), limit=12)
            if not translations:
                continue
            aliases = [traditional] if traditional != simplified else []
            yield DictionaryEntryDraft(
                term=simplified,
                translations=translations,
                source_lang=default_source_lang or "zh",
                target_lang=default_target_lang or "en",
                domain=default_domain,
                aliases=aliases,
                tags=["cc-cedict"],
                metadata={"traditional": traditional, "pinyin": pinyin},
                quality=0.72,
            )


def _xml_lang(element: Any) -> str:
    return str(
        element.attrib.get("{http://www.w3.org/XML/1998/namespace}lang")
        or element.attrib.get("lang")
        or element.attrib.get("xml:lang")
        or ""
    ).strip()


def _local_name(tag: str) -> str:
    return str(tag or "").rsplit("}", 1)[-1].lower()


def _text_content(element: Any) -> str:
    return " ".join(" ".join(element.itertext()).split())


def _parse_tmx(
    path: Path,
    *,
    default_source_lang: str,
    default_target_lang: str,
    default_domain: str,
) -> Iterator[DictionaryEntryDraft]:
    root = ElementTree.parse(str(path)).getroot()
    for tu in root.iter():
        if _local_name(tu.tag) != "tu":
            continue
        variants: dict[str, list[str]] = {}
        for tuv in tu:
            if _local_name(tuv.tag) != "tuv":
                continue
            lang = _xml_lang(tuv)
            seg_text = ""
            for child in tuv.iter():
                if _local_name(child.tag) == "seg":
                    seg_text = _text_content(child)
                    break
            if lang and seg_text:
                variants.setdefault(lang, []).append(seg_text)
        source_lang = default_source_lang or next(iter(variants.keys()), "")
        target_lang = default_target_lang or "zh"
        for src in variants.get(source_lang, [])[:8]:
            translations = variants.get(target_lang, [])
            if not src or not translations:
                continue
            yield DictionaryEntryDraft(
                term=src,
                translations=_compact_items(translations, limit=8),
                source_lang=source_lang,
                target_lang=target_lang,
                domain=default_domain,
                tags=["tmx"],
                quality=0.8,
            )


def _parse_tbx(
    path: Path,
    *,
    default_source_lang: str,
    default_target_lang: str,
    default_domain: str,
) -> Iterator[DictionaryEntryDraft]:
    root = ElementTree.parse(str(path)).getroot()
    for concept in root.iter():
        if _local_name(concept.tag) not in {"termentry", "conceptentry"}:
            continue
        terms_by_lang: dict[str, list[str]] = {}
        for lang_set in concept.iter():
            if _local_name(lang_set.tag) != "langset":
                continue
            lang = _xml_lang(lang_set)
            if not lang:
                continue
            for child in lang_set.iter():
                if _local_name(child.tag) == "term":
                    value = _text_content(child)
                    if value:
                        terms_by_lang.setdefault(lang, []).append(value)
        source_lang = default_source_lang or next(iter(terms_by_lang.keys()), "")
        target_lang = default_target_lang or "zh"
        translations = _compact_items(terms_by_lang.get(target_lang, []), limit=12)
        if not translations:
            continue
        for term in _compact_items(terms_by_lang.get(source_lang, []), limit=12):
            yield DictionaryEntryDraft(
                term=term,
                translations=translations,
                source_lang=source_lang,
                target_lang=target_lang,
                domain=default_domain,
                tags=["tbx"],
                quality=0.82,
            )


def _parse_jsonl(
    path: Path,
    *,
    default_source_lang: str,
    default_target_lang: str,
    default_domain: str,
) -> Iterator[DictionaryEntryDraft]:
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            clean = line.strip()
            if not clean:
                continue
            try:
                item = json.loads(clean)
            except Exception:
                continue
            if not isinstance(item, dict):
                continue
            word = str(item.get("word") or item.get("term") or "").strip()
            lang = str(item.get("lang_code") or item.get("source_lang") or default_source_lang or "").strip()
            if default_source_lang and lang and lang != default_source_lang:
                continue
            translations: list[str] = []
            for tr in item.get("translations") or []:
                if not isinstance(tr, dict):
                    continue
                tr_lang = str(tr.get("lang_code") or tr.get("target_lang") or "").strip()
                if default_target_lang and tr_lang and tr_lang != default_target_lang:
                    continue
                translations.append(str(tr.get("word") or tr.get("translation") or "").strip())
            definition = ""
            for sense in item.get("senses") or []:
                if not isinstance(sense, dict):
                    continue
                glosses = sense.get("glosses") if isinstance(sense.get("glosses"), list) else []
                if glosses:
                    definition = str(glosses[0] or "").strip()
                    break
            if not translations and definition and default_target_lang.lower().startswith("en"):
                translations = [definition]
            translations = _compact_items(translations, limit=12)
            if not word or not translations:
                continue
            yield DictionaryEntryDraft(
                term=word,
                translations=translations,
                source_lang=lang or default_source_lang,
                target_lang=default_target_lang or "zh",
                definition=definition,
                domain=default_domain,
                tags=_split_text_items(item.get("tags") or []),
                metadata={"source": "jsonl"},
                quality=0.65,
            )


def parse_dictionary_file(
    path: Path,
    *,
    format_name: str,
    source_lang: str = "",
    target_lang: str = "zh",
    domain: str = "",
) -> Iterator[DictionaryEntryDraft]:
    fmt = str(format_name or "").strip().lower()
    suffix = path.suffix.lower().lstrip(".")
    if not fmt or fmt == "auto":
        fmt = suffix or "csv"
    if fmt == "ecdict":
        fmt = "csv"
    if fmt in {"csv"}:
        yield from _parse_delimited(path, delimiter=",", default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    elif fmt in {"tsv", "tab"}:
        yield from _parse_delimited(path, delimiter="\t", default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    elif fmt in {"cc-cedict", "cedict"}:
        yield from _parse_cc_cedict(path, default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    elif fmt == "tmx":
        yield from _parse_tmx(path, default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    elif fmt == "tbx":
        yield from _parse_tbx(path, default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    elif fmt in {"jsonl", "wiktextract"}:
        yield from _parse_jsonl(path, default_source_lang=source_lang, default_target_lang=target_lang, default_domain=domain)
    else:
        raise ValueError(f"unsupported dictionary format: {format_name}")


def upsert_dictionary_source(
    db: Session,
    *,
    name: str,
    slug: str = "",
    description: str = "",
    source_lang: str = "",
    target_lang: str = "zh",
    format_name: str = "csv",
    license: str = "",
    license_url: str = "",
    source_url: str = "",
    version: str = "",
    attribution: str = "",
    domain: str = "",
    priority: int = 0,
    enabled: bool = True,
    metadata: dict[str, Any] | None = None,
) -> str:
    clean_name = " ".join(str(name or "").strip().split()) or "Dictionary"
    clean_slug = normalize_dictionary_slug(slug or clean_name)
    row = db.execute(
        text(
            """
            INSERT INTO translation_dictionary_sources (
                id, name, slug, description, source_lang, target_lang, format,
                license, license_url, source_url, version, attribution, domain,
                priority, enabled, metadata, updated_at
            )
            VALUES (
                CAST(:id AS uuid), :name, :slug, :description, :source_lang, :target_lang, :format,
                :license, :license_url, :source_url, :version, :attribution, :domain,
                :priority, :enabled, CAST(:metadata AS jsonb), now()
            )
            ON CONFLICT (slug) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                source_lang = EXCLUDED.source_lang,
                target_lang = EXCLUDED.target_lang,
                format = EXCLUDED.format,
                license = EXCLUDED.license,
                license_url = EXCLUDED.license_url,
                source_url = EXCLUDED.source_url,
                version = EXCLUDED.version,
                attribution = EXCLUDED.attribution,
                domain = EXCLUDED.domain,
                priority = EXCLUDED.priority,
                enabled = EXCLUDED.enabled,
                metadata = EXCLUDED.metadata,
                updated_at = now()
            RETURNING id
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "name": clean_name,
            "slug": clean_slug,
            "description": str(description or "").strip(),
            "source_lang": str(source_lang or "").strip(),
            "target_lang": str(target_lang or "zh").strip() or "zh",
            "format": str(format_name or "csv").strip().lower() or "csv",
            "license": str(license or "").strip(),
            "license_url": str(license_url or "").strip(),
            "source_url": str(source_url or "").strip(),
            "version": str(version or "").strip(),
            "attribution": str(attribution or "").strip(),
            "domain": str(domain or "").strip(),
            "priority": max(-1000, min(1000, int(priority or 0))),
            "enabled": bool(enabled),
            "metadata": json.dumps(metadata or {}, ensure_ascii=False),
        },
    ).first()
    return str(row[0])


def start_dictionary_import_batch(
    db: Session,
    *,
    source_id: str,
    filename: str,
    archive_path: str,
    file_sha256_value: str,
    file_size_bytes: int,
    format_name: str,
    import_mode: str,
    requested_by: str = "manual",
) -> str:
    batch_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO translation_dictionary_import_batches (
                id, source_id, status, filename, archive_path, file_sha256,
                file_size_bytes, format, import_mode, requested_by, started_at, updated_at
            )
            VALUES (
                CAST(:id AS uuid), CAST(:source_id AS uuid), 'running', :filename, :archive_path, :file_sha256,
                :file_size_bytes, :format, :import_mode, :requested_by, now(), now()
            )
            """
        ),
        {
            "id": batch_id,
            "source_id": source_id,
            "filename": filename,
            "archive_path": archive_path,
            "file_sha256": file_sha256_value,
            "file_size_bytes": int(file_size_bytes or 0),
            "format": str(format_name or "").strip().lower() or "csv",
            "import_mode": str(import_mode or "upsert").strip().lower() or "upsert",
            "requested_by": str(requested_by or "manual").strip() or "manual",
        },
    )
    return batch_id


def _finish_dictionary_import_batch(db: Session, *, batch_id: str, status: str, stats: dict[str, Any], error: str = "") -> None:
    db.execute(
        text(
            """
            UPDATE translation_dictionary_import_batches
            SET status = :status,
                stats = CAST(:stats AS jsonb),
                error = :error,
                finished_at = now(),
                updated_at = now()
            WHERE id = CAST(:id AS uuid)
            """
        ),
        {
            "id": batch_id,
            "status": status,
            "stats": json.dumps(stats, ensure_ascii=False),
            "error": str(error or "")[:4000],
        },
    )


def _insert_dictionary_entry(db: Session, *, source_id: str, batch_id: str, draft: DictionaryEntryDraft) -> str:
    term = str(draft.term or "").strip()
    norm = normalize_dictionary_term(term)
    source_lang = str(draft.source_lang or "").strip()
    target_lang = str(draft.target_lang or "zh").strip() or "zh"
    domain = str(draft.domain or "").strip()
    translations = _compact_items(draft.translations, limit=24)
    translation_text = " | ".join(translations)
    row = db.execute(
        text(
            """
            INSERT INTO translation_dictionary_entries (
                id, source_id, batch_id, source_lang, target_lang, term, normalized_term,
                translations, translation_text, pos, definition, domain, tags, aliases,
                examples, metadata, quality, enabled, updated_at
            )
            VALUES (
                CAST(:id AS uuid), CAST(:source_id AS uuid), CAST(:batch_id AS uuid),
                :source_lang, :target_lang, :term, :normalized_term,
                CAST(:translations AS jsonb), :translation_text, :pos, :definition, :domain,
                CAST(:tags AS jsonb), CAST(:aliases AS jsonb), CAST(:examples AS jsonb),
                CAST(:metadata AS jsonb), :quality, TRUE, now()
            )
            ON CONFLICT (source_id, source_lang, target_lang, normalized_term, domain) DO UPDATE SET
                term = EXCLUDED.term,
                batch_id = EXCLUDED.batch_id,
                translations = EXCLUDED.translations,
                translation_text = EXCLUDED.translation_text,
                pos = EXCLUDED.pos,
                definition = EXCLUDED.definition,
                tags = EXCLUDED.tags,
                aliases = EXCLUDED.aliases,
                examples = EXCLUDED.examples,
                metadata = EXCLUDED.metadata,
                quality = EXCLUDED.quality,
                enabled = TRUE,
                updated_at = now()
            RETURNING id
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "source_id": source_id,
            "batch_id": batch_id,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "term": term,
            "normalized_term": norm,
            "translations": json.dumps(translations, ensure_ascii=False),
            "translation_text": translation_text,
            "pos": str(draft.pos or "").strip(),
            "definition": str(draft.definition or "").strip(),
            "domain": domain,
            "tags": json.dumps(_compact_items(draft.tags), ensure_ascii=False),
            "aliases": json.dumps(_compact_items(draft.aliases), ensure_ascii=False),
            "examples": json.dumps([x for x in draft.examples if isinstance(x, dict)][:12], ensure_ascii=False),
            "metadata": json.dumps(draft.metadata or {}, ensure_ascii=False),
            "quality": max(0.0, min(1.0, float(draft.quality or 0.0))),
        },
    ).first()
    return str(row[0])


def import_dictionary_file(
    db: Session,
    *,
    path: Path,
    filename: str,
    archive_path: str,
    source_name: str,
    slug: str = "",
    description: str = "",
    source_lang: str = "",
    target_lang: str = "zh",
    format_name: str = "csv",
    license: str = "",
    license_url: str = "",
    source_url: str = "",
    version: str = "",
    attribution: str = "",
    domain: str = "",
    priority: int = 0,
    enabled: bool = True,
    import_mode: str = "upsert",
    requested_by: str = "manual",
    max_entries: int = 250000,
) -> dict[str, Any]:
    entry_limit = max(0, int(max_entries or 0))
    file_hash = file_sha256(path)
    source_id = upsert_dictionary_source(
        db,
        name=source_name,
        slug=slug,
        description=description,
        source_lang=source_lang,
        target_lang=target_lang,
        format_name=format_name,
        license=license,
        license_url=license_url,
        source_url=source_url,
        version=version,
        attribution=attribution,
        domain=domain,
        priority=priority,
        enabled=enabled,
        metadata={"last_import_sha256": file_hash},
    )
    batch_id = start_dictionary_import_batch(
        db,
        source_id=source_id,
        filename=filename,
        archive_path=archive_path,
        file_sha256_value=file_hash,
        file_size_bytes=path.stat().st_size,
        format_name=format_name,
        import_mode=import_mode,
        requested_by=requested_by,
    )
    stats = {"parsed": 0, "upserted": 0, "skipped": 0, "max_entries": entry_limit, "full_import": entry_limit <= 0}
    try:
        if str(import_mode or "").strip().lower() == "replace":
            db.execute(text("DELETE FROM translation_dictionary_entries WHERE source_id = CAST(:source_id AS uuid)"), {"source_id": source_id})
        for draft in parse_dictionary_file(path, format_name=format_name, source_lang=source_lang, target_lang=target_lang, domain=domain):
            stats["parsed"] += 1
            if entry_limit > 0 and stats["parsed"] > entry_limit:
                stats["skipped"] += 1
                continue
            term = str(draft.term or "").strip()
            translations = _compact_items(draft.translations)
            if not term or not normalize_dictionary_term(term) or not translations:
                stats["skipped"] += 1
                continue
            _insert_dictionary_entry(db, source_id=source_id, batch_id=batch_id, draft=draft)
            stats["upserted"] += 1
        entry_count = db.execute(
            text("SELECT count(*) FROM translation_dictionary_entries WHERE source_id = CAST(:source_id AS uuid)"),
            {"source_id": source_id},
        ).scalar()
        db.execute(
            text("UPDATE translation_dictionary_sources SET entry_count = :entry_count, updated_at = now() WHERE id = CAST(:source_id AS uuid)"),
            {"source_id": source_id, "entry_count": int(entry_count or 0)},
        )
        _finish_dictionary_import_batch(db, batch_id=batch_id, status="succeeded", stats=stats)
    except Exception as e:
        _finish_dictionary_import_batch(db, batch_id=batch_id, status="failed", stats=stats, error=str(e))
        raise
    return {
        "source_id": source_id,
        "batch_id": batch_id,
        "status": "succeeded",
        **stats,
        "sha256": file_hash,
    }


def _source_row(row: Any) -> dict[str, Any]:
    m = row._mapping
    return {
        "id": str(m["id"]),
        "name": m["name"],
        "slug": m["slug"],
        "description": m["description"],
        "source_lang": m["source_lang"],
        "target_lang": m["target_lang"],
        "format": m["format"],
        "license": m["license"],
        "license_url": m["license_url"],
        "source_url": m["source_url"],
        "version": m["version"],
        "attribution": m["attribution"],
        "domain": m["domain"],
        "priority": int(m["priority"] or 0),
        "enabled": bool(m["enabled"]),
        "metadata": _json_dict(m["metadata"]),
        "entry_count": int(m["entry_count"] or 0),
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


def list_dictionary_sources(db: Session, *, enabled: bool | None = None, q: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(500, int(limit)))}
    if enabled is not None:
        clauses.append("enabled = :enabled")
        params["enabled"] = bool(enabled)
    if q:
        clauses.append("(name ILIKE :q OR slug ILIKE :q OR description ILIKE :q OR domain ILIKE :q)")
        params["q"] = f"%{str(q).strip()}%"
    rows = db.execute(
        text(
            f"""
            SELECT id, name, slug, description, source_lang, target_lang, format,
                   license, license_url, source_url, version, attribution, domain,
                   priority, enabled, metadata, entry_count, created_at, updated_at
            FROM translation_dictionary_sources
            WHERE {' AND '.join(clauses)}
            ORDER BY priority DESC, updated_at DESC, name
            LIMIT :limit
            """
        ),
        params,
    ).all()
    return [_source_row(row) for row in rows]


def update_dictionary_source(
    db: Session,
    source_id: str,
    *,
    enabled: bool | None = None,
    priority: int | None = None,
    domain: str | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    sets: list[str] = []
    params: dict[str, Any] = {"id": str(source_id)}
    if enabled is not None:
        sets.append("enabled = :enabled")
        params["enabled"] = bool(enabled)
    if priority is not None:
        sets.append("priority = :priority")
        params["priority"] = max(-1000, min(1000, int(priority)))
    if domain is not None:
        sets.append("domain = :domain")
        params["domain"] = str(domain or "").strip()
    if description is not None:
        sets.append("description = :description")
        params["description"] = str(description or "").strip()
    if not sets:
        rows = list_dictionary_sources(db)
        return next((row for row in rows if row["id"] == str(source_id)), None)
    row = db.execute(
        text(
            f"""
            UPDATE translation_dictionary_sources
            SET {', '.join(sets)}, updated_at = now()
            WHERE id = CAST(:id AS uuid)
            RETURNING id, name, slug, description, source_lang, target_lang, format,
                      license, license_url, source_url, version, attribution, domain,
                      priority, enabled, metadata, entry_count, created_at, updated_at
            """
        ),
        params,
    ).first()
    return _source_row(row) if row else None


def delete_dictionary_source(db: Session, source_id: str) -> bool:
    result = db.execute(text("DELETE FROM translation_dictionary_sources WHERE id = CAST(:id AS uuid)"), {"id": str(source_id)})
    return int(getattr(result, "rowcount", 0) or 0) > 0


def _entry_row(row: Any) -> dict[str, Any]:
    m = row._mapping
    translations = [str(x) for x in _json_list(m["translations"]) if str(x or "").strip()]
    return {
        "id": str(m["id"]),
        "source_id": str(m["source_id"]),
        "source_name": m.get("source_name", ""),
        "source_slug": m.get("source_slug", ""),
        "source_lang": m["source_lang"],
        "target_lang": m["target_lang"],
        "term": m["term"],
        "normalized_term": m["normalized_term"],
        "translations": translations,
        "translation": translations[0] if translations else "",
        "translation_text": m["translation_text"],
        "pos": m["pos"],
        "definition": m["definition"],
        "domain": m["domain"],
        "tags": _json_list(m["tags"]),
        "aliases": _json_list(m["aliases"]),
        "examples": _json_list(m["examples"]),
        "metadata": _json_dict(m["metadata"]),
        "quality": float(m["quality"] or 0.0),
        "enabled": bool(m["enabled"]),
        "usage_count": int(m["usage_count"] or 0),
        "license": m.get("source_license", ""),
        "license_url": m.get("source_license_url", ""),
        "source_url": m.get("source_url", ""),
        "attribution": m.get("source_attribution", ""),
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


def list_dictionary_entries(
    db: Session,
    *,
    source_id: str | None = None,
    q: str | None = None,
    source_lang: str | None = None,
    target_lang: str | None = None,
    domain: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(500, int(limit))), "offset": max(0, int(offset))}
    if source_id:
        clauses.append("e.source_id = CAST(:source_id AS uuid)")
        params["source_id"] = str(source_id)
    if source_lang:
        clauses.append("e.source_lang = :source_lang")
        params["source_lang"] = str(source_lang).strip()
    if target_lang:
        clauses.append("e.target_lang = :target_lang")
        params["target_lang"] = str(target_lang).strip()
    if domain:
        clauses.append("(e.domain ILIKE :domain OR s.domain ILIKE :domain)")
        params["domain"] = f"%{str(domain).strip()}%"
    if enabled is not None:
        clauses.append("e.enabled = :enabled")
        params["enabled"] = bool(enabled)
    if q:
        clauses.append("(e.term ILIKE :q OR e.translation_text ILIKE :q OR e.definition ILIKE :q OR e.domain ILIKE :q)")
        params["q"] = f"%{str(q).strip()}%"
    rows = db.execute(
        text(
            f"""
            SELECT e.id, e.source_id, s.name AS source_name, s.slug AS source_slug,
                   e.source_lang, e.target_lang, e.term, e.normalized_term,
                   e.translations, e.translation_text, e.pos, e.definition, e.domain,
                   e.tags, e.aliases, e.examples, e.metadata, e.quality, e.enabled,
                   e.usage_count, s.license AS source_license, s.license_url AS source_license_url,
                   s.source_url, s.attribution AS source_attribution,
                   e.created_at, e.updated_at
            FROM translation_dictionary_entries e
            JOIN translation_dictionary_sources s ON s.id = e.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY s.priority DESC, e.updated_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    ).all()
    return [_entry_row(row) for row in rows]


def get_dictionary_entry(db: Session, entry_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT e.id, e.source_id, s.name AS source_name, s.slug AS source_slug,
                   e.source_lang, e.target_lang, e.term, e.normalized_term,
                   e.translations, e.translation_text, e.pos, e.definition, e.domain,
                   e.tags, e.aliases, e.examples, e.metadata, e.quality, e.enabled,
                   e.usage_count, s.license AS source_license, s.license_url AS source_license_url,
                   s.source_url, s.attribution AS source_attribution,
                   e.created_at, e.updated_at
            FROM translation_dictionary_entries e
            JOIN translation_dictionary_sources s ON s.id = e.source_id
            WHERE e.id = CAST(:id AS uuid)
            """
        ),
        {"id": str(entry_id)},
    ).first()
    return _entry_row(row) if row else None


def set_dictionary_entry_enabled(db: Session, entry_id: str, enabled: bool) -> dict[str, Any] | None:
    db.execute(
        text("UPDATE translation_dictionary_entries SET enabled = :enabled, updated_at = now() WHERE id = CAST(:id AS uuid)"),
        {"id": str(entry_id), "enabled": bool(enabled)},
    )
    return get_dictionary_entry(db, entry_id)


def delete_dictionary_entry(db: Session, entry_id: str) -> bool:
    result = db.execute(text("DELETE FROM translation_dictionary_entries WHERE id = CAST(:id AS uuid)"), {"id": str(entry_id)})
    return int(getattr(result, "rowcount", 0) or 0) > 0


def lookup_dictionary_entries(
    db: Session,
    *,
    term: str,
    target_lang: str,
    source_lang: str = "",
    domain: str = "",
    limit: int = 8,
    min_quality: float = 0.0,
    exact: bool = True,
) -> list[dict[str, Any]]:
    clean_term = " ".join(str(term or "").strip().split())
    if not clean_term:
        return []
    norm = normalize_dictionary_term(clean_term)
    params: dict[str, Any] = {
        "norm": norm,
        "q": f"%{clean_term}%",
        "target_lang": str(target_lang or "zh").strip() or "zh",
        "source_lang": str(source_lang or "").strip(),
        "domain": str(domain or "").strip(),
        "limit": max(1, min(50, int(limit))),
        "min_quality": max(0.0, min(1.0, float(min_quality))),
    }
    clauses = [
        "s.enabled = TRUE",
        "e.enabled = TRUE",
        "e.quality >= :min_quality",
        "e.target_lang = :target_lang",
        "(:source_lang = '' OR e.source_lang = '' OR e.source_lang = :source_lang)",
        "(:domain = '' OR e.domain = '' OR e.domain = :domain OR s.domain = '' OR s.domain = :domain)",
    ]
    if exact:
        clauses.append("e.normalized_term = :norm")
    else:
        clauses.append("(e.normalized_term = :norm OR e.term ILIKE :q OR e.translation_text ILIKE :q OR e.definition ILIKE :q)")
    rows = db.execute(
        text(
            f"""
            SELECT e.id, e.source_id, s.name AS source_name, s.slug AS source_slug,
                   e.source_lang, e.target_lang, e.term, e.normalized_term,
                   e.translations, e.translation_text, e.pos, e.definition, e.domain,
                   e.tags, e.aliases, e.examples, e.metadata, e.quality, e.enabled,
                   e.usage_count, s.license AS source_license, s.license_url AS source_license_url,
                   s.source_url, s.attribution AS source_attribution,
                   e.created_at, e.updated_at
            FROM translation_dictionary_entries e
            JOIN translation_dictionary_sources s ON s.id = e.source_id
            WHERE {' AND '.join(clauses)}
            ORDER BY
              CASE WHEN e.normalized_term = :norm THEN 0 ELSE 1 END,
              s.priority DESC,
              e.quality DESC,
              e.usage_count DESC,
              e.updated_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).all()
    out = [_entry_row(row) for row in rows]
    for item in out:
        db.execute(
            text(
                """
                UPDATE translation_dictionary_entries
                SET usage_count = usage_count + 1, last_lookup_at = now(), updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": item["id"]},
        )
    return out


def dictionary_entries_to_evidence(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in entries:
        translations = [str(x) for x in entry.get("translations") or [] if str(x or "").strip()]
        if not translations:
            continue
        source_name = str(entry.get("source_name") or entry.get("source_slug") or "dictionary")
        source_url = str(entry.get("source_url") or entry.get("license_url") or "").strip()
        snippet_parts = [
            f"{entry.get('term')}: {', '.join(translations[:6])}",
            str(entry.get("definition") or "").strip(),
            str(entry.get("pos") or "").strip(),
        ]
        out.append(
            {
                "title": f"Dictionary: {source_name} / {entry.get('term')}",
                "url": source_url,
                "snippet": " | ".join([x for x in snippet_parts if x])[:1200],
                "content": json.dumps(
                    {
                        "term": entry.get("term"),
                        "translations": translations,
                        "definition": entry.get("definition"),
                        "domain": entry.get("domain"),
                        "source": source_name,
                        "license": entry.get("license"),
                        "attribution": entry.get("attribution"),
                    },
                    ensure_ascii=False,
                ),
                "tool": "dictionary_lookup",
                "dictionary_entry_id": entry.get("id"),
                "quality": entry.get("quality"),
            }
        )
    return out


def dictionary_entries_to_context_cards(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for entry in entries:
        translations = [str(x) for x in entry.get("translations") or [] if str(x or "").strip()]
        if not translations:
            continue
        cards.append(
            {
                "term": str(entry.get("term") or ""),
                "translation": translations[0],
                "alternatives": translations[1:6],
                "domain": str(entry.get("domain") or ""),
                "aliases": [str(x) for x in entry.get("aliases") or [] if str(x or "").strip()][:8],
                "description": str(entry.get("definition") or entry.get("pos") or ""),
                "confidence": float(entry.get("quality") or 0.0),
                "score": float(entry.get("quality") or 0.0),
                "sources": [
                    {
                        "source": str(entry.get("source_name") or entry.get("source_slug") or "dictionary"),
                        "url": str(entry.get("source_url") or entry.get("license_url") or ""),
                        "license": str(entry.get("license") or ""),
                    }
                ],
                "knowledge_status": "dictionary_context",
                "dictionary_entry_id": str(entry.get("id") or ""),
            }
        )
    return cards
