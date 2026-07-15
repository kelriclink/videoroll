from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlparse, urlunparse

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session
from pydantic import BaseModel

from videoroll.ai.client import OpenAIChatConfig, request_openai_json_object
from videoroll.apps.egress_gateway.client import EgressGatewayClient, EgressResponse
from videoroll.apps.security.service_auth import service_token
from videoroll.apps.subtitle_service.agent_runtime import (
    AgentBudget,
    AgentBudgetExceeded,
    AgentDecision,
    AgentRuntime,
    AgentTraceEvent,
    GlossaryCandidate,
    RegisteredTool,
    SearchQueryPlan,
    ToolRegistry,
    ToolSpec as RuntimeToolSpec,
    VerificationResult,
    json_schema_for,
    validate_model,
)
from videoroll.apps.subtitle_service.agent_skills import AgentSkill, SkillRegistry
from videoroll.apps.subtitle_service.dictionaries import (
    dictionary_entries_to_context_cards,
    dictionary_entries_to_evidence,
    lookup_dictionary_entries,
)
from videoroll.apps.subtitle_service.embeddings import EmbeddingSettings, assert_embedding_dimensions, embed_text
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.retrieval import RetrievalPipeline
from videoroll.config import get_subtitle_settings
from videoroll.realtime import publish_agent_event


_TERM_SPLIT_RE = re.compile(r"[\s\-_]+")
_CANDIDATE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9][A-Za-z0-9'._:+#/-]*(?:\s+[A-Za-z0-9][A-Za-z0-9'._:+#/-]*){0,3}\b")
_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'._:+#/-]*")
_CJK_TOKEN_RE = re.compile(r"[\u3400-\u9fff]{2,}")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_ARTICLE_RE = re.compile(r"<(?:article|div)\b[^>]*class=[\"'][^\"']*\bresult\b[^\"']*[\"'][^>]*>.*?</(?:article|div)>", re.IGNORECASE | re.DOTALL)
_ANCHOR_RE = re.compile(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_SEARXNG_UI_TEXT_RE = re.compile(
    r"(my searxng|about preferences|preferences\s+search syntax|default language|clear\s+search\s+general)",
    re.IGNORECASE,
)
_SINGLE_LETTER_RE = re.compile(r"^[A-Za-z]$")
_MATH_LOGIC_DOMAIN_RE = re.compile(r"(logic|math|数学|逻辑|命题|proposition|propositional)", re.IGNORECASE)
_AUTO_APPROVE_CONFIDENCE_THRESHOLD = 0.9
_SEARCHABLE_GATE_CATEGORIES = {
    "proper_noun",
    "acronym",
    "domain_jargon",
    "work_specific_term",
    "ambiguous_term",
    "community_meme",
    "technical_standard",
}
_NON_SEARCH_GATE_CATEGORIES = {
    "basic_dictionary",
    "common_word",
    "local_variable",
    "unit_or_number",
    "full_sentence",
    "generic_action",
    "greeting",
}
_TERM_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "for",
    "from",
    "have",
    "he",
    "her",
    "his",
    "i",
    "in",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "this",
    "to",
    "use",
    "we",
    "with",
    "you",
    "your",
}
_COMMON_CONTEXT_TRANSLATIONS: dict[str, dict[str, str]] = {
    "truth table": {
        "translation": "真值表",
        "domain": "技术/逻辑",
        "description": "命题逻辑中列出命题变量所有取值组合及表达式真假结果的表格。",
    },
    "propositional logic": {
        "translation": "命题逻辑",
        "domain": "技术/逻辑",
        "description": "研究命题及命题连接词推理关系的逻辑分支。",
    },
}
_WIKIPEDIA_API_URL = "https://en.wikipedia.org/w/api.php"
_WIKIPEDIA_SOURCE_NAME = "Wikipedia"
_WIKIPEDIA_USER_AGENT = "VideoRoll-RAG-Agent/1.0 (https://github.com/kelriclink/videoroll)"


@dataclass(frozen=True)
class RagSettings:
    enabled: bool = False
    top_k: int = 8
    min_score: float = 0.68
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_model_dir: str = "/models/embeddings"
    embedding_device: str = "cpu"
    auto_discover_terms: bool = False
    auto_learn_terms: bool = False
    dictionary_enabled: bool = True
    dictionary_top_k: int = 8
    dictionary_min_quality: float = 0.0
    dictionary_auto_promote: bool = False
    search_enabled: bool = False
    search_url: str = ""
    search_categories: str = "general"
    search_engines: str = ""
    search_fallback_engines: str = "bing,baidu"
    search_language: str = "all"
    search_safesearch: int = 0
    search_time_range: str = ""
    search_pageno: int = 1
    wiki_enabled: bool = False
    domain: str = ""
    agent_parallelism: int = 1
    agent_timeout_seconds: float = 120.0
    agent_skills_enabled: bool = False
    agent_builtin_skills_enabled: bool = True
    agent_user_skills_enabled: bool = True


@dataclass(frozen=True)
class RagHit:
    id: str
    item_type: str
    term: str
    translation: str
    target_lang: str
    domain: str
    aliases: list[str]
    title: str
    content: str
    description: str
    sources: list[dict[str, Any]]
    confidence: float
    status: str
    score: float


@dataclass(frozen=True)
class RagContext:
    term_cards: list[dict[str, Any]]
    knowledge_cards: list[dict[str, Any]]
    hits: list[RagHit]


@dataclass(frozen=True)
class AgentResearchResult:
    term: str
    normalized_term: str
    context_card: dict[str, Any] | None = None
    hit: RagHit | None = None


@dataclass(frozen=True)
class ToolSpec:
    tool_name: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    description: str = ""
    timeout_seconds: float = 20.0
    retry_count: int = 0
    cost: dict[str, Any] | None = None
    rate_limit: dict[str, Any] | None = None
    guardrails: list[str] | None = None
    redact_fields: list[str] | None = None


@dataclass(frozen=True)
class ToolResult:
    spec: ToolSpec
    input: dict[str, Any]
    output: dict[str, Any]
    ok: bool
    duration_ms: int
    error_type: str = ""
    error: str = ""

    def to_step(self, *, action: str) -> dict[str, Any]:
        status = "ok" if self.ok else "failed"
        step: dict[str, Any] = {
            "event_id": str(uuid.uuid4()),
            "span_id": str(uuid.uuid4()),
            "kind": "tool",
            "action": action,
            "tool": self.spec.tool_name,
            "tool_name": self.spec.tool_name,
            "status": status,
            "description": self.spec.description,
            "input_schema": self.spec.input_schema,
            "output_schema": self.spec.output_schema,
            "input": self.input,
            "output": self.output,
            "ok": self.ok,
            "duration_ms": self.duration_ms,
            "timeout_seconds": self.spec.timeout_seconds,
            "retry_count": self.spec.retry_count,
            "cost": self.spec.cost or {},
            "rate_limit": self.spec.rate_limit or {},
            "guardrails": self.spec.guardrails or [],
            "redact_fields": self.spec.redact_fields or [],
        }
        if self.error_type:
            step["error_type"] = self.error_type
        if self.error:
            step["error"] = self.error
        return step


_SEARCH_TOOL_SPEC = ToolSpec(
    tool_name="search",
    input_schema={"type": "object", "required": ["query"], "properties": {"query": {"type": "string"}, "url": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"count": {"type": "integer"}, "results": {"type": "array"}}},
    description="Search the configured SearXNG instance and return filtered external result summaries.",
    timeout_seconds=20.0,
    retry_count=0,
    cost={"network_requests": 1},
    guardrails=["filter_search_engine_internal_pages", "dedupe_urls", "do_not_fetch_private_hosts"],
)

_FETCH_TOOL_SPEC = ToolSpec(
    tool_name="fetch",
    input_schema={"type": "object", "required": ["url"], "properties": {"url": {"type": "string"}, "title": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"chars": {"type": "integer"}, "excerpt": {"type": "string"}}},
    description="Fetch a public URL and extract compact readable text for evidence.",
    timeout_seconds=20.0,
    retry_count=0,
    cost={"network_requests": 1},
    guardrails=["http_https_only", "block_private_hosts", "limit_response_chars"],
)

_WIKI_SEARCH_TOOL_SPEC = ToolSpec(
    tool_name="wiki_search",
    input_schema={"type": "object", "required": ["query", "api_url"], "properties": {"query": {"type": "string"}, "api_url": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"count": {"type": "integer"}, "results": {"type": "array"}}},
    description="Search English Wikipedia through the MediaWiki API.",
    timeout_seconds=20.0,
    retry_count=0,
    cost={"network_requests": 1},
    guardrails=["fixed_english_wikipedia_api", "dedupe_pageids"],
)

_WIKI_READ_TOOL_SPEC = ToolSpec(
    tool_name="wiki_read",
    input_schema={"type": "object", "required": ["pageid", "api_url"], "properties": {"pageid": {"type": "integer"}, "api_url": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"title": {"type": "string"}, "chars": {"type": "integer"}, "excerpt": {"type": "string"}}},
    description="Read the lead extract for a Wikipedia page.",
    timeout_seconds=20.0,
    retry_count=0,
    cost={"network_requests": 1},
    guardrails=["intro_extract_only", "limit_response_chars"],
)


class RagLookupInput(BaseModel):
    term: str


class RagLookupOutput(BaseModel):
    exists: bool = False
    normalized_term: str = ""


class DictionaryLookupInput(BaseModel):
    term: str
    source_lang: str = ""
    target_lang: str = ""


class DictionaryLookupOutput(BaseModel):
    count: int = 0
    results: list[dict[str, Any]] = []


class WikiSearchInput(BaseModel):
    query: str
    api_url: str = _WIKIPEDIA_API_URL


class WikiSearchOutput(BaseModel):
    count: int = 0
    results: list[dict[str, Any]] = []


class SearchWebInput(BaseModel):
    query: str
    url: str = ""


class SearchWebOutput(BaseModel):
    count: int = 0
    results: list[dict[str, Any]] = []


class FetchUrlInput(BaseModel):
    url: str
    title: str = ""


class FetchUrlOutput(BaseModel):
    chars: int = 0
    excerpt: str = ""


class FinishInput(BaseModel):
    reason: str = ""
    final_answer_ready: bool = False


class FinishOutput(BaseModel):
    finished: bool = True


def _runtime_tool_spec(
    *,
    name: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    timeout_seconds: float = 20.0,
    retry_count: int = 0,
    cost: dict[str, Any] | None = None,
    rate_limit: dict[str, Any] | None = None,
    guardrails: list[str] | None = None,
    redact_fields: list[str] | None = None,
) -> RuntimeToolSpec:
    return RuntimeToolSpec(
        name=name,
        description=description,
        input_schema=json_schema_for(input_model),
        output_schema=json_schema_for(output_model),
        timeout_seconds=timeout_seconds,
        retry_count=retry_count,
        cost=cost or {},
        rate_limit=rate_limit or {},
        guardrails=guardrails or [],
        redact_fields=redact_fields or [],
    )


def _research_tool_registry(rag_settings: RagSettings) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            spec=_runtime_tool_spec(
                name="rag_lookup",
                description="Check whether the local translation knowledge base already contains this term.",
                input_model=RagLookupInput,
                output_model=RagLookupOutput,
                timeout_seconds=5.0,
                guardrails=["read_only", "target_language_scoped"],
            ),
            input_model=RagLookupInput,
            output_model=RagLookupOutput,
        )
    )
    if rag_settings.dictionary_enabled:
        registry.register(
            RegisteredTool(
                spec=_runtime_tool_spec(
                    name="dictionary_lookup",
                    description="Look up imported dictionary and terminology sources without writing to the knowledge base.",
                    input_model=DictionaryLookupInput,
                    output_model=DictionaryLookupOutput,
                    timeout_seconds=5.0,
                    guardrails=["read_only", "source_license_preserved", "do_not_auto_write_knowledge"],
                ),
                input_model=DictionaryLookupInput,
                output_model=DictionaryLookupOutput,
            )
        )
    if rag_settings.wiki_enabled:
        registry.register(
            RegisteredTool(
                spec=_runtime_tool_spec(
                    name="wiki_search",
                    description=_WIKI_SEARCH_TOOL_SPEC.description,
                    input_model=WikiSearchInput,
                    output_model=WikiSearchOutput,
                    timeout_seconds=_WIKI_SEARCH_TOOL_SPEC.timeout_seconds,
                    retry_count=_WIKI_SEARCH_TOOL_SPEC.retry_count,
                    cost=_WIKI_SEARCH_TOOL_SPEC.cost,
                    guardrails=_WIKI_SEARCH_TOOL_SPEC.guardrails,
                ),
                input_model=WikiSearchInput,
                output_model=WikiSearchOutput,
            )
        )
    if rag_settings.search_enabled:
        registry.register(
            RegisteredTool(
                spec=_runtime_tool_spec(
                    name="search_web",
                    description=_SEARCH_TOOL_SPEC.description,
                    input_model=SearchWebInput,
                    output_model=SearchWebOutput,
                    timeout_seconds=_SEARCH_TOOL_SPEC.timeout_seconds,
                    retry_count=_SEARCH_TOOL_SPEC.retry_count,
                    cost=_SEARCH_TOOL_SPEC.cost,
                    guardrails=_SEARCH_TOOL_SPEC.guardrails,
                ),
                input_model=SearchWebInput,
                output_model=SearchWebOutput,
            )
        )
    registry.register(
        RegisteredTool(
            spec=_runtime_tool_spec(
                name="fetch_url",
                description=_FETCH_TOOL_SPEC.description,
                input_model=FetchUrlInput,
                output_model=FetchUrlOutput,
                timeout_seconds=_FETCH_TOOL_SPEC.timeout_seconds,
                retry_count=_FETCH_TOOL_SPEC.retry_count,
                cost=_FETCH_TOOL_SPEC.cost,
                guardrails=_FETCH_TOOL_SPEC.guardrails,
            ),
            input_model=FetchUrlInput,
            output_model=FetchUrlOutput,
        )
    )
    registry.register(
        RegisteredTool(
            spec=_runtime_tool_spec(
                name="finish",
                description="Stop the child agent when evidence is sufficient or further research is not useful.",
                input_model=FinishInput,
                output_model=FinishOutput,
                timeout_seconds=1.0,
                guardrails=["requires_reason"],
            ),
            input_model=FinishInput,
            output_model=FinishOutput,
        )
    )
    return registry


def _ordered_tool_specs(registry: ToolRegistry) -> list[dict[str, Any]]:
    order = ["rag_lookup", "dictionary_lookup", "wiki_search", "search_web", "fetch_url", "finish"]
    out: list[dict[str, Any]] = []
    for name in order:
        try:
            spec = registry.spec(name)
        except KeyError:
            continue
        out.append(spec.model_dump())
    return out


def load_agent_skill_registry(rag_settings: RagSettings, *, force: bool = False) -> SkillRegistry:
    if not force and not rag_settings.agent_skills_enabled:
        return SkillRegistry(())
    return SkillRegistry.load(
        include_builtin=bool(rag_settings.agent_builtin_skills_enabled),
        include_user=bool(rag_settings.agent_user_skills_enabled),
    )


def _active_skill_payloads(skills: list[AgentSkill]) -> list[dict[str, Any]]:
    return [skill.prompt_payload() for skill in skills if skill.runnable]


def _tool_specs_for_active_skills(registry: ToolRegistry, active_skills: list[AgentSkill]) -> tuple[list[dict[str, Any]], list[str]]:
    available_tool_specs = _ordered_tool_specs(registry)
    allowed_by_skill: set[str] = set()
    for skill in active_skills:
        allowed_by_skill.update(name for name in skill.allowed_tools if name)
    if allowed_by_skill:
        allowed_by_skill.update({"rag_lookup", "finish"})
        available_tool_specs = [spec for spec in available_tool_specs if str(spec.get("name") or "") in allowed_by_skill]
    available_tools = [str(spec.get("name") or "") for spec in available_tool_specs if str(spec.get("name") or "")]
    return available_tool_specs, available_tools


_SEARXNG_TIME_RANGES = {"", "day", "month", "year"}


def _clean_searxng_csv(value: Any, *, default: str = "", limit: int = 20) -> str:
    raw_items = str(value or default or "").replace("\n", ",").split(",")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        clean = " ".join(str(item or "").strip().split())
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean[:80])
        if len(out) >= limit:
            break
    return ",".join(out)


def _clean_searxng_language(value: Any) -> str:
    clean = str(value or "all").strip()
    return (clean or "all")[:32]


def _clean_searxng_safesearch(value: Any) -> int:
    try:
        return max(0, min(2, int(value if value is not None else 0)))
    except Exception:
        return 0


def _clean_searxng_time_range(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in _SEARXNG_TIME_RANGES else ""


def _searxng_search_params(
    *,
    categories: str = "general",
    engines: str = "",
    language: str = "all",
    safesearch: int = 0,
    time_range: str = "",
    pageno: int = 1,
) -> dict[str, str]:
    params: dict[str, str] = {}
    clean_categories = _clean_searxng_csv(categories, default="general")
    clean_engines = _clean_searxng_csv(engines, default="")
    clean_language = _clean_searxng_language(language)
    clean_time_range = _clean_searxng_time_range(time_range)
    if clean_categories:
        params["categories"] = clean_categories
    if clean_engines:
        params["engines"] = clean_engines
    if clean_language:
        params["language"] = clean_language
    params["safesearch"] = str(_clean_searxng_safesearch(safesearch))
    if clean_time_range:
        params["time_range"] = clean_time_range
    try:
        clean_pageno = max(1, min(100, int(pageno or 1)))
    except Exception:
        clean_pageno = 1
    params["pageno"] = str(clean_pageno)
    return params


def _clean_searxng_pageno(value: Any) -> int:
    try:
        return max(1, min(100, int(value or 1)))
    except Exception:
        return 1


def rag_settings_from_translate_settings(settings: dict[str, Any]) -> RagSettings:
    return RagSettings(
        enabled=bool(settings.get("rag_enabled")),
        top_k=max(0, min(30, int(settings.get("rag_top_k") or 8))),
        min_score=max(0.0, min(1.0, float(settings.get("rag_min_score") or 0.68))),
        embedding_provider=str(settings.get("rag_embedding_provider") or "openai").strip().lower() or "openai",
        embedding_model=str(settings.get("rag_embedding_model") or "text-embedding-3-small").strip() or "text-embedding-3-small",
        embedding_dimensions=max(1, min(4096, int(settings.get("rag_embedding_dimensions") or 1536))),
        embedding_model_dir=str(settings.get("rag_embedding_model_dir") or "/models/embeddings").strip() or "/models/embeddings",
        embedding_device=str(settings.get("rag_embedding_device") or "cpu").strip() or "cpu",
        auto_discover_terms=bool(settings.get("rag_auto_discover_terms")),
        auto_learn_terms=bool(settings.get("rag_auto_learn_terms")),
        dictionary_enabled=bool(settings.get("rag_dictionary_enabled") if "rag_dictionary_enabled" in settings else True),
        dictionary_top_k=max(0, min(30, int(settings.get("rag_dictionary_top_k") or 8))),
        dictionary_min_quality=max(0.0, min(1.0, float(settings.get("rag_dictionary_min_quality") or 0.0))),
        dictionary_auto_promote=bool(settings.get("rag_dictionary_auto_promote")),
        search_enabled=bool(settings.get("rag_search_enabled")),
        search_url=str(settings.get("rag_search_url") or "").strip(),
        search_categories=_clean_searxng_csv(settings.get("rag_search_categories"), default="general"),
        search_engines=_clean_searxng_csv(settings.get("rag_search_engines"), default=""),
        search_fallback_engines=_clean_searxng_csv(settings.get("rag_search_fallback_engines"), default="bing,baidu"),
        search_language=_clean_searxng_language(settings.get("rag_search_language")),
        search_safesearch=_clean_searxng_safesearch(settings.get("rag_search_safesearch")),
        search_time_range=_clean_searxng_time_range(settings.get("rag_search_time_range")),
        search_pageno=_clean_searxng_pageno(settings.get("rag_search_pageno")),
        wiki_enabled=bool(settings.get("rag_wiki_enabled")),
        domain=str(settings.get("rag_domain") or "").strip(),
        agent_parallelism=max(1, min(8, int(settings.get("rag_agent_parallelism") or 1))),
        agent_timeout_seconds=max(10.0, min(900.0, float(settings.get("rag_agent_timeout_seconds") or 120.0))),
        agent_skills_enabled=bool(settings.get("rag_agent_skills_enabled")),
        agent_builtin_skills_enabled=bool(
            settings.get("rag_agent_builtin_skills_enabled")
            if "rag_agent_builtin_skills_enabled" in settings
            else True
        ),
        agent_user_skills_enabled=bool(
            settings.get("rag_agent_user_skills_enabled")
            if "rag_agent_user_skills_enabled" in settings
            else True
        ),
    )


def normalize_term(term: str) -> str:
    s = str(term or "").strip().lower()
    s = _TERM_SPLIT_RE.sub(" ", s)
    return s


def _add_unique_term_candidate(out: list[str], seen: set[str], raw: str, *, limit: int) -> bool:
    clean = " ".join(str(raw or "").strip(" .,:;!?()[]{}\"'“”‘’").split())
    if len(clean) < 2:
        return False
    norm = normalize_term(clean)
    if not norm or norm in seen:
        return False
    seen.add(norm)
    out.append(clean)
    return len(out) >= limit


def _block_lookup_candidates_from_text(text_value: str, *, limit: int = 96) -> list[str]:
    text = str(text_value or "")
    limit = max(1, min(200, int(limit)))
    seen: set[str] = set()
    out: list[str] = []

    tokens = [match.group(0).strip(" .,:;!?()[]{}\"'") for match in _WORD_TOKEN_RE.finditer(text)]
    tokens = [token for token in tokens if token]
    max_ngram = 4
    for size in range(max_ngram, 0, -1):
        for start in range(0, max(0, len(tokens) - size + 1)):
            phrase_tokens = tokens[start : start + size]
            lower_tokens = [token.lower() for token in phrase_tokens]
            if size == 1:
                token = phrase_tokens[0]
                if lower_tokens[0] in _TERM_STOPWORDS:
                    continue
                if len(token) <= 2 and not token.isupper():
                    continue
            else:
                if lower_tokens[0] in _TERM_STOPWORDS or lower_tokens[-1] in _TERM_STOPWORDS:
                    continue
                if all(token in _TERM_STOPWORDS for token in lower_tokens):
                    continue
            if _add_unique_term_candidate(out, seen, " ".join(phrase_tokens), limit=limit):
                return out

    for match in _CJK_TOKEN_RE.finditer(text):
        chunk = match.group(0)
        if _add_unique_term_candidate(out, seen, chunk, limit=limit):
            return out
        for size in range(min(6, len(chunk)), 1, -1):
            for start in range(0, len(chunk) - size + 1):
                if _add_unique_term_candidate(out, seen, chunk[start : start + size], limit=limit):
                    return out

    for match in _CANDIDATE_RE.finditer(text):
        if _add_unique_term_candidate(out, seen, match.group(0), limit=limit):
            return out

    return out


def normalize_wiki_api_url(raw_url: str) -> str:
    raw = str(raw_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/w/api.php"
    elif path.endswith("/api.php"):
        path = path
    elif "/wiki/" in path:
        prefix = path.split("/wiki/", 1)[0].rstrip("/")
        path = f"{prefix}/w/api.php" if prefix else "/w/api.php"
    elif path.endswith("/wiki"):
        prefix = path[: -len("/wiki")].rstrip("/")
        path = f"{prefix}/w/api.php" if prefix else "/w/api.php"
    else:
        path = f"{path}/w/api.php"
    return urlunparse(parsed._replace(path=path, query="", fragment=""))


def _wiki_page_url(api_url: str, title: str) -> str:
    parsed = urlparse(normalize_wiki_api_url(api_url))
    path = parsed.path
    if path.endswith("/w/api.php"):
        root = path[: -len("/w/api.php")]
    elif path.endswith("/api.php"):
        root = path[: -len("/api.php")]
    else:
        root = ""
    page_path = f"{root.rstrip('/')}/wiki/{quote(str(title or '').strip().replace(' ', '_'))}"
    return urlunparse(parsed._replace(path=page_path, query="", fragment=""))


def build_knowledge_embedding_text(
    *,
    item_type: str,
    term: str = "",
    translation: str = "",
    domain: str = "",
    aliases: Iterable[str] | None = None,
    title: str = "",
    content: str = "",
    description: str = "",
) -> str:
    parts = [
        f"type: {item_type}",
        f"domain: {domain}".strip(),
        f"term: {term}".strip(),
        f"translation: {translation}".strip(),
        f"aliases: {', '.join([a for a in aliases or [] if a])}".strip(),
        f"title: {title}".strip(),
        f"description: {description}".strip(),
        f"content: {content}".strip(),
    ]
    return "\n".join([p for p in parts if p and not p.endswith(":")]).strip()


def _hash_text(text_value: str) -> str:
    return hashlib.sha256(str(text_value or "").encode("utf-8")).hexdigest()


def _vector_literal(values: list[float]) -> str:
    if not values:
        raise ValueError("embedding vector is empty")
    return "[" + ",".join(f"{float(v):.8g}" for v in values) + "]"


def embedding_model_key(rag_settings: RagSettings) -> str:
    provider = str(rag_settings.embedding_provider or "openai").strip().lower() or "openai"
    model = str(rag_settings.embedding_model or "").strip()
    return f"{provider}:{model}" if model else provider


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _duration_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _strip_html(value: str) -> str:
    text_value = _SCRIPT_STYLE_RE.sub(" ", str(value or ""))
    text_value = re.sub(r"<br\s*/?>", "\n", text_value, flags=re.IGNORECASE)
    text_value = _TAG_RE.sub(" ", text_value)
    text_value = html.unescape(text_value)
    text_value = re.sub(r"[ \t\r\f\v]+", " ", text_value)
    text_value = re.sub(r"\n\s+", "\n", text_value)
    return text_value.strip()


def _collapse_text(value: str, *, limit: int = 12000) -> str:
    text_value = html.unescape(str(value or ""))
    text_value = re.sub(r"[ \t\r\f\v]+", " ", text_value)
    text_value = re.sub(r"\n{3,}", "\n\n", text_value)
    text_value = text_value.strip()
    return text_value[:limit]


def _context_for_llm(text_value: str, *, previous_summary: str = "", limit: int = 9000) -> str:
    current = str(text_value or "").strip()
    summary = str(previous_summary or "").strip()
    if summary:
        combined = f"前文摘要：\n{summary[:800]}\n\n当前字幕 block：\n{current}"
    else:
        combined = current
    return combined[:limit]


def _search_endpoint_from_base(search_base_url: str) -> str:
    raw = str(search_base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"q", "format"}]
    path = parsed.path.rstrip("/")
    if not path:
        path = "/search"
    elif path.endswith("/search"):
        path = path
    else:
        path = f"{path}/search"
    return urlunparse(parsed._replace(path=path, query=urlencode(params)))


def _search_url_with_params(
    search_base_url: str,
    *,
    query: str,
    json_format: bool,
    extra_params: dict[str, str] | None = None,
) -> str:
    endpoint = _search_endpoint_from_base(search_base_url)
    parsed = urlparse(endpoint)
    params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in {"q", "format"}]
    existing_names = {k for k, _v in params}
    for key, value in (extra_params or {}).items():
        if key in {"q", "format"} or key in existing_names:
            continue
        params.append((key, value))
    params.append(("q", query))
    if json_format:
        params.append(("format", "json"))
    return urlunparse(parsed._replace(query=urlencode(params)))


def _search_endpoint_has_param(search_base_url: str, name: str) -> bool:
    endpoint = _search_endpoint_from_base(search_base_url)
    parsed = urlparse(endpoint)
    return any(k == name for k, _v in parse_qsl(parsed.query, keep_blank_values=True))


def _normalize_result_url(raw_url: str, *, base_url: str) -> str:
    url = html.unescape(str(raw_url or "").strip())
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"}:
        return url
    if url.startswith("//"):
        return f"https:{url}"
    return urljoin(base_url, url)


def _is_search_engine_internal_url(url: str, *, search_url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    search_parsed = urlparse(_search_endpoint_from_base(search_url))
    hostname = (parsed.hostname or "").lower()
    if hostname in {"searx.space", "www.searx.space"}:
        return True
    if not parsed.netloc or not search_parsed.netloc:
        return False
    if parsed.netloc.lower() != search_parsed.netloc.lower():
        return False
    path = parsed.path.rstrip("/").lower()
    search_path = search_parsed.path.rstrip("/").lower()
    search_root = search_path[: -len("/search")] if search_path.endswith("/search") else ""
    if path in {"", "/"}:
        return True
    if search_root and path == search_root:
        return True
    return (
        path == search_path
        or path.startswith(f"{search_root}/info")
        or path.startswith(f"{search_root}/preferences")
        or path.startswith(f"{search_root}/stats")
        or path.startswith(f"{search_root}/config")
        or path.startswith(f"{search_root}/about")
    )


def _is_fetchable_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except ValueError:
        return False
    expected_port = {"http": 80, "https": 443}.get(parsed.scheme.lower())
    if expected_port is None or not parsed.hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if port is not None and port != expected_port:
        return False
    return True


def _url_with_params(url: str, params: dict[str, Any] | None) -> str:
    if not params:
        return url
    parsed = urlparse(url)
    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    for name, value in params.items():
        if isinstance(value, (list, tuple)):
            pairs.extend((str(name), str(item)) for item in value)
        else:
            pairs.append((str(name), str(value)))
    return urlunparse(parsed._replace(query=urlencode(pairs, doseq=True)))


class _PublicFetchClient:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self.timeout = timeout
        self.headers = headers or {}
        self.redirects = 5 if follow_redirects else 0
        settings = get_subtitle_settings()
        self.gateway = EgressGatewayClient(
            _egress_gateway_url(settings),
            service_token(settings),
            timeout=timeout,
            transport=_gateway_transport_factory(),
        )

    def __enter__(self) -> _PublicFetchClient:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.gateway.close()
        return False

    def get(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        redirects: int | None = None,
    ) -> EgressResponse:
        return self.gateway.fetch(
            _url_with_params(url, params),
            timeout=self.timeout,
            max_bytes=500_000,
            redirects=self.redirects if redirects is None else redirects,
        )


def _egress_gateway_url(settings: Any) -> str:
    configured = str(getattr(settings, "egress_gateway_url", "") or "").strip()
    return configured or str(os.getenv("EGRESS_GATEWAY_URL") or "http://egress-gateway:8020").strip()


def _gateway_transport_factory() -> httpx.BaseTransport | None:
    return None


def _safe_public_get(client: Any, url: str, *, max_redirects: int = 5) -> Any:
    if not _is_fetchable_url(url):
        raise RuntimeError(f"refusing to fetch invalid public URL: {url}")
    try:
        return client.get(url, redirects=max_redirects)
    except TypeError:
        return client.get(url)


def _parse_search_json(data: Any) -> list[dict[str, Any]]:
    raw_results = data.get("results") if isinstance(data, dict) else None
    if raw_results is None and isinstance(data, list):
        raw_results = data
    if not isinstance(raw_results, list):
        return []

    out: list[dict[str, Any]] = []
    for item in raw_results[:10]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or "").strip()
        snippet = str(item.get("snippet") or item.get("content") or item.get("description") or "").strip()
        if not title and not snippet:
            continue
        if _SEARXNG_UI_TEXT_RE.search(" ".join([title, snippet])):
            continue
        out.append({"title": _strip_html(title), "url": url, "snippet": _strip_html(snippet)[:800]})
    return out


def _filter_search_results(results: list[dict[str, Any]], *, search_url: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in results:
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        if not url:
            continue
        if _is_search_engine_internal_url(url, search_url=search_url):
            continue
        if _SEARXNG_UI_TEXT_RE.search(" ".join([title, snippet])):
            continue
        if title.lower() in {"about", "preferences", "search syntax"} and "searxng" in snippet.lower():
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": title, "url": url, "snippet": snippet[:800]})
        if len(out) >= 8:
            break
    return out


def _parse_search_html(html_value: str, *, base_url: str) -> list[dict[str, Any]]:
    source = str(html_value or "")
    chunks = _ARTICLE_RE.findall(source)
    if not chunks:
        chunks = re.findall(r"<article\b[^>]*>.*?</article>", source, flags=re.IGNORECASE | re.DOTALL)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks[:20]:
        anchors = _ANCHOR_RE.findall(chunk)
        if not anchors:
            continue
        title = ""
        url = ""
        for href, label_html in anchors:
            candidate_url = _normalize_result_url(href, base_url=base_url)
            if not candidate_url:
                continue
            if _is_search_engine_internal_url(candidate_url, search_url=base_url):
                continue
            parsed = urlparse(candidate_url)
            if parsed.scheme not in {"http", "https"}:
                continue
            clean_label = _strip_html(label_html)
            if not clean_label:
                continue
            if clean_label.lower() in {"about", "preferences", "search syntax"}:
                continue
            title = clean_label[:300]
            url = candidate_url
            break
        if not title or not url or url in seen:
            continue
        seen.add(url)
        text_value = _strip_html(chunk)
        snippet = text_value.replace(title, "", 1).strip()
        out.append({"title": title, "url": url, "snippet": snippet[:800]})
        if len(out) >= 8:
            break
    return out


def _extract_page_text(html_value: str) -> str:
    source = _SCRIPT_STYLE_RE.sub(" ", str(html_value or ""))
    title_match = re.search(r"<title\b[^>]*>(.*?)</title>", source, flags=re.IGNORECASE | re.DOTALL)
    title = _strip_html(title_match.group(1)) if title_match else ""
    main_match = re.search(r"<main\b[^>]*>(.*?)</main>", source, flags=re.IGNORECASE | re.DOTALL)
    article_match = re.search(r"<article\b[^>]*>(.*?)</article>", source, flags=re.IGNORECASE | re.DOTALL)
    body_match = re.search(r"<body\b[^>]*>(.*?)</body>", source, flags=re.IGNORECASE | re.DOTALL)
    body = (article_match or main_match or body_match)
    body_text = _strip_html(body.group(1) if body else source)
    joined = "\n\n".join([x for x in [title, body_text] if x])
    return _collapse_text(joined, limit=12000)


def _start_agent_run(
    db: Session,
    *,
    term: str,
    domain: str,
    target_lang: str,
    query: str,
    agent_type: str = "rag_term_research",
    parent_agent_run_id: str | None = None,
    task_id: str | None = None,
    subtitle_job_id: str | None = None,
) -> str:
    run_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO translation_agent_runs (
                id, agent_type, status, term, normalized_term, domain, target_lang,
                task_id, subtitle_job_id, query, steps, result, parent_agent_run_id,
                started_at, updated_at
            )
            VALUES (
                CAST(:id AS uuid), :agent_type, 'running', :term, :normalized_term,
                :domain, :target_lang, CAST(:task_id AS uuid), CAST(:subtitle_job_id AS uuid),
                :query, '[]'::jsonb, '{}'::jsonb, CAST(:parent_agent_run_id AS uuid), now(), now()
            )
            """
        ),
        {
            "id": run_id,
            "agent_type": str(agent_type or "rag_term_research").strip()[:64] or "rag_term_research",
            "term": str(term or "").strip(),
            "normalized_term": normalize_term(term),
            "domain": str(domain or "").strip(),
            "target_lang": str(target_lang or "zh").strip() or "zh",
            "task_id": task_id,
            "subtitle_job_id": subtitle_job_id,
            "query": str(query or "").strip(),
            "parent_agent_run_id": parent_agent_run_id,
        },
    )
    db.commit()
    publish_agent_event(
        get_subtitle_settings().redis_url,
        run_id=run_id,
        name="agent_run.started",
        data={
            "id": run_id,
            "agent_type": str(agent_type or "rag_term_research").strip()[:64] or "rag_term_research",
            "status": "running",
            "term": str(term or "").strip(),
            "domain": str(domain or "").strip(),
            "target_lang": str(target_lang or "zh").strip() or "zh",
            "task_id": task_id,
            "subtitle_job_id": subtitle_job_id,
            "query": str(query or "").strip(),
            "parent_agent_run_id": parent_agent_run_id,
        },
    )
    return run_id


def _append_agent_step(db: Session, run_id: str | None, step: dict[str, Any]) -> None:
    if not run_id:
        return
    clean_step = dict(step)
    clean_step.setdefault("event_id", str(uuid.uuid4()))
    clean_step.setdefault("span_id", str(uuid.uuid4()))
    clean_step.setdefault("at", _utc_now().isoformat())
    clean_step.setdefault("status", "failed" if clean_step.get("error") or clean_step.get("ok") is False else "ok")
    if "tool" in clean_step and "tool_name" not in clean_step:
        clean_step["tool_name"] = clean_step["tool"]
    try:
        db.execute(
            text(
                """
                UPDATE translation_agent_runs
                SET steps = steps || CAST(:step AS jsonb),
                    updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": run_id, "step": json.dumps([clean_step], ensure_ascii=False)},
        )
        db.commit()
        publish_agent_event(
            get_subtitle_settings().redis_url,
            run_id=run_id,
            name="agent_run.step_appended",
            data={
                "id": run_id,
                "step": {
                    key: clean_step.get(key)
                    for key in (
                        "event_id",
                        "kind",
                        "action",
                        "at",
                        "status",
                        "tool_name",
                        "model",
                        "duration_ms",
                        "ok",
                        "error_type",
                    )
                    if clean_step.get(key) is not None
                },
            },
        )
    except Exception:
        db.rollback()


def _append_tool_result(db: Session | None, run_id: str | None, result: ToolResult, *, action: str) -> None:
    if db is None:
        return
    _append_agent_step(db, run_id, result.to_step(action=action))


def _append_llm_step(
    db: Session | None,
    run_id: str | None,
    *,
    action: str,
    config: OpenAIChatConfig | None = None,
    input_value: dict[str, Any] | None = None,
    output_value: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error: str = "",
    error_type: str = "",
) -> None:
    if db is None:
        return
    step: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "span_id": str(uuid.uuid4()),
        "kind": "llm",
        "action": action,
        "status": "failed" if error or error_type else "ok",
    }
    if config is not None:
        step["model"] = config.model
        step["tokens"] = None
    if input_value is not None:
        step["input"] = input_value
    if output_value is not None:
        step["output"] = output_value
    if duration_ms is not None:
        step["duration_ms"] = duration_ms
    if error_type:
        step["error_type"] = error_type
    if error:
        step["error"] = error[:1000]
    _append_agent_step(db, run_id, step)


def _append_state_transition(
    db: Session | None,
    run_id: str | None,
    *,
    from_node: str,
    to_node: str,
    reason: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    if db is None:
        return
    _append_agent_step(
        db,
        run_id,
        {
            "kind": "agent",
            "action": "state_transition",
            "from_node": from_node,
            "to_node": to_node,
            "reason": reason,
            "metadata": metadata or {},
        },
    )


def _agent_budget_for_rag(rag_settings: RagSettings, *, max_steps: int = 6) -> AgentBudget:
    timeout_seconds = max(10.0, min(900.0, float(rag_settings.agent_timeout_seconds or 120.0)))
    return AgentBudget(
        max_llm_calls=max(4, min(24, int(max_steps) + 6)),
        max_tool_calls=max(4, min(30, int(max_steps) * 2 + 4)),
        max_fetch_calls=4,
        timeout_seconds=timeout_seconds,
    )


def _finish_agent_run(
    db: Session,
    run_id: str | None,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: str = "",
    knowledge_item_id: str | None = None,
) -> None:
    if not run_id:
        return
    try:
        db.execute(
            text(
                """
                UPDATE translation_agent_runs
                SET status = :status,
                    result = CAST(:result AS jsonb),
                    error = :error,
                    knowledge_item_id = CAST(:knowledge_item_id AS uuid),
                    finished_at = now(),
                    updated_at = now()
                WHERE id = CAST(:id AS uuid)
                """
            ),
            {
                "id": run_id,
                "status": str(status or "succeeded").strip() or "succeeded",
                "result": json.dumps(result or {}, ensure_ascii=False),
                "error": str(error or "")[:4000],
                "knowledge_item_id": knowledge_item_id,
            },
        )
        db.commit()
        publish_agent_event(
            get_subtitle_settings().redis_url,
            run_id=run_id,
            name="agent_run.finished",
            data={
                "id": run_id,
                "status": str(status or "succeeded").strip() or "succeeded",
                "error": str(error or "")[:1000],
                "knowledge_item_id": knowledge_item_id,
            },
        )
    except Exception:
        db.rollback()


def _row_to_hit(row: Any) -> RagHit:
    mapping = getattr(row, "_mapping", row)
    sources_raw = _json_list(mapping.get("sources"))
    sources = [x for x in sources_raw if isinstance(x, dict)]
    aliases = [str(x) for x in _json_list(mapping.get("aliases")) if str(x or "").strip()]
    return RagHit(
        id=str(mapping.get("id") or ""),
        item_type=str(mapping.get("item_type") or ""),
        term=str(mapping.get("term") or ""),
        translation=str(mapping.get("translation") or ""),
        target_lang=str(mapping.get("target_lang") or ""),
        domain=str(mapping.get("domain") or ""),
        aliases=aliases,
        title=str(mapping.get("title") or ""),
        content=str(mapping.get("content") or ""),
        description=str(mapping.get("description") or ""),
        sources=sources,
        confidence=float(mapping.get("confidence") or 0.0),
        status=str(mapping.get("status") or ""),
        score=float(mapping.get("score") or 0.0),
    )


def _term_candidates_from_text(text_value: str, *, limit: int = 24) -> list[str]:
    return _block_lookup_candidates_from_text(text_value, limit=limit)


def _lookup_dictionary_entries_for_terms(
    db: Session,
    *,
    terms: Iterable[str],
    target_lang: str,
    domain: str,
    per_term_limit: int,
    total_limit: int,
    min_quality: float,
    seen_entry_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen_ids = seen_entry_ids if seen_entry_ids is not None else set()
    seen_terms: set[str] = set()
    total_limit = max(1, min(80, int(total_limit)))
    per_term_limit = max(1, min(8, int(per_term_limit)))
    for term in terms:
        clean = " ".join(str(term or "").strip().split())
        norm = normalize_term(clean)
        if not clean or not norm or norm in seen_terms:
            continue
        seen_terms.add(norm)
        try:
            hits = lookup_dictionary_entries(
                db,
                term=clean,
                source_lang="",
                target_lang=target_lang,
                domain=domain,
                limit=per_term_limit,
                min_quality=min_quality,
                exact=True,
            )
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            errors.append(
                {
                    "term": clean,
                    "error_type": type(e).__name__,
                    "error": str(e)[:500],
                }
            )
            break
        for entry in hits:
            entry_id = str(entry.get("id") or "")
            if not entry_id or entry_id in seen_ids:
                continue
            seen_ids.add(entry_id)
            entries.append(entry)
            if len(entries) >= total_limit:
                return entries, errors
    return entries, errors


def _context_card_norms(cards: Iterable[dict[str, Any]]) -> set[str]:
    norms: set[str] = set()
    for card in cards:
        if not isinstance(card, dict):
            continue
        for value in [card.get("term"), *(card.get("aliases") or [])]:
            norm = normalize_term(str(value or ""))
            if norm:
                norms.add(norm)
    return norms


def _rag_hit_to_local_context(hit: RagHit) -> dict[str, Any]:
    return {
        "source": "rag_knowledge_base",
        "term": hit.term or hit.title,
        "translation": hit.translation,
        "domain": hit.domain,
        "description": hit.description or hit.content,
        "score": round(hit.score, 4),
        "confidence": hit.confidence,
        "status": hit.status,
    }


def _dictionary_card_to_local_context(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "dictionary",
        "term": str(card.get("term") or ""),
        "translation": str(card.get("translation") or ""),
        "alternatives": [str(x) for x in card.get("alternatives") or [] if str(x or "").strip()][:5],
        "domain": str(card.get("domain") or ""),
        "description": str(card.get("description") or ""),
        "score": card.get("score"),
        "confidence": card.get("confidence"),
        "status": str(card.get("knowledge_status") or "dictionary_context"),
    }


def _compact_local_context_for_gate(items: Iterable[dict[str, Any]], *, limit: int = 40) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        translation = str(item.get("translation") or "").strip()
        norm = normalize_term(term)
        if not norm or not translation:
            continue
        key = (str(item.get("source") or ""), norm)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "source": str(item.get("source") or "")[:60],
                "term": term[:160],
                "translation": translation[:160],
                "alternatives": [str(x)[:120] for x in item.get("alternatives") or [] if str(x or "").strip()][:5],
                "domain": str(item.get("domain") or "")[:120],
                "description": str(item.get("description") or "")[:400],
                "score": item.get("score"),
                "confidence": item.get("confidence"),
                "status": str(item.get("status") or "")[:80],
            }
        )
        if len(out) >= limit:
            break
    return out


def should_research_term(
    term: str,
    *,
    domain: str = "",
    context: str = "",
    gate_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    clean = str(term or "").strip()
    norm = normalize_term(clean)
    domain_text = str(domain or "").strip()
    category_hint = str((gate_item or {}).get("category") or "").strip()
    scope_hint = str((gate_item or {}).get("scope") or "").strip()
    need_rag_hint = (gate_item or {}).get("need_rag")
    need_search_hint = (gate_item or {}).get("need_search")
    if not clean or not norm:
        return {
            "should_research": False,
            "should_persist": False,
            "category": "empty",
            "reason": "empty term",
        }
    if _SINGLE_LETTER_RE.fullmatch(clean):
        category = "local_variable" if _MATH_LOGIC_DOMAIN_RE.search(domain_text) or _MATH_LOGIC_DOMAIN_RE.search(context or "") else "single_letter"
        return {
            "should_research": False,
            "should_persist": False,
            "category": category,
            "reason": "single-letter symbols are treated as local variables unless manually added to the knowledge base",
        }
    context_term = _COMMON_CONTEXT_TRANSLATIONS.get(norm)
    if context_term:
        return {
            "should_research": False,
            "should_persist": False,
            "category": "context_only",
            "reason": "common foundational term; provide a temporary translation hint without long-term auto-learning",
            "translation": context_term["translation"],
            "domain": context_term.get("domain") or domain_text,
            "description": context_term.get("description") or "",
        }
    if len(norm) <= 2 and clean.isalpha() and not clean.isupper():
        return {
            "should_research": False,
            "should_persist": False,
            "category": "short_token",
            "reason": "short lowercase token is unlikely to be a reusable translation term",
        }
    if norm in {"the", "and", "or", "to", "of", "in", "this", "that", "you", "we", "they", "it"}:
        return {
            "should_research": False,
            "should_persist": False,
            "category": "common_word",
            "reason": "common function word",
        }
    if category_hint in _NON_SEARCH_GATE_CATEGORIES:
        return {
            "should_research": False,
            "should_persist": False,
            "category": category_hint,
            "scope": scope_hint or "none",
            "reason": str((gate_item or {}).get("reason") or "RAG gate classified this as not requiring external knowledge"),
        }
    if need_rag_hint is False:
        return {
            "should_research": False,
            "should_persist": False,
            "category": category_hint or "gate_rejected",
            "scope": scope_hint or "none",
            "reason": str((gate_item or {}).get("reason") or "RAG gate decided this term does not need RAG"),
        }
    if category_hint and category_hint not in _SEARCHABLE_GATE_CATEGORIES and need_search_hint is not True:
        return {
            "should_research": False,
            "should_persist": scope_hint in {"task", "series", "global"},
            "category": category_hint,
            "scope": scope_hint or "task",
            "reason": str((gate_item or {}).get("reason") or "RAG gate did not classify this as external-search-worthy"),
        }
    if need_search_hint is False and category_hint not in _SEARCHABLE_GATE_CATEGORIES:
        return {
            "should_research": False,
            "should_persist": scope_hint in {"task", "series", "global"},
            "category": category_hint or "task_context",
            "scope": scope_hint or "task",
            "reason": str((gate_item or {}).get("reason") or "RAG gate requested local/task context only"),
        }
    return {
        "should_research": True,
        "should_persist": scope_hint != "task",
        "category": category_hint or "research",
        "scope": scope_hint or "global",
        "reason": str((gate_item or {}).get("reason") or "term may need external context"),
        "need_search": bool(need_search_hint) if need_search_hint is not None else True,
    }


def _context_only_term_card(term: str, decision: dict[str, Any], *, target_lang: str, domain: str) -> dict[str, Any]:
    return {
        "term": str(term or "").strip(),
        "translation": str(decision.get("translation") or "").strip(),
        "domain": str(decision.get("domain") or domain or "").strip(),
        "aliases": [],
        "description": str(decision.get("description") or decision.get("reason") or "").strip(),
        "confidence": 0.9,
        "score": 0.9,
        "sources": [],
        "target_lang": target_lang,
        "status": "context_only",
    }


def discover_terms_openai(
    text_value: str,
    *,
    target_lang: str,
    domain_hint: str,
    previous_summary: str = "",
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    source = str(text_value or "").strip()
    if not source:
        return []
    llm_context = _context_for_llm(source, previous_summary=previous_summary, limit=8000)
    data = request_openai_json_object(
        config=config,
        system_prompt="You identify translation terms in subtitles. Return ONLY valid JSON.",
        user_prompt=(
            "请从下面字幕片段中识别会影响翻译质量的术语/专有名词/梗/黑话。\n"
            "要求：\n"
            "- 只输出 JSON 对象；\n"
            "- terms 最多 20 个；\n"
            "- term 保持原文；domain 如果能判断就给出游戏/动漫/技术领域；\n"
            "- reason 用中文简短说明为什么它是术语。\n"
            f"- 目标语言：{target_lang or 'zh'}\n"
            f"- 领域提示：{domain_hint or '未知'}\n\n"
            f"上下文：\n{llm_context}\n\n"
            '输出 JSON：{"terms":[{"term":"","domain":"","reason":""}]}'
        ),
        client=client,
    )
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in terms:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        norm = normalize_term(term)
        if not term or norm in seen:
            continue
        seen.add(norm)
        out.append(
            {
                "term": term,
                "domain": str(item.get("domain") or domain_hint or "").strip(),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return out[:20]


def pretranslation_rag_gate_openai(
    text_value: str,
    *,
    target_lang: str,
    domain_hint: str,
    existing_terms: list[dict[str, Any]] | None = None,
    local_context: list[dict[str, Any]] | None = None,
    previous_summary: str = "",
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    source = str(text_value or "").strip()
    if not source:
        return []
    llm_context = _context_for_llm(source, previous_summary=previous_summary, limit=9000)
    compact_existing = [
        {
            "term": str(item.get("term") or "")[:160],
            "translation": str(item.get("translation") or "")[:160],
            "domain": str(item.get("domain") or "")[:120],
            "score": item.get("score"),
        }
        for item in (existing_terms or [])[:20]
        if isinstance(item, dict)
    ]
    compact_local_context = _compact_local_context_for_gate(local_context or [], limit=40)
    data = request_openai_json_object(
        config=config,
        system_prompt="You are a pre-translation RAG gate. Return ONLY valid JSON.",
        user_prompt=(
            "请在翻译这个字幕 block 之前，判断哪些词/短语如果不查资料或不查本地知识库，可能会翻错。\n"
            "这不是普通 NER，也不是词典抽取；目标是筛出高价值 RAG 候选。\n\n"
            "只返回满足以下至少一项的候选：\n"
            "- 专有名词、人名、组织、作品、角色、品牌；\n"
            "- 缩写或代号，且在当前语境可能有特定含义；\n"
            "- 游戏/动漫/技术/社区黑话、梗、固定译法；\n"
            "- 作品设定、领域标准、框架名、工具名、论文/协议/技术名词；\n"
            "- 普通词但当前语境可能有特殊含义或歧义。\n\n"
            "不要返回这些：\n"
            "- 基础词典词、常见动词/名词、寒暄句；\n"
            "- 数字、单位、尺寸、普通食材；\n"
            "- true/false/condition/validate/pixel/salt 这类模型能稳定翻译的基础词；\n"
            "- P/Q/R/x/y 这类局部变量；\n"
            "- 整句普通表达，除非它是固定梗或固定术语。\n\n"
            "category 只能使用：proper_noun, acronym, domain_jargon, work_specific_term, "
            "ambiguous_term, community_meme, technical_standard, task_context, basic_dictionary, "
            "common_word, local_variable, unit_or_number, full_sentence, generic_action, greeting。\n"
            "scope 只能使用：global, series, task, none。\n"
            "need_rag 表示是否需要本地 RAG/上下文辅助；need_search 表示本地未命中时是否值得外部搜索。\n"
            "如果“已有本地上下文 JSON”里的 RAG 或词典命中与当前字幕 block 和前文摘要贴切，"
            "应优先复用该译法/解释；这种情况下不需要外部搜索，need_search=false。"
            "如果本地上下文已经足够支持直接翻译，也可以不返回该词。\n"
            "只有当本地上下文缺失、明显不贴合、或仍无法判断固定译法时，才把 need_search 设为 true。\n"
            "如果你认为没有值得查的词，返回空数组。不要为了凑数返回基础词。返回数量不设上限，但必须是高价值项。\n\n"
            f"目标语言：{target_lang or 'zh'}\n"
            f"领域提示：{domain_hint or '未知'}\n"
            f"已有 RAG 术语命中 JSON：\n{json.dumps(compact_existing, ensure_ascii=False)}\n\n"
            f"已有本地上下文 JSON（来自 RAG 知识库或已导入词典）：\n"
            f"{json.dumps(compact_local_context, ensure_ascii=False)}\n\n"
            f"上下文：\n{llm_context}\n\n"
            '输出 JSON：{"terms":[{"term":"","category":"","domain":"","need_rag":true,'
            '"need_search":true,"scope":"global","priority":0.0,"reason":""}]}'
        ),
        client=client,
    )
    terms = data.get("terms")
    if not isinstance(terms, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in terms:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        norm = normalize_term(term)
        if not term or norm in seen:
            continue
        seen.add(norm)
        try:
            priority = float(item.get("priority") or 0.0)
        except Exception:
            priority = 0.0
        category = str(item.get("category") or "").strip()
        scope = str(item.get("scope") or "").strip()
        need_rag_raw = item.get("need_rag")
        need_search_raw = item.get("need_search")
        out.append(
            {
                "term": term,
                "category": category,
                "domain": str(item.get("domain") or domain_hint or "").strip(),
                "need_rag": True if need_rag_raw is None else _json_bool(need_rag_raw),
                "need_search": (category in _SEARCHABLE_GATE_CATEGORIES) if need_search_raw is None else _json_bool(need_search_raw),
                "scope": scope if scope in {"global", "series", "task", "none"} else "global",
                "priority": max(0.0, min(1.0, priority)),
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    out.sort(key=lambda x: float(x.get("priority") or 0.0), reverse=True)
    return out


def _dedupe_search_queries(queries: Iterable[str], *, limit: int = 3) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        clean = re.sub(r"\s+", " ", str(query or "").strip())
        if not clean:
            continue
        norm = clean.lower()
        if norm in seen:
            continue
        seen.add(norm)
        out.append(clean[:240])
        if len(out) >= max(1, min(5, int(limit))):
            break
    return out


def _fallback_search_queries(term: str, *, domain: str = "", target_lang: str = "zh", limit: int = 3) -> list[str]:
    clean_term = str(term or "").strip()
    clean_domain = str(domain or "").strip()
    if not clean_term:
        return []
    queries = [
        " ".join([p for p in [clean_domain, clean_term] if p]),
        " ".join([p for p in [clean_term, clean_domain, "definition"] if p]),
    ]
    if str(target_lang or "").lower().startswith("zh"):
        queries.append(" ".join([p for p in [clean_term, clean_domain, "中文 术语"] if p]))
    return _dedupe_search_queries(queries, limit=limit)


def generate_search_queries_openai(
    *,
    term: str,
    context: str,
    target_lang: str,
    domain_hint: str,
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
    max_queries: int = 3,
) -> list[str]:
    clean_term = str(term or "").strip()
    if not clean_term:
        return []
    safe_max = max(1, min(5, int(max_queries)))
    data = request_openai_json_object(
        config=config,
        system_prompt="You generate web search queries for a translation research agent. Return ONLY valid JSON.",
        user_prompt=(
            "请为字幕翻译术语研究生成高质量搜索 query。\n"
            "要求：\n"
            f"- queries 输出 2 到 {safe_max} 条；\n"
            "- query 要能帮助判断术语在当前上下文里的含义；\n"
            "- 优先加入领域关键词、definition/wiki/documentation 等有助于找到权威解释的词；\n"
            "- 不要生成站内搜索语法，不要编造 URL。\n\n"
            f"术语：{clean_term}\n"
            f"目标语言：{target_lang or 'zh'}\n"
            f"领域提示：{domain_hint or '未知'}\n\n"
            f"字幕上下文：\n{context[:2500]}\n\n"
            '输出 JSON：{"queries":[""]}'
        ),
        client=client,
    )
    try:
        plan = validate_model(SearchQueryPlan, data)
    except Exception:
        return []
    queries = _dedupe_search_queries([str(x or "") for x in plan.queries], limit=safe_max)
    return queries or _fallback_search_queries(clean_term, domain=domain_hint, target_lang=target_lang, limit=safe_max)


def decide_fetch_urls_openai(
    *,
    term: str,
    context: str,
    target_lang: str,
    domain_hint: str,
    search_results: list[dict[str, Any]],
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
    max_pages: int = 4,
) -> dict[str, Any]:
    compact_results: list[dict[str, Any]] = []
    for idx, item in enumerate(search_results[:8]):
        compact_results.append(
            {
                "index": idx,
                "title": str(item.get("title") or "")[:240],
                "snippet": str(item.get("snippet") or "")[:700],
                "url": str(item.get("url") or "")[:1000],
            }
        )
    safe_max_pages = max(1, min(6, int(max_pages)))
    data = request_openai_json_object(
        config=config,
        system_prompt="You decide whether a translation research agent should fetch pages. Return ONLY valid JSON.",
        user_prompt=(
            "你是字幕翻译术语研究 Agent。请根据字幕上下文和搜索结果，决定是否需要打开网页正文。\n"
            "要求：\n"
            "- 如果搜索标题和摘要已经足够判断术语含义，就不要打开网页；\n"
            "- 如果摘要过短、来源不清、存在歧义，选择最有价值的 URL 打开；\n"
            f"- 最多选择 {safe_max_pages} 个 URL；优先官方 Wiki、官方文档、百科、项目文档；\n"
            "- 只从给定 search_results 里选择 URL，不要编造 URL；\n"
            "- confidence 表示仅凭当前搜索摘要判断术语含义的把握。\n\n"
            f"术语：{term}\n"
            f"目标语言：{target_lang or 'zh'}\n"
            f"领域提示：{domain_hint or '未知'}\n\n"
            f"字幕上下文：\n{context[:2500]}\n\n"
            f"search_results JSON：\n{json.dumps(compact_results, ensure_ascii=False)}\n\n"
            '输出 JSON：{"summary_sufficient":true,"fetch_urls":[],"reason":"","confidence":0.0}'
        ),
        client=client,
    )
    allowed = {str(item.get("url") or "").strip() for item in search_results if str(item.get("url") or "").strip()}
    urls: list[str] = []
    raw_urls = data.get("fetch_urls")
    if isinstance(raw_urls, list):
        for value in raw_urls:
            url = str(value or "").strip()
            if url and url in allowed and url not in urls:
                urls.append(url)
    raw_indexes = data.get("fetch_indexes")
    if isinstance(raw_indexes, list):
        for value in raw_indexes:
            try:
                idx = int(value)
            except Exception:
                continue
            if 0 <= idx < len(search_results):
                url = str(search_results[idx].get("url") or "").strip()
                if url and url in allowed and url not in urls:
                    urls.append(url)
    try:
        confidence = float(data.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    raw_sufficient = data.get("summary_sufficient")
    if isinstance(raw_sufficient, str):
        summary_sufficient = raw_sufficient.strip().lower() in {"1", "true", "yes", "y"}
    else:
        summary_sufficient = bool(raw_sufficient)
    return {
        "summary_sufficient": summary_sufficient,
        "fetch_urls": urls[: max(1, min(6, int(max_pages)))],
        "reason": str(data.get("reason") or "").strip()[:1000],
        "confidence": max(0.0, min(1.0, confidence)),
    }


def fetch_search_evidence(
    term: str,
    *,
    domain: str,
    search_url: str,
    search_categories: str = "general",
    search_engines: str = "",
    search_fallback_engines: str = "bing,baidu",
    search_language: str = "all",
    search_safesearch: int = 0,
    search_time_range: str = "",
    search_pageno: int = 1,
    queries: list[str] | None = None,
    context: str = "",
    target_lang: str = "zh",
    config: OpenAIChatConfig | None = None,
    timeout_seconds: float = 20.0,
    max_pages: int = 4,
    db: Session | None = None,
    agent_run_id: str | None = None,
) -> list[dict[str, Any]]:
    endpoint = str(search_url or "").strip()
    if not endpoint:
        return []
    if queries is None:
        search_queries = _dedupe_search_queries([" ".join([p for p in [str(domain or "").strip(), str(term or "").strip()] if p])], limit=1)
    else:
        search_queries = _dedupe_search_queries(queries, limit=3)
    if not search_queries:
        return []

    search_results: list[dict[str, Any]] = []
    seen_result_urls: set[str] = set()
    try:
        with _PublicFetchClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "VideoRoll-RAG-Agent/1.0", "Accept": "application/json, text/html;q=0.9,*/*;q=0.8"},
        ) as client:
            base_search_params = _searxng_search_params(
                categories=search_categories,
                engines=search_engines,
                language=search_language,
                safesearch=search_safesearch,
                time_range=search_time_range,
                pageno=search_pageno,
            )
            fallback_engines = _clean_searxng_csv(search_fallback_engines, default="")
            has_configured_engines = _search_endpoint_has_param(endpoint, "engines") or bool(base_search_params.get("engines"))
            for query in search_queries:
                filtered_results: list[dict[str, Any]] = []
                attempt_configs: list[dict[str, str]] = [dict(base_search_params)]
                if not has_configured_engines and fallback_engines:
                    fallback_params = dict(base_search_params)
                    fallback_params["engines"] = fallback_engines
                    attempt_configs.append(fallback_params)
                for attempt_index, extra_params in enumerate(attempt_configs):
                    json_url = _search_url_with_params(endpoint, query=query, json_format=True, extra_params=extra_params)
                    html_url = _search_url_with_params(endpoint, query=query, json_format=False, extra_params=extra_params)
                    started = time.perf_counter()
                    json_error = ""
                    raw_result_count: int | None = None
                    parsed_result_count = 0
                    unresponsive_engines: list[Any] = []
                    try:
                        try:
                            resp = client.get(json_url)
                            resp.raise_for_status()
                            content_type = resp.headers.get("content-type", "")
                            if "json" in content_type.lower():
                                data = resp.json()
                                raw_results = data.get("results") if isinstance(data, dict) else data if isinstance(data, list) else None
                                raw_result_count = len(raw_results) if isinstance(raw_results, list) else None
                                if isinstance(data, dict):
                                    raw_unresponsive = data.get("unresponsive_engines")
                                    unresponsive_engines = raw_unresponsive if isinstance(raw_unresponsive, list) else []
                                filtered_results = _parse_search_json(data)
                                parsed_result_count = len(filtered_results)
                            else:
                                try:
                                    data = resp.json()
                                    raw_results = data.get("results") if isinstance(data, dict) else data if isinstance(data, list) else None
                                    raw_result_count = len(raw_results) if isinstance(raw_results, list) else None
                                    if isinstance(data, dict):
                                        raw_unresponsive = data.get("unresponsive_engines")
                                        unresponsive_engines = raw_unresponsive if isinstance(raw_unresponsive, list) else []
                                    filtered_results = _parse_search_json(data)
                                    parsed_result_count = len(filtered_results)
                                except Exception:
                                    filtered_results = _parse_search_html(resp.text, base_url=str(resp.url))
                                    parsed_result_count = len(filtered_results)
                        except Exception as e:
                            json_error = str(e)[:300]
                            filtered_results = []
                        if not filtered_results and raw_result_count is None:
                            try:
                                resp = client.get(html_url)
                                resp.raise_for_status()
                                filtered_results = _parse_search_html(resp.text, base_url=str(resp.url))
                                parsed_result_count = len(filtered_results)
                            except Exception as e:
                                if json_error:
                                    raise RuntimeError(f"json search failed: {json_error}; html search failed: {e}") from e
                                raise
                        filtered_results = _filter_search_results(filtered_results, search_url=endpoint)
                        compact_results = [
                            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": str(r.get("snippet") or "")[:240]}
                            for r in filtered_results[:8]
                        ]
                        output_value: dict[str, Any] = {
                            "count": len(filtered_results),
                            "raw_count": raw_result_count,
                            "parsed_count": parsed_result_count,
                            "filtered_count": len(filtered_results),
                            "results": compact_results,
                            "json_error": json_error,
                            "search_params": extra_params,
                        }
                        if attempt_index > 0:
                            output_value["fallback_params"] = extra_params
                        if unresponsive_engines:
                            output_value["unresponsive_engines"] = unresponsive_engines[:8]
                        step = ToolResult(
                            spec=_SEARCH_TOOL_SPEC,
                            input={"query": query, "url": json_url, "fallback_params": extra_params or {}},
                            output=output_value,
                            ok=True,
                            duration_ms=_duration_ms(started),
                        ).to_step(action="search")
                        step.update(
                            {
                                "query": query,
                                "url": json_url,
                                "count": len(filtered_results),
                                "raw_count": raw_result_count,
                                "parsed_count": parsed_result_count,
                                "filtered_count": len(filtered_results),
                                "results": compact_results,
                                "retry_count": attempt_index,
                                "search_params": extra_params,
                            }
                        )
                        if attempt_index > 0:
                            step["fallback_params"] = extra_params
                        if unresponsive_engines:
                            step["unresponsive_engines"] = unresponsive_engines[:8]
                        if db is not None:
                            _append_agent_step(db, agent_run_id, step)
                    except Exception as e:
                        step = ToolResult(
                            spec=_SEARCH_TOOL_SPEC,
                            input={"query": query, "url": json_url, "fallback_params": extra_params or {}},
                            output={"count": 0, "results": []},
                            ok=False,
                            duration_ms=_duration_ms(started),
                            error_type=type(e).__name__,
                            error=str(e)[:300],
                        ).to_step(action="search_failed")
                        step.update({"query": query, "url": json_url, "retry_count": attempt_index})
                        if extra_params:
                            step["fallback_params"] = extra_params
                        if db is not None:
                            _append_agent_step(db, agent_run_id, step)
                        continue
                    if filtered_results:
                        break
                for item in filtered_results:
                    url = str(item.get("url") or "").strip()
                    if not url or url in seen_result_urls:
                        continue
                    seen_result_urls.add(url)
                    search_results.append(item)
                    if len(search_results) >= 8:
                        break
                if len(search_results) >= 8:
                    break

            if not search_results:
                if db is not None:
                    _append_agent_step(
                        db,
                        agent_run_id,
                        {
                            "kind": "tool",
                            "action": "search_no_valid_results",
                            "tool": "search",
                            "reason": "No usable external search results after filtering search engine internal pages.",
                        },
                    )
                return []
            fetch_decision = {
                "summary_sufficient": False,
                "fetch_urls": [],
                "reason": "LLM fetch decision was not available; using compatibility fallback.",
                "confidence": 0.0,
            }
            decision_failed = False
            if config is not None and search_results:
                started = time.perf_counter()
                try:
                    decision_input = {
                        "term": term,
                        "domain": domain,
                        "target_lang": target_lang,
                        "context_excerpt": str(context or "")[:500],
                        "results": [
                            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("snippet", "")[:240]}
                            for r in search_results[:8]
                        ],
                    }
                    fetch_decision = decide_fetch_urls_openai(
                        term=term,
                        context=context,
                        target_lang=target_lang,
                        domain_hint=domain,
                        search_results=search_results,
                        config=config,
                        max_pages=max_pages,
                    )
                    _append_llm_step(
                        db,
                        agent_run_id,
                        action="decide_fetch",
                        config=config,
                        input_value=decision_input,
                        output_value=fetch_decision,
                        duration_ms=_duration_ms(started),
                    )
                except Exception as e:
                    decision_failed = True
                    _append_llm_step(
                        db,
                        agent_run_id,
                        action="decide_fetch_failed",
                        config=config,
                        duration_ms=_duration_ms(started),
                        error=str(e)[:300],
                        error_type=type(e).__name__,
                    )
            if config is None:
                fetch_decision["fetch_urls"] = [
                    str(r.get("url") or "").strip()
                    for r in search_results[: max(1, min(6, int(max_pages)))]
                    if str(r.get("url") or "").strip()
                ]
            elif decision_failed and not fetch_decision.get("fetch_urls"):
                fetch_decision["fetch_urls"] = [
                    str(r.get("url") or "").strip()
                    for r in search_results[:1]
                    if str(r.get("url") or "").strip()
                ]
                if db is not None:
                    _append_agent_step(
                        db,
                        agent_run_id,
                        {
                            "kind": "policy",
                            "action": "fetch_fallback",
                            "reason": "LLM fetch decision failed; falling back to the first filtered external result.",
                            "urls": fetch_decision["fetch_urls"],
                        },
                    )
            selected_urls = {str(url or "").strip() for url in fetch_decision.get("fetch_urls") or [] if str(url or "").strip()}
            out: list[dict[str, Any]] = []
            fetched_count = 0
            for item in search_results[:8]:
                title = str(item.get("title") or "").strip()
                url = str(item.get("url") or "").strip()
                snippet = str(item.get("snippet") or "").strip()
                page: dict[str, Any] = {"title": title, "url": url, "snippet": snippet[:800]}
                if url and url in selected_urls and _is_fetchable_url(url) and fetched_count < max(1, min(6, int(max_pages))):
                    started = time.perf_counter()
                    try:
                        page_resp = _safe_public_get(client, url)
                        page_resp.raise_for_status()
                        ctype = page_resp.headers.get("content-type", "").lower()
                        body = page_resp.text[:500_000]
                        if "html" in ctype or "<html" in body[:1000].lower():
                            page_text = _extract_page_text(body)
                        else:
                            page_text = _collapse_text(body, limit=12000)
                        page["content"] = page_text[:5000]
                        fetched_count += 1
                        step = ToolResult(
                            spec=_FETCH_TOOL_SPEC,
                            input={"url": url, "title": title},
                            output={"url": url, "chars": len(page_text), "excerpt": page_text[:360]},
                            ok=True,
                            duration_ms=_duration_ms(started),
                        ).to_step(action="read_url")
                        step.update({"url": url, "title": title, "chars": len(page_text), "excerpt": page_text[:360]})
                        if db is not None:
                            _append_agent_step(db, agent_run_id, step)
                    except Exception as e:
                        page["fetch_error"] = str(e)[:300]
                        step = ToolResult(
                            spec=_FETCH_TOOL_SPEC,
                            input={"url": url, "title": title},
                            output={"url": url},
                            ok=False,
                            duration_ms=_duration_ms(started),
                            error_type=type(e).__name__,
                            error=str(e)[:300],
                        ).to_step(action="read_url_failed")
                        step.update({"url": url, "title": title})
                        if db is not None:
                            _append_agent_step(db, agent_run_id, step)
                elif url:
                    if url in selected_urls:
                        page["fetch_skipped"] = "not_fetchable_or_page_limit"
                    else:
                        page["fetch_skipped"] = "not_selected_by_agent"
                if title or snippet or page.get("content"):
                    out.append(page)
            return out
    except Exception:
        if db is not None:
            _append_agent_step(
                db,
                agent_run_id,
                {
                    "kind": "tool",
                    "action": "search_failed",
                    "tool": "search",
                    "tool_name": "search",
                    "queries": search_queries,
                    "error_type": "unexpected_error",
                },
            )
        return []


def fetch_wikipedia_evidence(
    term: str,
    *,
    domain: str,
    queries: list[str] | None = None,
    timeout_seconds: float = 20.0,
    max_pages: int = 3,
    db: Session | None = None,
    agent_run_id: str | None = None,
) -> list[dict[str, Any]]:
    clean_term = str(term or "").strip()
    if not clean_term:
        return []
    raw_queries = [clean_term]
    raw_queries.extend(queries or [])
    search_queries = _dedupe_search_queries(raw_queries, limit=4)
    if not search_queries:
        return []

    search_results: list[dict[str, Any]] = []
    seen_pages: set[str] = set()
    try:
        with _PublicFetchClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _WIKIPEDIA_USER_AGENT, "Accept": "application/json"},
        ) as client:
            for query in search_queries:
                started = time.perf_counter()
                params = {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 5,
                    "format": "json",
                    "formatversion": "2",
                }
                try:
                    resp = client.get(_WIKIPEDIA_API_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    raw_items = data.get("query", {}).get("search", []) if isinstance(data, dict) else []
                    results: list[dict[str, Any]] = []
                    if isinstance(raw_items, list):
                        for raw in raw_items[:5]:
                            if not isinstance(raw, dict):
                                continue
                            title = _strip_html(str(raw.get("title") or "")).strip()
                            pageid = raw.get("pageid")
                            snippet = _strip_html(str(raw.get("snippet") or "")).strip()
                            if not title or pageid is None:
                                continue
                            page_key = str(pageid)
                            result = {
                                "title": title,
                                "pageid": pageid,
                                "url": _wiki_page_url(_WIKIPEDIA_API_URL, title),
                                "snippet": snippet[:800],
                                "source": _WIKIPEDIA_SOURCE_NAME,
                                "tool": "wiki",
                            }
                            results.append(result)
                            if page_key not in seen_pages:
                                seen_pages.add(page_key)
                                search_results.append(result)
                    compact_results = [
                        {"title": r["title"], "pageid": r["pageid"], "url": r["url"], "snippet": str(r.get("snippet") or "")[:240]}
                        for r in results[:5]
                    ]
                    step = ToolResult(
                        spec=_WIKI_SEARCH_TOOL_SPEC,
                        input={"query": query, "api_url": _WIKIPEDIA_API_URL},
                        output={"count": len(results), "results": compact_results},
                        ok=True,
                        duration_ms=_duration_ms(started),
                    ).to_step(action="wiki_search")
                    step.update({"query": query, "api_url": _WIKIPEDIA_API_URL, "count": len(results), "results": compact_results})
                    if db is not None:
                        _append_agent_step(db, agent_run_id, step)
                except Exception as e:
                    step = ToolResult(
                        spec=_WIKI_SEARCH_TOOL_SPEC,
                        input={"query": query, "api_url": _WIKIPEDIA_API_URL},
                        output={"count": 0, "results": []},
                        ok=False,
                        duration_ms=_duration_ms(started),
                        error_type=type(e).__name__,
                        error=str(e)[:300],
                    ).to_step(action="wiki_search_failed")
                    step.update({"query": query, "api_url": _WIKIPEDIA_API_URL})
                    if db is not None:
                        _append_agent_step(db, agent_run_id, step)
                    continue
                if len(search_results) >= 8:
                    break

            if not search_results:
                if db is not None:
                    _append_agent_step(
                        db,
                        agent_run_id,
                        {
                            "kind": "tool",
                            "action": "wiki_no_results",
                            "tool": "wiki_search",
                            "tool_name": "wiki_search",
                            "api_url": _WIKIPEDIA_API_URL,
                            "queries": search_queries,
                        },
                    )
                return []

            out: list[dict[str, Any]] = []
            read_limit = max(1, min(5, int(max_pages)))
            for item in search_results[:read_limit]:
                pageid = item.get("pageid")
                title = str(item.get("title") or "").strip()
                page: dict[str, Any] = {
                    "title": title,
                    "url": str(item.get("url") or "").strip(),
                    "snippet": str(item.get("snippet") or "").strip()[:800],
                    "source": _WIKIPEDIA_SOURCE_NAME,
                    "tool": "wiki",
                }
                started = time.perf_counter()
                params = {
                    "action": "query",
                    "prop": "extracts",
                    "exintro": "1",
                    "explaintext": "1",
                    "pageids": str(pageid),
                    "format": "json",
                    "formatversion": "2",
                }
                try:
                    resp = client.get(_WIKIPEDIA_API_URL, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                    pages = data.get("query", {}).get("pages", []) if isinstance(data, dict) else []
                    wiki_page = pages[0] if isinstance(pages, list) and pages and isinstance(pages[0], dict) else {}
                    extract = _collapse_text(str(wiki_page.get("extract") or ""), limit=5000)
                    if extract:
                        page["content"] = extract
                    if wiki_page.get("title"):
                        page["title"] = str(wiki_page.get("title") or title)
                        page["url"] = _wiki_page_url(_WIKIPEDIA_API_URL, page["title"])
                    step = ToolResult(
                        spec=_WIKI_READ_TOOL_SPEC,
                        input={"pageid": int(pageid), "api_url": _WIKIPEDIA_API_URL, "title": title},
                        output={"title": page["title"], "chars": len(extract), "excerpt": extract[:360]},
                        ok=True,
                        duration_ms=_duration_ms(started),
                    ).to_step(action="wiki_read")
                    step.update({"pageid": pageid, "title": page["title"], "url": page["url"], "chars": len(extract), "excerpt": extract[:360]})
                    if db is not None:
                        _append_agent_step(db, agent_run_id, step)
                except Exception as e:
                    page["fetch_error"] = str(e)[:300]
                    step = ToolResult(
                        spec=_WIKI_READ_TOOL_SPEC,
                        input={"pageid": pageid, "api_url": _WIKIPEDIA_API_URL, "title": title},
                        output={"title": title, "chars": 0, "excerpt": ""},
                        ok=False,
                        duration_ms=_duration_ms(started),
                        error_type=type(e).__name__,
                        error=str(e)[:300],
                    ).to_step(action="wiki_read_failed")
                    step.update({"pageid": pageid, "title": title, "url": page["url"]})
                    if db is not None:
                        _append_agent_step(db, agent_run_id, step)
                if page.get("snippet") or page.get("content"):
                    out.append(page)
            return out
    except Exception:
        if db is not None:
            _append_agent_step(
                db,
                agent_run_id,
                {
                    "kind": "tool",
                    "action": "wiki_failed",
                    "tool": "wiki_search",
                    "tool_name": "wiki_search",
                    "api_url": _WIKIPEDIA_API_URL,
                    "queries": search_queries,
                    "error_type": "unexpected_error",
                },
            )
        return []


def fetch_url_evidence(
    *,
    url: str,
    title: str = "",
    timeout_seconds: float = 20.0,
    db: Session | None = None,
    agent_run_id: str | None = None,
) -> dict[str, Any] | None:
    clean_url = str(url or "").strip()
    if not clean_url or not _is_fetchable_url(clean_url):
        return None
    started = time.perf_counter()
    try:
        with _PublicFetchClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": "VideoRoll-RAG-Agent/1.0", "Accept": "text/html, text/plain;q=0.9,*/*;q=0.8"},
        ) as client:
            resp = _safe_public_get(client, clean_url)
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            body = resp.text[:500_000]
            if "html" in ctype or "<html" in body[:1000].lower():
                page_text = _extract_page_text(body)
            else:
                page_text = _collapse_text(body, limit=12000)
        page = {
            "title": str(title or clean_url).strip(),
            "url": clean_url,
            "snippet": page_text[:800],
            "content": page_text[:5000],
            "tool": "fetch",
        }
        step = ToolResult(
            spec=_FETCH_TOOL_SPEC,
            input={"url": clean_url, "title": title},
            output={"url": clean_url, "chars": len(page_text), "excerpt": page_text[:360]},
            ok=True,
            duration_ms=_duration_ms(started),
        ).to_step(action="fetch_url")
        step.update({"url": clean_url, "title": page["title"], "chars": len(page_text), "excerpt": page_text[:360]})
        if db is not None:
            _append_agent_step(db, agent_run_id, step)
        return page
    except Exception as e:
        step = ToolResult(
            spec=_FETCH_TOOL_SPEC,
            input={"url": clean_url, "title": title},
            output={"url": clean_url},
            ok=False,
            duration_ms=_duration_ms(started),
            error_type=type(e).__name__,
            error=str(e)[:300],
        ).to_step(action="fetch_url_failed")
        step.update({"url": clean_url, "title": title})
        if db is not None:
            _append_agent_step(db, agent_run_id, step)
        return None


def research_agent_next_action_openai(
    *,
    term: str,
    context: str,
    target_lang: str,
    domain_hint: str,
    available_tools: list[str],
    available_tool_specs: list[dict[str, Any]] | None = None,
    active_skills: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    config: OpenAIChatConfig,
    step_no: int,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    compact_observations = observations[-8:]
    compact_evidence = [
        {
            "title": str(item.get("title") or "")[:180],
            "url": str(item.get("url") or "")[:500],
            "snippet": str(item.get("snippet") or "")[:500],
            "has_content": bool(str(item.get("content") or "").strip()),
            "tool": str(item.get("tool") or ""),
        }
        for item in evidence[-8:]
        if isinstance(item, dict)
    ]
    data = request_openai_json_object(
        config=config,
        system_prompt="You are a tool-using translation research sub-agent. Return ONLY valid JSON.",
        user_prompt=(
            "你是一个字幕翻译术语研究子 Agent。你需要像 coding agent 一样根据已有 observation 自己决定下一步调用哪个工具。\n"
            "目标：找到术语在当前字幕上下文中最贴切的含义和中文译法，并收集足够证据。\n\n"
            "可用工具：\n"
            "- rag_lookup: 查询本地知识库是否已有该术语。输入 term。\n"
            "- dictionary_lookup: 查询已导入词典/术语库。输入 term，可选 source_lang/target_lang。\n"
            "- wiki_search: 查询 English Wikipedia。输入 query。\n"
            "- search_web: 调用配置的 SearXNG 搜索。输入 query。\n"
            "- fetch_url: 打开一个 URL 抽取正文。输入 url。\n"
            "- finish: 认为证据足够或无需继续。\n\n"
            "可用 Skill：\n"
            "- Skill 是可运行的能力包：它会给你额外 instructions/resources，并可能限制推荐工具。\n"
            "- 如果某一步是按某个 Skill 执行，请在输出里填写 skill_name。\n\n"
            "决策要求：\n"
            "- 不要一开始机械调用所有工具；根据 observation 判断下一步。\n"
            "- 如果是普通词义或导入术语表可能覆盖的固定译法，优先 dictionary_lookup。\n"
            "- Wikipedia 不足、无中文译名支撑、或上下文不一致时，应继续 search_web 或 fetch_url。\n"
            "- 如果本地知识库已命中，通常 finish。\n"
            "- 如果证据明显和字幕无关，也可以 finish 并说明无法入库。\n"
            "- 只输出 JSON，不要解释性文本。\n\n"
            f"step_no: {step_no}\n"
            f"术语: {term}\n"
            f"目标语言: {target_lang or 'zh'}\n"
            f"领域提示: {domain_hint or '未知'}\n"
            f"可用工具: {json.dumps(available_tools, ensure_ascii=False)}\n\n"
            f"工具 schema JSON:\n{json.dumps(available_tool_specs or [], ensure_ascii=False)[:5000]}\n\n"
            f"active skills JSON:\n{json.dumps(active_skills or [], ensure_ascii=False)[:7000]}\n\n"
            f"字幕上下文:\n{context[:2600]}\n\n"
            f"observations JSON:\n{json.dumps(compact_observations, ensure_ascii=False)[:5000]}\n\n"
            f"evidence JSON:\n{json.dumps(compact_evidence, ensure_ascii=False)[:5000]}\n\n"
            '输出 JSON：{"action":"rag_lookup|dictionary_lookup|wiki_search|search_web|fetch_url|finish",'
            '"query":"","url":"","skill_name":"","reason":"","final_answer_ready":false}'
        ),
        client=client,
    )
    action = str(data.get("action") or "").strip()
    if action not in {"rag_lookup", "dictionary_lookup", "wiki_search", "search_web", "fetch_url", "finish"}:
        action = "finish"
    decision = validate_model(
        AgentDecision,
        {
            "action": action,
            "query": str(data.get("query") or "").strip()[:240],
            "url": str(data.get("url") or "").strip()[:1000],
            "skill_name": str(data.get("skill_name") or "").strip()[:120],
            "reason": str(data.get("reason") or "").strip()[:1000],
            "final_answer_ready": _json_bool(data.get("final_answer_ready")),
        },
        fallback=AgentDecision(action="finish", reason="invalid tool decision output"),
    )
    return decision.model_dump()


def _dedupe_evidence(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        key = url or title.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _collect_evidence_with_tool_agent(
    db: Session,
    *,
    agent_run_id: str | None,
    term: str,
    domain_hint: str,
    target_lang: str,
    rag_settings: RagSettings,
    chat_config: OpenAIChatConfig,
    llm_context: str,
    search_queries: list[str],
    active_skills: list[AgentSkill] | None = None,
    max_steps: int = 6,
) -> tuple[list[dict[str, Any]], list[str], int]:
    active_skills = active_skills or []
    tool_registry = _research_tool_registry(rag_settings)
    available_tool_specs, available_tools = _tool_specs_for_active_skills(tool_registry, active_skills)
    active_skill_payloads = _active_skill_payloads(active_skills)

    observations: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    tools_used: list[str] = []
    used_actions: set[str] = set()
    empty_search_count = 0
    rounds = 0
    runtime = AgentRuntime(
        agent_name="rag_term_research",
        run_id=agent_run_id,
        budget=_agent_budget_for_rag(rag_settings, max_steps=max_steps),
        trace_recorder=(lambda step: _append_agent_step(db, agent_run_id, step)) if db is not None else None,
    )
    runtime.record(
        AgentTraceEvent(
            kind="agent",
            action="agent_runtime_start",
            output={
                "available_tools": available_tools,
                "tool_specs": available_tool_specs,
                "active_skills": [skill.summary() for skill in active_skills],
                "budget": runtime.budget.model_dump(),
            },
        )
    )
    for skill in active_skills:
        runtime.record(
            AgentTraceEvent(
                kind="agent",
                action="skill_activated",
                output=skill.summary(),
            )
        )
    runtime.record(
        AgentTraceEvent(
            kind="agent",
            action="state_transition",
            output={"from_node": "start", "to_node": "decide_tool", "reason": "child agent initialized"},
        )
    )

    for step_no in range(1, max(1, min(10, int(max_steps))) + 1):
        rounds = step_no
        try:
            runtime.before_llm()
            started = time.perf_counter()
            decision = research_agent_next_action_openai(
                term=term,
                context=llm_context,
                target_lang=target_lang,
                domain_hint=domain_hint,
                available_tools=available_tools,
                available_tool_specs=available_tool_specs,
                active_skills=active_skill_payloads,
                observations=observations,
                evidence=evidence,
                config=chat_config,
                step_no=step_no,
            )
            _append_llm_step(
                db,
                agent_run_id,
                action="agent_tool_decision",
                config=chat_config,
                input_value={
                    "term": term,
                    "step_no": step_no,
                    "available_tools": available_tools,
                    "active_skills": [skill.name for skill in active_skills],
                    "observation_count": len(observations),
                    "evidence_count": len(evidence),
                },
                output_value=decision,
                duration_ms=_duration_ms(started),
            )
        except AgentBudgetExceeded as e:
            observations.append({"action": "finish", "reason": str(e), "evidence_count": len(evidence)})
            runtime.record(
                AgentTraceEvent(
                    kind="policy",
                    action="agent_budget_exceeded",
                    status="failed",
                    error_type=type(e).__name__,
                    error=str(e),
                    output={"evidence_count": len(evidence), "round": step_no},
                )
            )
            break
        except Exception as e:
            decision = {"action": "finish", "query": "", "url": "", "skill_name": "", "reason": f"tool decision failed: {e}", "final_answer_ready": False}
            _append_llm_step(
                db,
                agent_run_id,
                action="agent_tool_decision_failed",
                config=chat_config,
                error=str(e)[:300],
                error_type=type(e).__name__,
            )

        action = str(decision.get("action") or "finish")
        reason = str(decision.get("reason") or "")
        skill_name = str(decision.get("skill_name") or "").strip()
        if action == "finish":
            observations.append({"action": "finish", "reason": reason, "skill_name": skill_name, "evidence_count": len(evidence)})
            _append_agent_step(
                db,
                agent_run_id,
                {"kind": "agent", "action": "agent_finish_decision", "skill_name": skill_name, "reason": reason, "evidence_count": len(evidence)},
            )
            runtime.record(
                AgentTraceEvent(
                    kind="agent",
                    action="state_transition",
                    output={"from_node": "decide_tool", "to_node": "finish", "reason": reason},
                )
            )
            break

        if action == "rag_lookup":
            try:
                runtime.before_tool("rag_lookup")
            except AgentBudgetExceeded as e:
                runtime.record(
                    AgentTraceEvent(
                        kind="policy",
                        action="agent_budget_exceeded",
                        status="failed",
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                )
                break
            tools_used.append("rag_lookup")
            used_actions.add(action)
            norm_value = normalize_term(term)
            exists = norm_value in existing_term_norms(db, terms=[term], target_lang=target_lang)
            observations.append({"action": "rag_lookup", "term": term, "skill_name": skill_name, "exists": exists, "normalized_term": norm_value})
            _append_agent_step(
                db,
                agent_run_id,
                {"kind": "tool", "action": "rag_lookup", "tool": "rag_lookup", "skill_name": skill_name, "term": term, "exists": exists, "ok": True},
            )
            if exists:
                runtime.record(
                    AgentTraceEvent(
                        kind="agent",
                        action="state_transition",
                        output={"from_node": "rag_lookup", "to_node": "finish", "reason": "local knowledge already exists"},
                    )
                )
                break
            continue

        if action == "dictionary_lookup" and rag_settings.dictionary_enabled:
            try:
                runtime.before_tool("dictionary_lookup")
            except AgentBudgetExceeded as e:
                runtime.record(
                    AgentTraceEvent(
                        kind="policy",
                        action="agent_budget_exceeded",
                        status="failed",
                        error_type=type(e).__name__,
                        error=str(e),
                    )
                )
                break
            tools_used.append("dictionary")
            used_actions.add(action)
            try:
                dictionary_hits = lookup_dictionary_entries(
                    db,
                    term=term,
                    source_lang="",
                    target_lang=target_lang,
                    domain=domain_hint,
                    limit=rag_settings.dictionary_top_k,
                    min_quality=rag_settings.dictionary_min_quality,
                    exact=True,
                )
            except Exception as e:
                db.rollback()
                dictionary_hits = []
                observations.append({"action": action, "term": term, "skill_name": skill_name, "error": str(e)[:300]})
            dictionary_evidence = dictionary_entries_to_evidence(dictionary_hits)
            evidence = _dedupe_evidence([*evidence, *dictionary_evidence])
            observations.append(
                {
                    "action": action,
                    "term": term,
                    "skill_name": skill_name,
                    "count": len(dictionary_hits),
                    "total_evidence": len(evidence),
                }
            )
            _append_agent_step(
                db,
                agent_run_id,
                {
                    "kind": "tool",
                    "action": "dictionary_lookup",
                    "tool": "dictionary_lookup",
                    "skill_name": skill_name,
                    "term": term,
                    "count": len(dictionary_hits),
                    "results": dictionary_hits[:8],
                    "ok": True,
                },
            )
            continue

        query = str(decision.get("query") or "").strip()
        if not query:
            query = (search_queries[0] if search_queries else " ".join([p for p in [domain_hint, term] if p]).strip()) or term

        if action == "wiki_search" and rag_settings.wiki_enabled:
            try:
                runtime.before_tool("wiki_search")
            except AgentBudgetExceeded as e:
                runtime.record(
                    AgentTraceEvent(kind="policy", action="agent_budget_exceeded", status="failed", error_type=type(e).__name__, error=str(e))
                )
                break
            tools_used.append("wikipedia")
            used_actions.add(action)
            extra = fetch_wikipedia_evidence(term, domain=domain_hint, queries=[query], db=db, agent_run_id=agent_run_id)
            evidence = _dedupe_evidence([*evidence, *extra])
            observations.append({"action": action, "query": query, "skill_name": skill_name, "count": len(extra), "total_evidence": len(evidence)})
            continue

        if action == "search_web" and rag_settings.search_enabled:
            try:
                runtime.before_tool("search")
            except AgentBudgetExceeded as e:
                runtime.record(
                    AgentTraceEvent(kind="policy", action="agent_budget_exceeded", status="failed", error_type=type(e).__name__, error=str(e))
                )
                break
            tools_used.append("search")
            used_actions.add(action)
            extra = fetch_search_evidence(
                term,
                domain=domain_hint,
                search_url=rag_settings.search_url,
                search_categories=rag_settings.search_categories,
                search_engines=rag_settings.search_engines,
                search_fallback_engines=rag_settings.search_fallback_engines,
                search_language=rag_settings.search_language,
                search_safesearch=rag_settings.search_safesearch,
                search_time_range=rag_settings.search_time_range,
                search_pageno=rag_settings.search_pageno,
                queries=[query],
                context=llm_context,
                target_lang=target_lang,
                config=chat_config,
                db=db,
                agent_run_id=agent_run_id,
            )
            evidence = _dedupe_evidence([*evidence, *extra])
            observations.append({"action": action, "query": query, "skill_name": skill_name, "count": len(extra), "total_evidence": len(evidence)})
            if not extra:
                empty_search_count += 1
                if empty_search_count >= 2:
                    observations.append({"action": "finish", "reason": "search_web returned no evidence twice", "evidence_count": len(evidence)})
                    _append_agent_step(
                        db,
                        agent_run_id,
                        {
                            "kind": "policy",
                            "action": "search_exhausted",
                            "tool": "search",
                            "reason": "search_web returned no usable evidence twice; stopping this child agent search loop.",
                            "empty_search_count": empty_search_count,
                            "evidence_count": len(evidence),
                        },
                    )
                    break
            else:
                empty_search_count = 0
            continue

        if action == "fetch_url":
            try:
                runtime.before_tool("fetch_url")
            except AgentBudgetExceeded as e:
                runtime.record(
                    AgentTraceEvent(kind="policy", action="agent_budget_exceeded", status="failed", error_type=type(e).__name__, error=str(e))
                )
                break
            tools_used.append("fetch")
            used_actions.add(action)
            url = str(decision.get("url") or "").strip()
            if not url:
                for item in reversed(evidence):
                    candidate = str(item.get("url") or "").strip()
                    if candidate and not str(item.get("content") or "").strip():
                        url = candidate
                        break
            page = fetch_url_evidence(url=url, db=db, agent_run_id=agent_run_id) if url else None
            if page:
                evidence = _dedupe_evidence([*evidence, page])
            observations.append({"action": action, "url": url, "skill_name": skill_name, "ok": bool(page), "total_evidence": len(evidence)})
            continue

        observations.append({"action": action, "skill_name": skill_name, "reason": f"tool unavailable or disabled: {action}"})

    # Safety fallback: if the model stopped too early, try enabled tools once.
    if not evidence:
        runtime.record(
            AgentTraceEvent(
                kind="policy",
                action="agent_evidence_fallback",
                output={"reason": "no evidence collected in tool loop", "used_actions": sorted(used_actions)},
            )
        )
        if rag_settings.dictionary_enabled and "dictionary_lookup" in available_tools and "dictionary_lookup" not in used_actions:
            tools_used.append("dictionary")
            try:
                dictionary_hits = lookup_dictionary_entries(
                    db,
                    term=term,
                    source_lang="",
                    target_lang=target_lang,
                    domain=domain_hint,
                    limit=rag_settings.dictionary_top_k,
                    min_quality=rag_settings.dictionary_min_quality,
                    exact=True,
                )
            except Exception:
                db.rollback()
                dictionary_hits = []
            evidence = _dedupe_evidence(dictionary_entries_to_evidence(dictionary_hits))
        if not evidence and rag_settings.wiki_enabled and "wiki_search" in available_tools and "wiki_search" not in used_actions:
            tools_used.append("wikipedia")
            evidence = _dedupe_evidence(fetch_wikipedia_evidence(term, domain=domain_hint, queries=search_queries, db=db, agent_run_id=agent_run_id))
        if not evidence and rag_settings.search_enabled and "search_web" in available_tools and "search_web" not in used_actions:
            tools_used.append("search")
            evidence = _dedupe_evidence(
                fetch_search_evidence(
                    term,
                    domain=domain_hint,
                    search_url=rag_settings.search_url,
                    search_categories=rag_settings.search_categories,
                    search_engines=rag_settings.search_engines,
                    search_fallback_engines=rag_settings.search_fallback_engines,
                    search_language=rag_settings.search_language,
                    search_safesearch=rag_settings.search_safesearch,
                    search_time_range=rag_settings.search_time_range,
                    search_pageno=rag_settings.search_pageno,
                    queries=search_queries,
                    context=llm_context,
                    target_lang=target_lang,
                    config=chat_config,
                    db=db,
                    agent_run_id=agent_run_id,
                )
            )
    return evidence, tools_used, rounds


def explain_term_from_evidence_openai(
    *,
    term: str,
    context: str,
    target_lang: str,
    domain_hint: str,
    evidence: list[dict[str, Any]],
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    clean_term = str(term or "").strip()
    if not clean_term:
        return None
    data = request_openai_json_object(
        config=config,
        system_prompt="You build a verified translation glossary. Return ONLY valid JSON.",
        user_prompt=(
            "请根据字幕上下文和检索资料，判断术语最贴切的中文译法。\n"
            "要求：\n"
            "- 不确定时 confidence 低于 0.7；\n"
            "- 检索资料可能包含搜索结果摘要、url、以及打开网页后抽取的 content；优先使用 content 和可靠来源；\n"
            "- 不要把无关网页、广告、导航文字当成术语依据；\n"
            "- sources 只保留实际支持判断的来源；\n"
            "- description 用中文说明语境；\n"
            "- translation 是字幕翻译可直接使用的译法。\n\n"
            f"术语：{clean_term}\n"
            f"目标语言：{target_lang or 'zh'}\n"
            f"领域提示：{domain_hint or '未知'}\n\n"
            f"字幕上下文：\n{context[:3000]}\n\n"
            f"检索资料 JSON：\n{json.dumps(evidence, ensure_ascii=False)[:6000]}\n\n"
            '输出 JSON：{"term":"","translation":"","domain":"","aliases":[],"description":"","sources":[],"confidence":0.0}'
        ),
        client=client,
    )
    translation = str(data.get("translation") or "").strip()
    if not translation:
        return None
    aliases = data.get("aliases")
    sources = data.get("sources")
    try:
        confidence = float(data.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    try:
        candidate = validate_model(
            GlossaryCandidate,
            {
                "term": str(data.get("term") or clean_term).strip(),
                "translation": translation,
                "domain": str(data.get("domain") or domain_hint or "").strip(),
                "aliases": aliases if isinstance(aliases, list) else [],
                "description": str(data.get("description") or "").strip(),
                "sources": sources if isinstance(sources, list) else [],
                "confidence": max(0.0, min(1.0, confidence)),
            },
        )
    except Exception:
        return None
    return candidate.model_dump()


def _json_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _valid_external_evidence(evidence: list[dict[str, Any]], *, search_url: str = "") -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        content = str(item.get("content") or "").strip()
        if not url or item.get("fetch_error"):
            continue
        if search_url and _is_search_engine_internal_url(url, search_url=search_url):
            continue
        if not snippet and not content:
            continue
        snippet_norm = re.sub(r"[\W_]+", " ", snippet.lower()).strip()
        if not content and (len(snippet) < 24 or snippet_norm in {"read more", "read more read more"}):
            continue
        out.append({"title": title, "url": url})
    return out


def verify_glossary_entry_openai(
    *,
    term: str,
    context: str,
    target_lang: str,
    domain_hint: str,
    evidence: list[dict[str, Any]],
    candidate: dict[str, Any],
    config: OpenAIChatConfig,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    clean_term = str(term or "").strip()
    compact_evidence = [
        {
            "title": str(item.get("title") or "")[:240],
            "url": str(item.get("url") or "")[:1000],
            "snippet": str(item.get("snippet") or "")[:700],
            "content": str(item.get("content") or "")[:1200],
            "fetch_error": str(item.get("fetch_error") or "")[:300],
        }
        for item in evidence[:8]
        if isinstance(item, dict)
    ]
    compact_candidate = {
        "term": str(candidate.get("term") or clean_term),
        "translation": str(candidate.get("translation") or ""),
        "domain": str(candidate.get("domain") or domain_hint or ""),
        "aliases": candidate.get("aliases") if isinstance(candidate.get("aliases"), list) else [],
        "description": str(candidate.get("description") or ""),
        "sources": candidate.get("sources") if isinstance(candidate.get("sources"), list) else [],
        "confidence": float(candidate.get("confidence") or 0.0),
    }
    data = request_openai_json_object(
        config=config,
        system_prompt="You verify a translation glossary candidate for a RAG knowledge base. Return ONLY valid JSON.",
        user_prompt=(
            "请作为独立 verifier，判断候选术语条目是否应该写入长期翻译知识库。\n"
            "要求：\n"
            "- 必须检查检索资料是否真正支持候选译法和描述；\n"
            "- 必须检查候选解释是否符合字幕上下文；\n"
            "- 搜索引擎 About/Preferences/导航页、广告页、无正文摘要不能作为有效来源；\n"
            "- 单字母变量、局部变量、一次性占位符不应该写入长期知识库；\n"
            "- 如果没有可靠外部来源，但译法只适合当前字幕，failure_category 使用 context_only，should_write=false；\n"
            "- should_auto_approve 只有在来源明确、上下文一致、置信度很高时才为 true。\n\n"
            f"术语：{clean_term}\n"
            f"目标语言：{target_lang or 'zh'}\n"
            f"领域提示：{domain_hint or '未知'}\n\n"
            f"字幕上下文：\n{context[:3000]}\n\n"
            f"候选条目 JSON：\n{json.dumps(compact_candidate, ensure_ascii=False)}\n\n"
            f"检索资料 JSON：\n{json.dumps(compact_evidence, ensure_ascii=False)[:7000]}\n\n"
            '输出 JSON：{"supported":true,"context_consistent":true,"should_write":true,'
            '"should_auto_approve":false,"confidence":0.0,"reason":"","failure_category":""}'
        ),
        client=client,
    )
    try:
        confidence = float(data.get("confidence") or 0.0)
    except Exception:
        confidence = 0.0
    verified = validate_model(
        VerificationResult,
        {
            "supported": _json_bool(data.get("supported")),
            "context_consistent": _json_bool(data.get("context_consistent")),
            "should_write": _json_bool(data.get("should_write")),
            "should_auto_approve": _json_bool(data.get("should_auto_approve")),
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(data.get("reason") or "").strip()[:1200],
            "failure_category": str(data.get("failure_category") or "").strip()[:120],
        },
    )
    return verified.model_dump()


def upsert_knowledge_item(
    db: Session,
    *,
    item_type: str,
    target_lang: str,
    term: str = "",
    translation: str = "",
    domain: str = "",
    aliases: list[str] | None = None,
    title: str = "",
    content: str = "",
    description: str = "",
    sources: list[dict[str, Any]] | None = None,
    confidence: float = 0.0,
    status: str = "approved",
    created_by: str = "manual",
    embedding: list[float] | None = None,
    embedding_model: str = "",
    dedupe_any_domain: bool = False,
) -> str:
    item_id = str(uuid.uuid4())
    clean_item_type = str(item_type or "document").strip() or "document"
    clean_target_lang = str(target_lang or "zh").strip() or "zh"
    clean_term = str(term or "").strip()
    clean_domain = str(domain or "").strip()
    norm = normalize_term(clean_term)
    alias_list = [str(x).strip() for x in aliases or [] if str(x or "").strip()]
    source_list = [x for x in sources or [] if isinstance(x, dict)]
    emb_literal = _vector_literal(embedding) if embedding else None
    embedding_text = build_knowledge_embedding_text(
        item_type=clean_item_type,
        term=clean_term,
        translation=translation,
        domain=clean_domain,
        aliases=alias_list,
        title=title,
        content=content,
        description=description,
    )
    embedding_hash = _hash_text(embedding_text) if embedding else ""

    if clean_item_type == "term" and norm:
        existing = db.execute(
            text(
                """
                SELECT id FROM translation_knowledge_items
                WHERE item_type = 'term'
                  AND target_lang = :target_lang
                  AND domain = :domain
                  AND normalized_term = :normalized_term
                LIMIT 1
                """
            ),
            {"target_lang": clean_target_lang, "domain": clean_domain, "normalized_term": norm},
        ).first()
        if not existing and dedupe_any_domain:
            existing = db.execute(
                text(
                    """
                    SELECT id FROM translation_knowledge_items
                    WHERE item_type = 'term'
                      AND target_lang = :target_lang
                      AND normalized_term = :normalized_term
                      AND status <> 'archived'
                    ORDER BY
                      CASE
                        WHEN domain = :domain THEN 0
                        WHEN domain = '' THEN 1
                        ELSE 2
                      END,
                      updated_at DESC
                    LIMIT 1
                    """
                ),
                {"target_lang": clean_target_lang, "domain": clean_domain, "normalized_term": norm},
            ).first()
        if existing:
            item_id = str(existing[0])
            db.execute(
                text(
                    """
                    UPDATE translation_knowledge_items
                    SET term = :term,
                        translation = :translation,
                        aliases = CAST(:aliases AS jsonb),
                        title = :title,
                        content = :content,
                        description = :description,
                        sources = CAST(:sources AS jsonb),
                        confidence = :confidence,
                        status = :status,
                        created_by = :created_by,
                        embedding = COALESCE(CAST(:embedding AS vector), embedding),
                        embedding_model = CASE WHEN :embedding_model <> '' THEN :embedding_model ELSE embedding_model END,
                        embedding_text_hash = CASE WHEN :embedding_hash <> '' THEN :embedding_hash ELSE embedding_text_hash END,
                        last_verified_at = :last_verified_at,
                        updated_at = now()
                    WHERE id = CAST(:id AS uuid)
                    """
                ),
                {
                    "id": item_id,
                    "term": clean_term,
                    "translation": str(translation or "").strip(),
                    "aliases": json.dumps(alias_list, ensure_ascii=False),
                    "title": str(title or "").strip(),
                    "content": str(content or "").strip(),
                    "description": str(description or "").strip(),
                    "sources": json.dumps(source_list, ensure_ascii=False),
                    "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
                    "status": str(status or "approved").strip() or "approved",
                    "created_by": str(created_by or "manual").strip() or "manual",
                    "embedding": emb_literal,
                    "embedding_model": str(embedding_model or "").strip(),
                    "embedding_hash": embedding_hash,
                    "last_verified_at": datetime.now(timezone.utc),
                },
            )
            return item_id

    db.execute(
        text(
            """
            INSERT INTO translation_knowledge_items (
                id, item_type, term, normalized_term, translation, target_lang, domain,
                aliases, title, content, description, sources, confidence, status,
                created_by, embedding, embedding_model, embedding_text_hash, last_verified_at
            )
            VALUES (
                CAST(:id AS uuid), :item_type, :term, :normalized_term, :translation, :target_lang, :domain,
                CAST(:aliases AS jsonb), :title, :content, :description, CAST(:sources AS jsonb),
                :confidence, :status, :created_by, CAST(:embedding AS vector), :embedding_model,
                :embedding_hash, :last_verified_at
            )
            """
        ),
        {
            "id": item_id,
            "item_type": clean_item_type,
            "term": clean_term,
            "normalized_term": norm,
            "translation": str(translation or "").strip(),
            "target_lang": clean_target_lang,
            "domain": clean_domain,
            "aliases": json.dumps(alias_list, ensure_ascii=False),
            "title": str(title or "").strip(),
            "content": str(content or "").strip(),
            "description": str(description or "").strip(),
            "sources": json.dumps(source_list, ensure_ascii=False),
            "confidence": max(0.0, min(1.0, float(confidence or 0.0))),
            "status": str(status or "approved").strip() or "approved",
            "created_by": str(created_by or "manual").strip() or "manual",
            "embedding": emb_literal,
            "embedding_model": str(embedding_model or "").strip(),
            "embedding_hash": embedding_hash,
            "last_verified_at": datetime.now(timezone.utc),
        },
    )
    return item_id


def search_knowledge(
    db: Session,
    *,
    query_embedding: list[float],
    target_lang: str,
    domain: str = "",
    embedding_model: str = "",
    top_k: int = 8,
    min_score: float = 0.68,
) -> list[RagHit]:
    if not query_embedding or top_k <= 0:
        return []
    params = {
        "embedding": _vector_literal(query_embedding),
        "dimensions": len(query_embedding),
        "target_lang": str(target_lang or "zh").strip() or "zh",
        "domain": str(domain or "").strip(),
        "embedding_model": str(embedding_model or "").strip(),
        "limit": max(1, min(30, int(top_k))),
        "min_score": max(0.0, min(1.0, float(min_score))),
    }
    rows = db.execute(
        text(
            """
            SELECT id, item_type, term, translation, target_lang, domain, aliases, title,
                   content, description, sources, confidence, status,
                   GREATEST(0, 1 - (embedding <=> CAST(:embedding AS vector))) AS score
            FROM translation_knowledge_items
            WHERE target_lang = :target_lang
              AND status IN ('approved', 'auto_approved')
              AND embedding IS NOT NULL
              AND vector_dims(embedding) = :dimensions
              AND (:embedding_model = '' OR embedding_model = :embedding_model)
              AND (:domain = '' OR domain = '' OR domain = :domain)
              AND GREATEST(0, 1 - (embedding <=> CAST(:embedding AS vector))) >= :min_score
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :limit
            """
        ),
        params,
    ).all()
    return [_row_to_hit(row) for row in rows]


def exact_term_hits(
    db: Session,
    *,
    text_value: str,
    target_lang: str,
    domain: str = "",
    limit: int = 20,
) -> list[RagHit]:
    candidates = _term_candidates_from_text(text_value, limit=limit)
    norms = [normalize_term(x) for x in candidates]
    if not norms:
        return []
    rows = db.execute(
        text(
            """
            SELECT id, item_type, term, translation, target_lang, domain, aliases, title,
                   content, description, sources, confidence, status, 1.0 AS score
            FROM translation_knowledge_items
            WHERE item_type = 'term'
              AND target_lang = :target_lang
              AND status IN ('approved', 'auto_approved')
              AND normalized_term = ANY(:norms)
              AND (:domain = '' OR domain = '' OR domain = :domain)
            LIMIT :limit
            """
        ),
        {
            "target_lang": str(target_lang or "zh").strip() or "zh",
            "domain": str(domain or "").strip(),
            "norms": norms,
            "limit": max(1, min(50, int(limit))),
        },
    ).all()
    return [_row_to_hit(row) for row in rows]


def existing_term_norms(
    db: Session,
    *,
    terms: Iterable[str],
    target_lang: str,
    include_archived: bool = False,
    limit: int = 100,
) -> set[str]:
    norms: list[str] = []
    seen: set[str] = set()
    for term in terms:
        norm = normalize_term(str(term or ""))
        if not norm or norm in seen:
            continue
        seen.add(norm)
        norms.append(norm)
    if not norms:
        return set()

    clauses = [
        "item_type = 'term'",
        "target_lang = :target_lang",
        "normalized_term = ANY(:norms)",
    ]
    if not include_archived:
        clauses.append("status <> 'archived'")
    try:
        rows = db.execute(
            text(
                f"""
                SELECT DISTINCT normalized_term
                FROM translation_knowledge_items
                WHERE {' AND '.join(clauses)}
                LIMIT :limit
                """
            ),
            {
                "target_lang": str(target_lang or "zh").strip() or "zh",
                "norms": norms,
                "limit": max(1, min(500, int(limit))),
            },
        ).all()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
        return set()

    out: set[str] = set()
    for row in rows:
        mapping = getattr(row, "_mapping", None)
        if mapping is not None:
            value = mapping.get("normalized_term")
        else:
            try:
                value = row[0]
            except Exception:
                value = None
        norm = normalize_term(str(value or ""))
        if norm:
            out.add(norm)
    return out


def _research_discovered_term(
    db: Session,
    *,
    item: dict[str, Any],
    target_lang: str,
    rag_settings: RagSettings,
    embedding_settings: EmbeddingSettings,
    chat_config: OpenAIChatConfig,
    text_value: str,
    llm_context: str,
    previous_summary: str,
    existing_term_cards: list[dict[str, Any]],
    gate_duration_ms: int | None,
    skill_registry: SkillRegistry | None = None,
    parent_agent_run_id: str | None = None,
    task_id: str | None = None,
    subtitle_job_id: str | None = None,
) -> AgentResearchResult | None:
    term = str(item.get("term") or "").strip()
    norm = normalize_term(term)
    if not term or not norm:
        return None

    domain_hint = str(item.get("domain") or rag_settings.domain or "").strip()
    query = " ".join([p for p in [domain_hint, term] if p]).strip()
    agent_run_id: str | None = None
    try:
        agent_run_id = _start_agent_run(
            db,
            term=term,
            domain=domain_hint,
            target_lang=target_lang,
            query=query,
            agent_type="rag_term_research",
            parent_agent_run_id=parent_agent_run_id,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
        )
        _append_llm_step(
            db,
            agent_run_id,
            action="pretranslation_rag_gate",
            config=chat_config,
            input_value={
                "domain": rag_settings.domain,
                "context_excerpt": text_value[:500],
                "previous_summary": str(previous_summary or "").strip()[:500],
                "existing_hit_count": len(existing_term_cards),
            },
            output_value={
                "term": term,
                "domain": domain_hint,
                "category": str(item.get("category") or ""),
                "need_rag": bool(item.get("need_rag")),
                "need_search": bool(item.get("need_search")),
                "scope": str(item.get("scope") or ""),
                "priority": float(item.get("priority") or 0.0),
                "reason": str(item.get("reason") or "").strip(),
            },
            duration_ms=gate_duration_ms,
        )
        _append_state_transition(
            db,
            agent_run_id,
            from_node="start",
            to_node="policy",
            reason="child agent received gate item",
            metadata={"term": term, "category": str(item.get("category") or "")},
        )
    except Exception:
        db.rollback()
        agent_run_id = None

    research_policy = should_research_term(term, domain=domain_hint, context=text_value, gate_item=item)
    active_skills: list[AgentSkill] = []
    if rag_settings.agent_skills_enabled:
        try:
            registry = skill_registry or load_agent_skill_registry(rag_settings)
            active_skills = registry.select(term=term, domain=domain_hint or rag_settings.domain, context=llm_context, limit=4)
        except Exception:
            active_skills = []
    if active_skills:
        _append_agent_step(
            db,
            agent_run_id,
            {
                "kind": "agent",
                "action": "skills_selected",
                "term": term,
                "skills": [skill.summary() for skill in active_skills],
            },
        )
    _append_agent_step(
        db,
        agent_run_id,
        {
            "kind": "policy",
            "action": "term_research_policy",
            "term": term,
            "domain": domain_hint,
            "decision": research_policy,
        },
    )
    if not research_policy.get("should_research"):
        _append_state_transition(
            db,
            agent_run_id,
            from_node="policy",
            to_node="skipped",
            reason=str(research_policy.get("reason") or "policy skipped research"),
            metadata={"category": str(research_policy.get("category") or "")},
        )
        context_card: dict[str, Any] | None = None
        if research_policy.get("category") == "context_only" and research_policy.get("translation"):
            context_card = _context_only_term_card(term, research_policy, target_lang=target_lang, domain=domain_hint)
        elif research_policy.get("scope") in {"task", "series"} or item.get("need_rag"):
            hint = str(item.get("translation") or item.get("translation_hint") or "").strip()
            if hint:
                context_card = {
                    "term": term,
                    "translation": hint,
                    "domain": domain_hint,
                    "aliases": [],
                    "description": str(item.get("reason") or research_policy.get("reason") or "").strip(),
                    "confidence": float(item.get("priority") or 0.0) or 0.7,
                    "score": float(item.get("priority") or 0.0) or 0.7,
                    "sources": [],
                    "target_lang": target_lang,
                    "status": str(research_policy.get("scope") or "task_context"),
                }
        _finish_agent_run(
            db,
            agent_run_id,
            status="skipped",
            result={
                "term": term,
                "domain": domain_hint,
                "knowledge_status": str(research_policy.get("category") or "skipped"),
                "failure_category": str(research_policy.get("category") or "skipped"),
                "reason": str(research_policy.get("reason") or ""),
            },
        )
        return AgentResearchResult(term=term, normalized_term=norm, context_card=context_card)

    if not bool(research_policy.get("need_search", True)):
        _append_state_transition(
            db,
            agent_run_id,
            from_node="policy",
            to_node="skipped",
            reason=str(research_policy.get("reason") or "external lookup was not requested"),
            metadata={"category": str(research_policy.get("category") or "")},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="skipped",
            result={
                "term": term,
                "domain": domain_hint,
                "knowledge_status": "local_rag_only",
                "failure_category": "no_external_search_requested",
                "reason": str(research_policy.get("reason") or ""),
            },
        )
        return AgentResearchResult(term=term, normalized_term=norm)

    if norm in existing_term_norms(db, terms=[term], target_lang=target_lang):
        _append_state_transition(
            db,
            agent_run_id,
            from_node="policy",
            to_node="skipped",
            reason="term already exists in knowledge base",
            metadata={"normalized_term": norm},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="skipped",
            result={
                "term": term,
                "domain": domain_hint,
                "knowledge_status": "already_exists",
                "failure_category": "existing_knowledge",
                "reason": "term already exists in the knowledge base; skipping external research",
            },
        )
        return AgentResearchResult(term=term, normalized_term=norm)

    external_lookup_enabled = bool(rag_settings.wiki_enabled or rag_settings.search_enabled)
    search_queries: list[str] = []
    if external_lookup_enabled:
        _append_state_transition(
            db,
            agent_run_id,
            from_node="policy",
            to_node="query_planning",
            reason="external lookup enabled",
            metadata={"wiki": bool(rag_settings.wiki_enabled), "search": bool(rag_settings.search_enabled)},
        )
        started = time.perf_counter()
        try:
            search_queries = generate_search_queries_openai(
                term=term,
                context=llm_context,
                target_lang=target_lang,
                domain_hint=domain_hint,
                config=chat_config,
                max_queries=3,
            )
            _append_llm_step(
                db,
                agent_run_id,
                action="generate_search_queries",
                config=chat_config,
                input_value={
                    "term": term,
                    "domain": domain_hint,
                    "context_excerpt": text_value[:500],
                    "previous_summary": str(previous_summary or "").strip()[:500],
                },
                output_value={"queries": search_queries},
                duration_ms=_duration_ms(started),
            )
        except Exception as e:
            search_queries = _fallback_search_queries(term, domain=domain_hint, target_lang=target_lang, limit=3)
            _append_llm_step(
                db,
                agent_run_id,
                action="generate_search_queries_failed",
                config=chat_config,
                duration_ms=_duration_ms(started),
                error=str(e)[:300],
                error_type=type(e).__name__,
            )
            _append_agent_step(
                db,
                agent_run_id,
                {
                    "kind": "policy",
                    "action": "search_query_fallback",
                    "queries": search_queries,
                    "reason": "LLM query generation failed; using deterministic fallback queries.",
                },
            )
    tools_used: list[str] = []

    def _fetch_wiki_evidence_round() -> list[dict[str, Any]]:
        tools_used.append("wikipedia")
        return fetch_wikipedia_evidence(
            term,
            domain=domain_hint,
            queries=search_queries,
            db=db,
            agent_run_id=agent_run_id,
        )

    def _fetch_search_evidence_round() -> list[dict[str, Any]]:
        tools_used.append("search")
        return fetch_search_evidence(
            term,
            domain=domain_hint,
            search_url=rag_settings.search_url,
            search_categories=rag_settings.search_categories,
            search_engines=rag_settings.search_engines,
            search_fallback_engines=rag_settings.search_fallback_engines,
            search_language=rag_settings.search_language,
            search_safesearch=rag_settings.search_safesearch,
            search_time_range=rag_settings.search_time_range,
            search_pageno=rag_settings.search_pageno,
            queries=search_queries,
            context=llm_context,
            target_lang=target_lang,
            config=chat_config,
            db=db,
            agent_run_id=agent_run_id,
        )

    def _fetch_fallback_evidence(reason: str) -> list[dict[str, Any]]:
        """Try the next enabled-but-unused evidence tool instead of giving up.

        Design: when one tool fails or its evidence is judged insufficient, the agent
        must fall back to the other enabled tools (e.g. wiki -> web search) before
        finishing the run.
        """
        if rag_settings.search_enabled and "search" not in tools_used:
            fallback_tool = "search"
            extra = _fetch_search_evidence_round()
        elif rag_settings.wiki_enabled and "wikipedia" not in tools_used:
            fallback_tool = "wikipedia"
            extra = _fetch_wiki_evidence_round()
        else:
            return []
        _append_agent_step(
            db,
            agent_run_id,
            {
                "kind": "policy",
                "action": "evidence_tool_fallback",
                "tool": fallback_tool,
                "reason": reason,
                "extra_evidence_count": len(extra),
            },
        )
        return extra

    evidence, agent_tools_used, evidence_rounds = _collect_evidence_with_tool_agent(
        db,
        agent_run_id=agent_run_id,
        term=term,
        domain_hint=domain_hint,
        target_lang=target_lang,
        rag_settings=rag_settings,
        chat_config=chat_config,
        llm_context=llm_context,
        search_queries=search_queries,
        active_skills=active_skills,
        max_steps=6,
    )
    tools_used.extend(agent_tools_used)
    _append_state_transition(
        db,
        agent_run_id,
        from_node="query_planning",
        to_node="summarize",
        reason="evidence collection finished",
        metadata={"evidence_count": len(evidence), "tools_used": tools_used, "rounds": evidence_rounds},
    )
    if not evidence:
        _append_state_transition(
            db,
            agent_run_id,
            from_node="summarize",
            to_node="failed",
            reason="no search evidence",
            metadata={"tools_used": tools_used},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="failed",
            result={
                "term": term,
                "domain": domain_hint,
                "search_queries": search_queries,
                "failure_category": "no_search_evidence",
                "tools": {
                    "wikipedia": bool(rag_settings.wiki_enabled),
                    "search": bool(rag_settings.search_enabled),
                },
                "tools_used": tools_used,
            },
            error="no search evidence",
        )
        return None

    explained: dict[str, Any] | None = None
    verification: dict[str, Any] = {
        "supported": False,
        "context_consistent": False,
        "should_write": False,
        "should_auto_approve": False,
        "confidence": 0.0,
        "reason": "verification did not run",
        "failure_category": "verify_not_run",
    }
    valid_sources: list[dict[str, str]] = []
    final_confidence = 0.0
    should_write = False
    max_evidence_rounds = max(2, evidence_rounds + 1)
    while True:
        summarized: dict[str, Any] | None = None
        try:
            summarize_started = time.perf_counter()
            summarized = explain_term_from_evidence_openai(
                term=term,
                context=llm_context,
                target_lang=target_lang,
                domain_hint=domain_hint,
                evidence=evidence,
                config=chat_config,
            )
            _append_llm_step(
                db,
                agent_run_id,
                action="summarize_evidence",
                config=chat_config,
                input_value={
                    "term": term,
                    "domain": domain_hint,
                    "evidence_count": len(evidence),
                    "evidence_round": evidence_rounds,
                },
                output_value={
                    "term": str((summarized or {}).get("term") or term),
                    "translation": str((summarized or {}).get("translation") or ""),
                    "confidence": float((summarized or {}).get("confidence") or 0.0),
                },
                duration_ms=_duration_ms(summarize_started),
            )
        except Exception as e:
            _append_llm_step(
                db,
                agent_run_id,
                action="summarize_failed",
                config=chat_config,
                error=str(e)[:300],
                error_type=type(e).__name__,
            )
            summarized = None
        if summarized is not None:
            explained = summarized
        elif explained is not None:
            # Summarizer failed on a fallback round; keep the previous candidate and stop retrying.
            break
        else:
            if evidence_rounds < max_evidence_rounds:
                extra = _fetch_fallback_evidence("summarizer produced no usable glossary entry; trying another evidence tool")
                if extra:
                    evidence = extra + evidence
                    evidence_rounds += 1
                    continue
            _finish_agent_run(
                db,
                agent_run_id,
                status="failed",
                result={
                    "term": term,
                    "domain": domain_hint,
                    "evidence_count": len(evidence),
                    "failure_category": "summarize_failed",
                    "tools_used": tools_used,
                    "evidence_rounds": evidence_rounds,
                },
                error="LLM did not produce a usable glossary entry",
            )
            return None

        promotion_evidence = evidence if rag_settings.dictionary_auto_promote else [item for item in evidence if item.get("tool") != "dictionary_lookup"]
        valid_sources = _valid_external_evidence(promotion_evidence, search_url=rag_settings.search_url)
        _append_state_transition(
            db,
            agent_run_id,
            from_node="summarize",
            to_node="verify",
            reason="candidate summary available",
            metadata={"evidence_round": evidence_rounds, "valid_source_count": len(valid_sources)},
        )
        try:
            verify_started = time.perf_counter()
            verification = verify_glossary_entry_openai(
                term=term,
                context=llm_context,
                target_lang=target_lang,
                domain_hint=domain_hint,
                evidence=evidence,
                candidate=explained,
                config=chat_config,
            )
            _append_llm_step(
                db,
                agent_run_id,
                action="verify_glossary_entry",
                config=chat_config,
                input_value={
                    "term": term,
                    "candidate_confidence": float(explained.get("confidence") or 0.0),
                    "valid_source_count": len(valid_sources),
                    "evidence_round": evidence_rounds,
                },
                output_value=verification,
                duration_ms=_duration_ms(verify_started),
            )
        except Exception as e:
            verification = {
                "supported": False,
                "context_consistent": False,
                "should_write": False,
                "should_auto_approve": False,
                "confidence": 0.0,
                "reason": "verifier failed",
                "failure_category": "verify_failed",
            }
            _append_llm_step(
                db,
                agent_run_id,
                action="verify_failed",
                config=chat_config,
                error=str(e)[:300],
                error_type=type(e).__name__,
            )

        candidate_confidence = float(explained.get("confidence") or 0.0)
        verifier_confidence = float(verification.get("confidence") or 0.0)
        final_confidence = min(candidate_confidence, verifier_confidence) if verifier_confidence > 0 else candidate_confidence
        should_write = (
            bool(verification.get("supported"))
            and bool(verification.get("context_consistent"))
            and bool(verification.get("should_write"))
            and bool(valid_sources)
        )
        if should_write:
            _append_state_transition(
                db,
                agent_run_id,
                from_node="verify",
                to_node="write_rag",
                reason="verifier approved knowledge write",
                metadata={"confidence": final_confidence, "valid_source_count": len(valid_sources)},
            )
            break
        # Before finishing a rejected candidate, try another enabled evidence tool.
        # context_only is a semantic rejection rather than an evidence gap.
        failure_category = str(verification.get("failure_category") or "").strip()
        should_try_more_evidence = failure_category != "context_only"
        if should_try_more_evidence and evidence_rounds < max_evidence_rounds:
            extra = _fetch_fallback_evidence(
                "verifier rejected the candidate "
                f"(failure_category={failure_category or 'unknown'}); trying another evidence tool"
            )
            if extra:
                evidence = extra + evidence
                evidence_rounds += 1
                continue
        break

    if explained is None:
        return None
    if not should_write:
        failure_category = str(verification.get("failure_category") or "")
        if not valid_sources:
            failure_category = failure_category or "no_valid_external_source"
        _append_state_transition(
            db,
            agent_run_id,
            from_node="verify",
            to_node="skipped",
            reason=str(verification.get("reason") or "verifier rejected glossary entry"),
            metadata={"failure_category": failure_category or "verify_rejected", "confidence": final_confidence},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="skipped",
            result={
                "term": str(explained.get("term") or term),
                "translation": str(explained.get("translation") or ""),
                "domain": str(explained.get("domain") or domain_hint or ""),
                "confidence": final_confidence,
                "knowledge_status": "context_only" if failure_category == "context_only" else "not_written",
                "failure_category": failure_category or "verify_rejected",
                "verification": verification,
                "valid_source_count": len(valid_sources),
                "search_queries": search_queries,
                "tools_used": tools_used,
                "evidence_rounds": evidence_rounds,
            },
            error=str(verification.get("reason") or "verifier rejected glossary entry")[:4000],
        )
        return AgentResearchResult(term=term, normalized_term=norm)

    status = (
        "auto_approved"
        if bool(verification.get("should_auto_approve")) and final_confidence >= _AUTO_APPROVE_CONFIDENCE_THRESHOLD
        else "pending"
    )
    source_list = [x for x in explained.get("sources") or [] if isinstance(x, dict)] or valid_sources
    try:
        _append_state_transition(
            db,
            agent_run_id,
            from_node="write_rag",
            to_node="embedding",
            reason="embedding verified glossary entry",
            metadata={"status": status, "confidence": final_confidence},
        )
        _append_agent_step(
            db,
            agent_run_id,
            {
                "kind": "policy",
                "action": "knowledge_write_decision",
                "term": str(explained.get("term") or term),
                "translation": str(explained.get("translation") or ""),
                "confidence": final_confidence,
                "status": status,
                "auto_threshold": _AUTO_APPROVE_CONFIDENCE_THRESHOLD,
                "valid_source_count": len(valid_sources),
            },
        )
        emb_text = build_knowledge_embedding_text(
            item_type="term",
            term=str(explained.get("term") or term),
            translation=str(explained.get("translation") or ""),
            domain=str(explained.get("domain") or ""),
            aliases=[str(x) for x in explained.get("aliases") or []],
            description=str(explained.get("description") or ""),
        )
        emb = embed_text(emb_text, settings=embedding_settings)
        assert_embedding_dimensions(emb, rag_settings.embedding_dimensions)
        learned_id = upsert_knowledge_item(
            db,
            item_type="term",
            target_lang=target_lang,
            term=str(explained.get("term") or term),
            translation=str(explained.get("translation") or ""),
            domain=str(explained.get("domain") or rag_settings.domain or ""),
            aliases=[str(x) for x in explained.get("aliases") or []],
            description=str(explained.get("description") or ""),
            sources=source_list,
            confidence=final_confidence,
            status=status,
            created_by="agent",
            embedding=emb,
            embedding_model=embedding_model_key(rag_settings),
            dedupe_any_domain=True,
        )
        db.commit()
        _append_state_transition(
            db,
            agent_run_id,
            from_node="embedding",
            to_node="succeeded",
            reason="knowledge item saved",
            metadata={"knowledge_item_id": learned_id, "status": status},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="succeeded",
            result={
                "term": str(explained.get("term") or term),
                "translation": str(explained.get("translation") or ""),
                "domain": str(explained.get("domain") or rag_settings.domain or ""),
                "confidence": final_confidence,
                "knowledge_status": status,
                "verification": verification,
                "valid_source_count": len(valid_sources),
                "search_queries": search_queries,
                "sources": source_list[:5],
            },
            knowledge_item_id=learned_id,
        )
        if status == "auto_approved":
            return AgentResearchResult(
                term=term,
                normalized_term=norm,
                hit=RagHit(
                    id=learned_id,
                    item_type="term",
                    term=str(explained.get("term") or term),
                    translation=str(explained.get("translation") or ""),
                    target_lang=target_lang,
                    domain=str(explained.get("domain") or rag_settings.domain or ""),
                    aliases=[str(x) for x in explained.get("aliases") or []],
                    title="",
                    content="",
                    description=str(explained.get("description") or ""),
                    sources=source_list,
                    confidence=final_confidence,
                    status=status,
                    score=final_confidence,
                ),
            )
        return AgentResearchResult(term=term, normalized_term=norm)
    except Exception as e:
        db.rollback()
        _append_state_transition(
            db,
            agent_run_id,
            from_node="embedding",
            to_node="failed",
            reason="knowledge save failed",
            metadata={"error_type": type(e).__name__},
        )
        _finish_agent_run(
            db,
            agent_run_id,
            status="failed",
            result={
                "term": str(explained.get("term") or term),
                "translation": str(explained.get("translation") or ""),
                "confidence": final_confidence,
                "failure_category": "knowledge_save_failed",
            },
            error=f"knowledge save failed: {e}",
        )
        return None


def _run_research_agent(
    *,
    db: Session,
    session_factory: Callable[[], Session] | None,
    item: dict[str, Any],
    target_lang: str,
    rag_settings: RagSettings,
    embedding_settings: EmbeddingSettings,
    chat_config: OpenAIChatConfig,
    text_value: str,
    llm_context: str,
    previous_summary: str,
    existing_term_cards: list[dict[str, Any]],
    gate_duration_ms: int | None,
    skill_registry: SkillRegistry | None = None,
    parent_agent_run_id: str | None = None,
    task_id: str | None = None,
    subtitle_job_id: str | None = None,
) -> AgentResearchResult | None:
    if session_factory is None:
        return _research_discovered_term(
            db,
            item=item,
            target_lang=target_lang,
            rag_settings=rag_settings,
            embedding_settings=embedding_settings,
            chat_config=chat_config,
            text_value=text_value,
            llm_context=llm_context,
            previous_summary=previous_summary,
            existing_term_cards=existing_term_cards,
            gate_duration_ms=gate_duration_ms,
            skill_registry=skill_registry,
            parent_agent_run_id=parent_agent_run_id,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
        )
    worker_db = session_factory()
    try:
        return _research_discovered_term(
            worker_db,
            item=item,
            target_lang=target_lang,
            rag_settings=rag_settings,
            embedding_settings=embedding_settings,
            chat_config=chat_config,
            text_value=text_value,
            llm_context=llm_context,
            previous_summary=previous_summary,
            existing_term_cards=existing_term_cards,
            gate_duration_ms=gate_duration_ms,
            skill_registry=skill_registry,
            parent_agent_run_id=parent_agent_run_id,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
        )
    finally:
        worker_db.close()


def _run_research_agents(
    *,
    db: Session,
    session_factory: Callable[[], Session] | None,
    items: list[dict[str, Any]],
    target_lang: str,
    rag_settings: RagSettings,
    embedding_settings: EmbeddingSettings,
    chat_config: OpenAIChatConfig,
    text_value: str,
    llm_context: str,
    previous_summary: str,
    existing_term_cards: list[dict[str, Any]],
    gate_duration_ms: int | None,
    skill_registry: SkillRegistry | None = None,
    parent_agent_run_id: str | None = None,
    task_id: str | None = None,
    subtitle_job_id: str | None = None,
) -> list[AgentResearchResult]:
    if not items:
        return []
    parallelism = max(1, min(8, int(rag_settings.agent_parallelism or 1)))
    if session_factory is None:
        parallelism = 1
    timeout_seconds = max(10.0, min(900.0, float(rag_settings.agent_timeout_seconds or 120.0)))
    if parallelism <= 1 or len(items) <= 1:
        out: list[AgentResearchResult] = []
        deadline = time.monotonic() + timeout_seconds * max(1, len(items))
        for item in items:
            if time.monotonic() >= deadline:
                break
            result = _run_research_agent(
                db=db,
                session_factory=None,
                item=item,
                target_lang=target_lang,
                rag_settings=rag_settings,
                embedding_settings=embedding_settings,
                chat_config=chat_config,
                text_value=text_value,
                llm_context=llm_context,
                previous_summary=previous_summary,
                existing_term_cards=existing_term_cards,
                gate_duration_ms=gate_duration_ms,
                skill_registry=skill_registry,
                parent_agent_run_id=parent_agent_run_id,
                task_id=task_id,
                subtitle_job_id=subtitle_job_id,
            )
            if result is not None:
                out.append(result)
        return out

    out: list[AgentResearchResult] = []
    max_workers = min(parallelism, len(items))
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="rag-agent")
    try:
        futures = [
            executor.submit(
                _run_research_agent,
                db=db,
                session_factory=session_factory,
                item=item,
                target_lang=target_lang,
                rag_settings=rag_settings,
                embedding_settings=embedding_settings,
                chat_config=chat_config,
                text_value=text_value,
                llm_context=llm_context,
                previous_summary=previous_summary,
                existing_term_cards=existing_term_cards,
                gate_duration_ms=gate_duration_ms,
                skill_registry=skill_registry,
                parent_agent_run_id=parent_agent_run_id,
                task_id=task_id,
                subtitle_job_id=subtitle_job_id,
            )
            for item in items
        ]
        try:
            for future in as_completed(futures, timeout=timeout_seconds * max(1, math.ceil(len(items) / max_workers))):
                try:
                    result = future.result()
                except Exception:
                    result = None
                if result is not None:
                    out.append(result)
        except FuturesTimeoutError:
            for future in futures:
                future.cancel()
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return out


def build_rag_context(
    db: Session,
    *,
    segments: list[Segment],
    target_lang: str,
    rag_settings: RagSettings,
    embedding_settings: EmbeddingSettings,
    chat_config: OpenAIChatConfig,
    previous_summary: str = "",
    session_factory: Callable[[], Session] | None = None,
    task_id: str | None = None,
    subtitle_job_id: str | None = None,
) -> RagContext:
    if not rag_settings.enabled:
        return RagContext(term_cards=[], knowledge_cards=[], hits=[])

    text_value = "\n".join([str(s.text or "").strip() for s in segments if str(s.text or "").strip()])
    if not text_value:
        return RagContext(term_cards=[], knowledge_cards=[], hits=[])
    llm_context = _context_for_llm(text_value, previous_summary=previous_summary, limit=9000)
    try:
        skill_registry = load_agent_skill_registry(rag_settings)
    except Exception:
        skill_registry = SkillRegistry(())
    skill_summaries = skill_registry.summaries()
    master_agent_run_id: str | None = None
    try:
        master_agent_run_id = _start_agent_run(
            db,
            term="",
            domain=rag_settings.domain,
            target_lang=target_lang,
            query=text_value[:240],
            agent_type="rag_master",
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
        )
        _append_agent_step(
            db,
            master_agent_run_id,
            {
                "kind": "agent",
                "action": "master_start",
                "context_excerpt": text_value[:500],
                "previous_summary": str(previous_summary or "")[:500],
                "tools": {
                    "rag_lookup": True,
                    "wikipedia": bool(rag_settings.wiki_enabled),
                    "search": bool(rag_settings.search_enabled),
                    "fetch_url": True,
                },
                "skills": {
                    "enabled": bool(rag_settings.agent_skills_enabled),
                    "available_count": len(skill_summaries),
                    "available": skill_summaries[:20],
                },
            },
        )
        _append_state_transition(
            db,
            master_agent_run_id,
            from_node="start",
            to_node="retrieval_exact",
            reason="master agent initialized",
            metadata={"segment_count": len(segments)},
        )
    except Exception:
        db.rollback()
        master_agent_run_id = None

    retrieval = RetrievalPipeline[RagHit](
        id_getter=lambda hit: hit.id,
        trace_recorder=(lambda step: _append_agent_step(db, master_agent_run_id, step)) if master_agent_run_id else None,
        error_handler=lambda _e: db.rollback(),
    )
    gate_terms: list[dict[str, Any]] | None = None
    gate_norms: set[str] = set()
    gate_duration_ms: int | None = None
    existing_gate_terms: list[dict[str, Any]] = []
    dictionary_context_cards: list[dict[str, Any]] = []
    dictionary_entry_ids: set[str] = set()
    dictionary_card_norms: set[str] = set()
    local_context_items: list[dict[str, Any]] = []
    local_lookup_terms = _block_lookup_candidates_from_text(text_value, limit=96)
    local_lookup_norms = {normalize_term(term) for term in local_lookup_terms if normalize_term(term)}

    exact_hits = retrieval.run_stage(
        "retrieval_exact_terms",
        text_value[:240],
        lambda: exact_term_hits(
            db,
            text_value=text_value,
            target_lang=target_lang,
            domain=rag_settings.domain,
            limit=max(rag_settings.top_k * 4, 50),
        ),
    )
    for hit in exact_hits:
        if hit.item_type == "term":
            existing_gate_terms.append(
                {
                    "term": hit.term,
                    "translation": hit.translation,
                    "domain": hit.domain,
                    "score": round(hit.score, 4),
                }
            )
            local_context_items.append(_rag_hit_to_local_context(hit))
    if existing_gate_terms:
        _append_agent_step(
            db,
            master_agent_run_id,
            {
                "kind": "tool",
                "action": "master_rag_lookup",
                "tool": "rag_lookup",
                "count": len(existing_gate_terms),
                "results": existing_gate_terms[:10],
                "ok": True,
            },
        )
    if rag_settings.dictionary_enabled and rag_settings.dictionary_top_k > 0:
        dictionary_total_limit = max(1, min(50, max(rag_settings.dictionary_top_k, rag_settings.dictionary_top_k * 4)))
        dictionary_entries, dictionary_errors = _lookup_dictionary_entries_for_terms(
            db,
            terms=local_lookup_terms,
            target_lang=target_lang,
            domain=rag_settings.domain,
            per_term_limit=max(1, min(3, rag_settings.dictionary_top_k)),
            total_limit=dictionary_total_limit,
            min_quality=rag_settings.dictionary_min_quality,
            seen_entry_ids=dictionary_entry_ids,
        )
        dictionary_context_cards = dictionary_entries_to_context_cards(dictionary_entries)
        dictionary_card_norms = _context_card_norms(dictionary_context_cards)
        local_context_items.extend(_dictionary_card_to_local_context(card) for card in dictionary_context_cards)
        if dictionary_context_cards or dictionary_errors:
            _append_agent_step(
                db,
                master_agent_run_id,
                {
                    "kind": "tool",
                    "action": "master_dictionary_block_lookup",
                    "tool": "dictionary_lookup",
                    "term_count": len(local_lookup_terms),
                    "count": len(dictionary_context_cards),
                    "results": dictionary_context_cards[:10],
                    "errors": dictionary_errors[:3],
                    "ok": not dictionary_errors,
                },
            )
    hits: list[RagHit] = retrieval.hits
    seen_ids: set[str] = {hit.id for hit in hits}
    _append_state_transition(
        db,
        master_agent_run_id,
        from_node="retrieval_exact",
        to_node="rag_gate" if rag_settings.auto_discover_terms else "retrieval_pgvector",
        reason="exact term retrieval finished",
        metadata={"hit_count": len(hits), "existing_term_count": len(existing_gate_terms)},
    )

    if rag_settings.auto_discover_terms:
        gate_started = time.perf_counter()
        try:
            gate_terms = pretranslation_rag_gate_openai(
                text_value,
                target_lang=target_lang,
                domain_hint=rag_settings.domain,
                existing_terms=existing_gate_terms,
                local_context=local_context_items,
                previous_summary=previous_summary,
                config=chat_config,
            )
            gate_norms = {normalize_term(str(item.get("term") or "")) for item in gate_terms if isinstance(item, dict)}
            gate_norms.discard("")
            _append_llm_step(
                db,
                master_agent_run_id,
                action="master_pretranslation_rag_gate",
                config=chat_config,
                input_value={
                    "domain": rag_settings.domain,
                    "context_excerpt": text_value[:500],
                    "previous_summary": str(previous_summary or "")[:500],
                    "existing_hit_count": len(existing_gate_terms),
                    "local_context_count": len(local_context_items),
                    "dictionary_context_count": len(dictionary_context_cards),
                },
                output_value={"terms": gate_terms},
                duration_ms=_duration_ms(gate_started),
            )
        except Exception:
            gate_terms = None
            _append_llm_step(
                db,
                master_agent_run_id,
                action="master_pretranslation_rag_gate_failed",
                config=chat_config,
                duration_ms=_duration_ms(gate_started),
                error_type="gate_failed",
            )
        gate_duration_ms = _duration_ms(gate_started)
        _append_state_transition(
            db,
            master_agent_run_id,
            from_node="rag_gate",
            to_node="retrieval_pgvector",
            reason="pre-translation RAG gate finished",
            metadata={"candidate_count": len(gate_terms or [])},
        )

    rag_query_text = text_value[:8000]
    if gate_terms is not None:
        rag_query_text = "\n".join(
            [
                " | ".join(
                    [
                        str(item.get("term") or "").strip(),
                        str(item.get("category") or "").strip(),
                        str(item.get("reason") or "").strip(),
                    ]
                ).strip(" |")
                for item in gate_terms
                if isinstance(item, dict) and str(item.get("term") or "").strip()
            ]
        )
    if rag_settings.dictionary_enabled and rag_settings.dictionary_top_k > 0 and gate_terms:
        gate_dictionary_terms = [
            str(item.get("term") or "").strip()
            for item in gate_terms
            if (
                isinstance(item, dict)
                and str(item.get("term") or "").strip()
                and normalize_term(str(item.get("term") or "")) not in local_lookup_norms
            )
        ]
        remaining_dictionary_limit = max(0, min(50, rag_settings.dictionary_top_k * 4) - len(dictionary_context_cards))
        if gate_dictionary_terms and remaining_dictionary_limit > 0:
            extra_entries, dictionary_errors = _lookup_dictionary_entries_for_terms(
                db,
                terms=gate_dictionary_terms,
                target_lang=target_lang,
                domain=rag_settings.domain,
                per_term_limit=max(1, min(3, rag_settings.dictionary_top_k)),
                total_limit=remaining_dictionary_limit,
                min_quality=rag_settings.dictionary_min_quality,
                seen_entry_ids=dictionary_entry_ids,
            )
            extra_cards = dictionary_entries_to_context_cards(extra_entries)
            if extra_cards:
                dictionary_context_cards.extend(extra_cards)
                dictionary_card_norms.update(_context_card_norms(extra_cards))
                local_context_items.extend(_dictionary_card_to_local_context(card) for card in extra_cards)
            _append_agent_step(
                db,
                master_agent_run_id,
                {
                    "kind": "tool",
                    "action": "master_dictionary_gate_lookup",
                    "tool": "dictionary_lookup",
                    "term_count": len(gate_dictionary_terms),
                    "count": len(extra_cards),
                    "results": extra_cards[:10],
                    "errors": dictionary_errors[:3],
                    "ok": not dictionary_errors,
                },
            )
    if rag_query_text.strip():
        def _vector_lookup() -> list[RagHit]:
            query_embedding = embed_text(rag_query_text[:8000], settings=embedding_settings)
            assert_embedding_dimensions(query_embedding, rag_settings.embedding_dimensions)
            vector_hits = search_knowledge(
                db,
                query_embedding=query_embedding,
                target_lang=target_lang,
                domain=rag_settings.domain,
                embedding_model=embedding_model_key(rag_settings),
                top_k=rag_settings.top_k,
                min_score=rag_settings.min_score,
            )
            if gate_terms is None:
                return vector_hits
            return [
                hit
                for hit in vector_hits
                if not (hit.item_type == "term" and normalize_term(hit.term) not in gate_norms)
            ]

        retrieval.run_stage("retrieval_pgvector", rag_query_text[:240], _vector_lookup)
        hits = retrieval.hits
        seen_ids = {hit.id for hit in hits}
    _append_state_transition(
        db,
        master_agent_run_id,
        from_node="retrieval_pgvector",
        to_node="dispatch_subagents" if rag_settings.auto_discover_terms else "build_context",
        reason="vector retrieval finished",
        metadata={"hit_count": len(hits), "stage_count": len(retrieval.stages)},
    )

    context_only_cards: list[dict[str, Any]] = list(dictionary_context_cards)
    subagent_count = 0

    if rag_settings.auto_discover_terms:
        known_norms = {normalize_term(h.term) for h in hits if h.term}
        known_norms.update(dictionary_card_norms)
        existing_term_cards = [
            {
                "term": hit.term,
                "translation": hit.translation,
                "domain": hit.domain,
                "score": round(hit.score, 4),
            }
            for hit in hits
            if hit.item_type == "term"
        ]
        discovered = gate_terms
        if discovered is None:
            fallback_started = time.perf_counter()
            try:
                discovered = discover_terms_openai(
                    text_value,
                    target_lang=target_lang,
                    domain_hint=rag_settings.domain,
                    previous_summary=previous_summary,
                    config=chat_config,
                )
                for item in discovered:
                    item.setdefault("need_rag", True)
                    item.setdefault("need_search", True)
                    item.setdefault("scope", "global")
                    item.setdefault("category", "legacy_discovery")
                    item.setdefault("priority", 0.5)
            except Exception:
                discovered = []
            gate_duration_ms = _duration_ms(fallback_started)
        discovered_terms = [
            str(item.get("term") or "").strip()
            for item in discovered
            if isinstance(item, dict) and str(item.get("term") or "").strip()
        ]
        known_norms.update(existing_term_norms(db, terms=discovered_terms, target_lang=target_lang))
        research_items: list[dict[str, Any]] = []
        skipped_by_local_context = 0
        skipped_by_policy = 0
        for item in discovered:
            term = str(item.get("term") or "").strip()
            norm = normalize_term(term)
            if not term or norm in known_norms:
                if term and norm in known_norms:
                    skipped_by_local_context += 1
                continue
            domain_hint = str(item.get("domain") or rag_settings.domain or "").strip()
            research_policy = should_research_term(term, domain=domain_hint, context=text_value, gate_item=item)
            if not research_policy.get("should_research") or not bool(research_policy.get("need_search", True)):
                skipped_by_policy += 1
                context_card: dict[str, Any] | None = None
                if research_policy.get("category") == "context_only" and research_policy.get("translation"):
                    context_card = _context_only_term_card(term, research_policy, target_lang=target_lang, domain=domain_hint)
                else:
                    hint = str(item.get("translation") or item.get("translation_hint") or "").strip()
                    if hint:
                        context_card = {
                            "term": term,
                            "translation": hint,
                            "domain": domain_hint,
                            "aliases": [],
                            "description": str(item.get("reason") or research_policy.get("reason") or "").strip(),
                            "confidence": float(item.get("priority") or 0.0) or 0.7,
                            "score": float(item.get("priority") or 0.0) or 0.7,
                            "sources": [],
                            "target_lang": target_lang,
                            "status": str(research_policy.get("scope") or "task_context"),
                        }
                if context_card:
                    context_only_cards.append(context_card)
                    known_norms.add(norm)
                continue
            if not rag_settings.auto_learn_terms:
                continue
            research_items.append(item)
            known_norms.add(norm)
        subagent_count = len(research_items)
        _append_agent_step(
            db,
            master_agent_run_id,
            {
                "kind": "agent",
                "action": "master_dispatch_subagents",
                "candidate_count": len(discovered),
                "research_count": len(research_items),
                "skipped_by_local_context": skipped_by_local_context,
                "skipped_by_policy": skipped_by_policy,
                "terms": [str(item.get("term") or "") for item in research_items[:20] if isinstance(item, dict)],
                "parallelism": rag_settings.agent_parallelism,
            },
        )
        _append_state_transition(
            db,
            master_agent_run_id,
            from_node="dispatch_subagents",
            to_node="wait_subagents",
            reason="research children dispatched",
            metadata={"research_count": len(research_items), "parallelism": rag_settings.agent_parallelism},
        )
        for result in _run_research_agents(
            db=db,
            session_factory=session_factory,
            items=research_items,
            target_lang=target_lang,
            rag_settings=rag_settings,
            embedding_settings=embedding_settings,
            chat_config=chat_config,
            text_value=text_value,
            llm_context=llm_context,
            previous_summary=previous_summary,
            existing_term_cards=existing_term_cards,
            gate_duration_ms=gate_duration_ms,
            skill_registry=skill_registry,
            parent_agent_run_id=master_agent_run_id,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
        ):
            if result.normalized_term:
                known_norms.add(result.normalized_term)
            if result.context_card:
                context_only_cards.append(result.context_card)
            if result.hit and result.hit.id not in seen_ids:
                seen_ids.add(result.hit.id)
                hits.append(result.hit)
        discovered = []
        _append_state_transition(
            db,
            master_agent_run_id,
            from_node="wait_subagents",
            to_node="build_context",
            reason="child agents returned",
            metadata={"context_only_cards": len(context_only_cards), "hit_count": len(hits)},
        )

    term_cards: list[dict[str, Any]] = list(context_only_cards)
    knowledge_cards: list[dict[str, Any]] = []
    for hit in hits[: rag_settings.top_k]:
        if hit.item_type == "term":
            term_cards.append(
                {
                    "term": hit.term,
                    "translation": hit.translation,
                    "domain": hit.domain,
                    "aliases": hit.aliases,
                    "description": hit.description,
                    "confidence": hit.confidence,
                    "score": round(hit.score, 4),
                    "sources": hit.sources[:3],
                }
            )
        else:
            knowledge_cards.append(
                {
                    "title": hit.title or hit.term,
                    "domain": hit.domain,
                    "content": (hit.content or hit.description)[:1200],
                    "score": round(hit.score, 4),
                    "sources": hit.sources[:3],
                }
            )

    if task_id or subtitle_job_id:
        _record_matches(
            db,
            hits=hits[: rag_settings.top_k],
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
            context=text_value[:2000],
        )

    _append_state_transition(
        db,
        master_agent_run_id,
        from_node="build_context",
        to_node="succeeded",
        reason="RagContext built",
        metadata={"term_cards": len(term_cards), "knowledge_cards": len(knowledge_cards), "hits": len(hits)},
    )
    _finish_agent_run(
        db,
        master_agent_run_id,
        status="succeeded",
        result={
            "term_cards": len(term_cards),
            "knowledge_cards": len(knowledge_cards),
            "hits": len(hits),
            "target_lang": target_lang,
            "subagents": subagent_count,
        },
    )
    return RagContext(term_cards=term_cards, knowledge_cards=knowledge_cards, hits=hits)


def _record_matches(
    db: Session,
    *,
    hits: list[RagHit],
    task_id: str | None,
    subtitle_job_id: str | None,
    context: str,
) -> None:
    if not hits:
        return
    for hit in hits:
        try:
            db.execute(
                text(
                    """
                    INSERT INTO translation_term_matches (
                        id, task_id, subtitle_job_id, knowledge_item_id, term,
                        normalized_term, raw_context, decision
                    )
                    VALUES (
                        CAST(:id AS uuid),
                        CAST(:task_id AS uuid),
                        CAST(:subtitle_job_id AS uuid),
                        CAST(:knowledge_item_id AS uuid),
                        :term,
                        :normalized_term,
                        :raw_context,
                        :decision
                    )
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "task_id": str(task_id) if task_id else None,
                    "subtitle_job_id": str(subtitle_job_id) if subtitle_job_id else None,
                    "knowledge_item_id": hit.id,
                    "term": hit.term or hit.title,
                    "normalized_term": normalize_term(hit.term or hit.title),
                    "raw_context": context,
                    "decision": f"score={hit.score:.4f}",
                },
            )
            db.execute(
                text("UPDATE translation_knowledge_items SET usage_count = usage_count + 1, updated_at = now() WHERE id = CAST(:id AS uuid)"),
                {"id": hit.id},
            )
        except Exception:
            continue


def list_knowledge_items(
    db: Session,
    *,
    item_type: str | None = None,
    status: str | None = None,
    q: str | None = None,
    domain: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(500, int(limit))), "offset": max(0, int(offset))}
    if item_type:
        clauses.append("item_type = :item_type")
        params["item_type"] = item_type
    if status:
        clauses.append("status = :status")
        params["status"] = status
    if domain:
        clauses.append("domain ILIKE :domain")
        params["domain"] = f"%{str(domain).strip()}%"
    if q:
        clauses.append(
            """
            (
                term ILIKE :q OR translation ILIKE :q OR title ILIKE :q OR
                description ILIKE :q OR content ILIKE :q OR domain ILIKE :q
            )
            """
        )
        params["q"] = f"%{str(q).strip()}%"
    rows = db.execute(
        text(
            f"""
            SELECT id, item_type, term, translation, target_lang, domain, aliases, title,
                   content, description, sources, confidence, status, created_by,
                   usage_count, embedding_model, last_verified_at, created_at, updated_at
            FROM translation_knowledge_items
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            OFFSET :offset
            """
        ),
        params,
    ).all()
    out: list[dict[str, Any]] = []
    for row in rows:
        m = row._mapping
        out.append(
            {
                "id": str(m["id"]),
                "item_type": m["item_type"],
                "term": m["term"],
                "translation": m["translation"],
                "target_lang": m["target_lang"],
                "domain": m["domain"],
                "aliases": _json_list(m["aliases"]),
                "title": m["title"],
                "content": m["content"],
                "description": m["description"],
                "sources": _json_list(m["sources"]),
                "confidence": float(m["confidence"] or 0.0),
                "status": m["status"],
                "created_by": m["created_by"],
                "usage_count": int(m["usage_count"] or 0),
                "embedding_model": m["embedding_model"],
                "last_verified_at": m["last_verified_at"],
                "created_at": m["created_at"],
                "updated_at": m["updated_at"],
            }
        )
    return out


def delete_knowledge_item(db: Session, item_id: str) -> bool:
    result = db.execute(
        text("DELETE FROM translation_knowledge_items WHERE id = CAST(:id AS uuid)"),
        {"id": str(item_id or "").strip()},
    )
    return int(getattr(result, "rowcount", 0) or 0) > 0


def _agent_run_row_to_dict(row: Any) -> dict[str, Any]:
    m = row._mapping
    result = m["result"]
    steps = m["steps"]
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:
            result = {}
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            steps = []
    return {
        "id": str(m["id"]),
        "agent_type": m["agent_type"],
        "status": m["status"],
        "term": m["term"],
        "domain": m["domain"],
        "target_lang": m["target_lang"],
        "task_id": str(m["task_id"]) if m["task_id"] else None,
        "subtitle_job_id": str(m["subtitle_job_id"]) if m["subtitle_job_id"] else None,
        "query": m["query"],
        "steps": steps if isinstance(steps, list) else [],
        "result": result if isinstance(result, dict) else {},
        "error": m["error"],
        "knowledge_item_id": str(m["knowledge_item_id"]) if m["knowledge_item_id"] else None,
        "parent_agent_run_id": str(m["parent_agent_run_id"]) if m["parent_agent_run_id"] else None,
        "started_at": m["started_at"],
        "finished_at": m["finished_at"],
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
    }


def list_agent_runs(db: Session, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(100, int(limit)))}
    if status:
        clauses.append("status = :status")
        params["status"] = str(status).strip()
    rows = db.execute(
        text(
            f"""
            SELECT id, agent_type, status, term, domain, target_lang, task_id, subtitle_job_id,
                   query, steps, result, error, knowledge_item_id, parent_agent_run_id,
                   started_at, finished_at, created_at, updated_at
            FROM translation_agent_runs
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, started_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).all()
    return [_agent_run_row_to_dict(row) for row in rows]


def get_agent_run(db: Session, run_id: str) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT id, agent_type, status, term, domain, target_lang, task_id, subtitle_job_id,
                   query, steps, result, error, knowledge_item_id, parent_agent_run_id,
                   started_at, finished_at, created_at, updated_at
            FROM translation_agent_runs
            WHERE id = CAST(:id AS uuid)
            """
        ),
        {"id": str(run_id or "").strip()},
    ).first()
    return _agent_run_row_to_dict(row) if row is not None else None


def rebuild_knowledge_embeddings(
    db: Session,
    *,
    rag_settings: RagSettings,
    embedding_settings: EmbeddingSettings,
    item_type: str | None = None,
    status: str | None = None,
    limit: int = 1000,
) -> dict[str, Any]:
    clauses = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(10000, int(limit)))}
    if item_type:
        clauses.append("item_type = :item_type")
        params["item_type"] = str(item_type).strip()
    if status:
        clauses.append("status = :status")
        params["status"] = str(status).strip()

    rows = db.execute(
        text(
            f"""
            SELECT id, item_type, term, translation, target_lang, domain, aliases, title,
                   content, description
            FROM translation_knowledge_items
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, created_at DESC
            LIMIT :limit
            """
        ),
        params,
    ).all()

    model_key = embedding_model_key(rag_settings)
    updated = 0
    failed = 0
    skipped = 0
    errors: list[dict[str, str]] = []
    for row in rows:
        m = row._mapping
        embedding_text = build_knowledge_embedding_text(
            item_type=str(m["item_type"] or ""),
            term=str(m["term"] or ""),
            translation=str(m["translation"] or ""),
            domain=str(m["domain"] or ""),
            aliases=[str(x) for x in _json_list(m["aliases"]) if str(x or "").strip()],
            title=str(m["title"] or ""),
            content=str(m["content"] or ""),
            description=str(m["description"] or ""),
        )
        if not embedding_text.strip():
            skipped += 1
            continue
        try:
            embedding = embed_text(embedding_text, settings=embedding_settings)
            assert_embedding_dimensions(embedding, rag_settings.embedding_dimensions)
            with db.begin_nested():
                db.execute(
                    text(
                        """
                        UPDATE translation_knowledge_items
                        SET embedding = CAST(:embedding AS vector),
                            embedding_model = :embedding_model,
                            embedding_text_hash = :embedding_text_hash,
                            last_verified_at = :last_verified_at,
                            updated_at = now()
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {
                        "id": str(m["id"]),
                        "embedding": _vector_literal(embedding),
                        "embedding_model": model_key,
                        "embedding_text_hash": _hash_text(embedding_text),
                        "last_verified_at": datetime.now(timezone.utc),
                    },
                )
            updated += 1
        except Exception as e:
            failed += 1
            if len(errors) < 20:
                errors.append({"id": str(m["id"]), "error": str(e)})
    return {
        "total": len(rows),
        "updated": updated,
        "failed": failed,
        "skipped": skipped,
        "embedding_model": model_key,
        "dimensions": rag_settings.embedding_dimensions,
        "errors": errors,
    }
