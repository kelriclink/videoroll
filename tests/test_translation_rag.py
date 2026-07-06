from __future__ import annotations

import json

import httpx

from videoroll.ai.client import OpenAIChatConfig
from videoroll.ai.client import request_openai_embedding
from videoroll.apps.subtitle_service.agent_runtime import AgentBudget, AgentBudgetExceeded, AgentRuntime, AgentTraceEvent
from videoroll.apps.subtitle_service.agent_skills import AgentSkill, SkillRegistry
from videoroll.apps.subtitle_service.retrieval import RetrievalPipeline
from videoroll.apps.subtitle_service.rag import (
    _run_research_agents,
    _fallback_search_queries,
    _parse_search_json,
    _search_url_with_params,
    build_knowledge_embedding_text,
    build_rag_context,
    embedding_model_key,
    fetch_search_evidence,
    fetch_wikipedia_evidence,
    normalize_wiki_api_url,
    normalize_term,
    pretranslation_rag_gate_openai,
    rag_settings_from_translate_settings,
    rebuild_knowledge_embeddings,
    search_knowledge,
    should_research_term,
)
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.embeddings import (
    assert_embedding_dimensions,
    embed_text,
    embedding_settings_from_translate_settings,
    safe_embedding_model_name,
)


def test_normalize_term_collapses_case_and_separators() -> None:
    assert normalize_term("  Rush-B  ") == "rush b"
    assert normalize_term("Minecraft_Forge") == "minecraft forge"


def test_rag_settings_from_translate_settings_clamps_values() -> None:
    cfg = rag_settings_from_translate_settings(
        {
            "rag_enabled": True,
            "rag_top_k": 99,
            "rag_min_score": -1,
            "rag_embedding_provider": "local",
            "rag_embedding_model": "embed-small",
            "rag_embedding_dimensions": 99999,
            "rag_embedding_model_dir": "/models/embeddings",
            "rag_embedding_device": "cpu",
            "rag_embedding_base_url": "https://embedding.example/v1",
            "rag_embedding_timeout_seconds": 15,
            "rag_auto_discover_terms": True,
            "rag_auto_learn_terms": True,
            "rag_dictionary_enabled": True,
            "rag_dictionary_top_k": 99,
            "rag_dictionary_min_quality": -1,
            "rag_dictionary_auto_promote": True,
            "rag_wiki_enabled": True,
            "rag_search_enabled": True,
            "rag_search_url": "https://search.example",
            "rag_search_categories": "general,it,general",
            "rag_search_engines": "bing, baidu",
            "rag_search_fallback_engines": "brave,duckduckgo",
            "rag_search_language": "zh-CN",
            "rag_search_safesearch": 2,
            "rag_search_time_range": "month",
            "rag_search_pageno": 3,
            "rag_domain": "CS2",
            "rag_agent_parallelism": 99,
            "rag_agent_timeout_seconds": 9999,
            "rag_agent_skills_enabled": True,
            "rag_agent_builtin_skills_enabled": False,
            "rag_agent_user_skills_enabled": True,
        }
    )
    assert cfg.enabled is True
    assert cfg.top_k == 30
    assert cfg.min_score == 0.0
    assert cfg.embedding_provider == "local"
    assert cfg.embedding_model == "embed-small"
    assert cfg.embedding_dimensions == 4096
    assert cfg.embedding_model_dir == "/models/embeddings"
    assert cfg.embedding_device == "cpu"
    assert cfg.auto_discover_terms is True
    assert cfg.auto_learn_terms is True
    assert cfg.dictionary_enabled is True
    assert cfg.dictionary_top_k == 30
    assert cfg.dictionary_min_quality == 0.0
    assert cfg.dictionary_auto_promote is True
    assert cfg.wiki_enabled is True
    assert cfg.search_enabled is True
    assert cfg.search_url == "https://search.example"
    assert cfg.search_categories == "general,it"
    assert cfg.search_engines == "bing,baidu"
    assert cfg.search_fallback_engines == "brave,duckduckgo"
    assert cfg.search_language == "zh-CN"
    assert cfg.search_safesearch == 2
    assert cfg.search_time_range == "month"
    assert cfg.search_pageno == 3
    assert cfg.domain == "CS2"
    assert cfg.agent_parallelism == 8
    assert cfg.agent_timeout_seconds == 900.0
    assert cfg.agent_skills_enabled is True
    assert cfg.agent_builtin_skills_enabled is False
    assert cfg.agent_user_skills_enabled is True


def test_agent_runtime_enforces_budget_and_records_normalized_steps() -> None:
    steps: list[dict[str, object]] = []
    runtime = AgentRuntime(
        agent_name="rag_term_research",
        run_id="run-1",
        budget=AgentBudget(max_llm_calls=1, max_tool_calls=1, max_fetch_calls=0, timeout_seconds=30),
        trace_recorder=steps.append,
    )

    runtime.before_llm()
    runtime.record(AgentTraceEvent(kind="agent", action="started", output={"ok": True}))

    try:
        runtime.before_llm()
    except AgentBudgetExceeded as e:
        assert "LLM call budget" in str(e)
    else:
        raise AssertionError("expected LLM budget to stop the agent")

    assert steps[0]["run_id"] == "run-1"
    assert steps[0]["agent_name"] == "rag_term_research"
    assert steps[0]["event_id"]
    assert steps[0]["span_id"]
    assert steps[0]["status"] == "ok"
    assert steps[0]["budget"]["llm_calls"] == 1  # type: ignore[index]


def test_retrieval_pipeline_dedupes_records_and_rolls_back_on_error() -> None:
    events: list[dict[str, object]] = []
    errors: list[str] = []

    pipeline = RetrievalPipeline[dict[str, str]](
        id_getter=lambda item: item["id"],
        trace_recorder=events.append,
        error_handler=lambda e: errors.append(type(e).__name__),
    )

    first = pipeline.run_stage("exact", "AWP", lambda: [{"id": "1"}, {"id": "1"}, {"id": "2"}])
    second = pipeline.run_stage("vector", "AWP", lambda: (_ for _ in ()).throw(RuntimeError("db aborted")))

    assert first == [{"id": "1"}, {"id": "1"}, {"id": "2"}]
    assert second == []
    assert pipeline.hits == [{"id": "1"}, {"id": "2"}]
    assert events[0]["action"] == "exact"
    assert events[0]["count"] == 2
    assert events[1]["status"] == "failed"
    assert events[1]["error_type"] == "RuntimeError"
    assert errors == ["RuntimeError"]


def test_research_tool_registry_exposes_enabled_tool_schemas() -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    cfg = rag_settings_from_translate_settings(
        {
            "rag_enabled": True,
            "rag_wiki_enabled": True,
            "rag_search_enabled": True,
        }
    )
    specs = rag_module._ordered_tool_specs(rag_module._research_tool_registry(cfg))
    names = [spec["name"] for spec in specs]

    assert names == ["rag_lookup", "dictionary_lookup", "wiki_search", "search_web", "fetch_url", "finish"]
    search_spec = next(spec for spec in specs if spec["name"] == "search_web")
    assert "input_schema" in search_spec
    assert "query" in search_spec["input_schema"]["properties"]
    assert "filter_search_engine_internal_pages" in search_spec["guardrails"]
    dictionary_spec = next(spec for spec in specs if spec["name"] == "dictionary_lookup")
    assert "source_license_preserved" in dictionary_spec["guardrails"]


def test_skill_registry_loads_json_markdown_and_selects(tmp_path) -> None:
    json_skill = tmp_path / "tech"
    json_skill.mkdir()
    (json_skill / "notes.md").write_text("Prefer official manuals.", encoding="utf-8")
    (json_skill / "skill.json").write_text(
        json.dumps(
            {
                "name": "technical-term-research",
                "description": "Research hardware terms.",
                "domain": ["hardware"],
                "triggers": ["VGA"],
                "allowed_tools": ["search_web", "fetch_url"],
                "instructions": "Use Chinese technical terminology.",
                "resources": [{"name": "notes", "path": "notes.md"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    md_skill = tmp_path / "wiki"
    md_skill.mkdir()
    (md_skill / "SKILL.md").write_text(
        "---\n"
        "name: wiki-check\n"
        "description: Verify encyclopedia terms.\n"
        "domain: astrophysics\n"
        "triggers:\n"
        "  - Lyman-alpha\n"
        "allowed_tools:\n"
        "  - wiki_search\n"
        "---\n"
        "# Wiki Check\n"
        "Check stable terminology before writing RAG.",
        encoding="utf-8",
    )

    registry = SkillRegistry.load(include_builtin=False, include_user=True, user_dir=tmp_path)
    names = [skill.name for skill in registry.list()]
    selected = registry.select(term="VGA red signal", domain="hardware", context="VGA connector pinout")

    assert names == ["technical-term-research", "wiki-check"]
    assert selected[0].name == "technical-term-research"
    assert selected[0].resources[0].content == "Prefer official manuals."


def test_builtin_skills_are_discoverable() -> None:
    registry = SkillRegistry.load(include_builtin=True, include_user=False)
    names = {skill.name for skill in registry.list()}
    searxng_skill = next(skill for skill in registry.list() if skill.name == "searxng-web-research")

    assert "translation-term-research-core" in names
    assert "source-verification-rag-write" in names
    assert "wikipedia-encyclopedia-research" in names
    assert "searxng-web-research" in names
    assert searxng_skill.resources
    assert "Query patterns" in searxng_skill.resources[0].content


def test_active_skill_is_passed_to_child_agent_and_filters_tools(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    captured: dict[str, object] = {}
    steps: list[dict[str, object]] = []

    monkeypatch.setattr(rag_module, "_append_agent_step", lambda _db, _run_id, step: steps.append(step))
    monkeypatch.setattr(rag_module, "_append_llm_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "fetch_search_evidence", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag_module, "fetch_wikipedia_evidence", lambda *_args, **_kwargs: [])

    def fake_decision(**kwargs):
        captured.update(kwargs)
        return {"action": "finish", "reason": "done", "skill_name": "web-only"}

    monkeypatch.setattr(rag_module, "research_agent_next_action_openai", fake_decision)

    skill = AgentSkill(
        name="web-only",
        description="Use web search only.",
        allowed_tools=["search_web"],
        instructions="Prefer search snippets first.",
    )
    evidence, tools_used, rounds = rag_module._collect_evidence_with_tool_agent(
        object(),  # type: ignore[arg-type]
        agent_run_id="run-1",
        term="VGA",
        domain_hint="hardware",
        target_lang="zh",
        rag_settings=rag_settings_from_translate_settings(
            {
                "rag_enabled": True,
                "rag_wiki_enabled": True,
                "rag_search_enabled": True,
                "rag_search_url": "https://search.example",
            }
        ),
        chat_config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
        llm_context="VGA red signal",
        search_queries=["VGA 红色 信号"],
        active_skills=[skill],
        max_steps=1,
    )

    assert evidence == []
    assert tools_used == ["search"]
    assert rounds == 1
    assert captured["available_tools"] == ["rag_lookup", "search_web", "finish"]
    assert captured["active_skills"][0]["name"] == "web-only"  # type: ignore[index]
    assert any(step.get("action") == "skill_activated" for step in steps)


def test_build_knowledge_embedding_text_includes_term_fields() -> None:
    text = build_knowledge_embedding_text(
        item_type="term",
        term="AWP",
        translation="AWP 狙击枪",
        domain="CS2",
        aliases=["Magnum Sniper Rifle"],
        description="Counter-Strike 系列中的狙击枪。",
    )
    assert "term: AWP" in text
    assert "translation: AWP 狙击枪" in text
    assert "domain: CS2" in text
    assert "Magnum Sniper Rifle" in text


def test_should_research_term_skips_logic_variables() -> None:
    decision = should_research_term("Q", domain="技术/逻辑", context="truth table for P and Q")

    assert decision["should_research"] is False
    assert decision["should_persist"] is False
    assert decision["category"] == "local_variable"


def test_should_research_term_keeps_common_terms_context_only() -> None:
    decision = should_research_term("truth table", domain="logic")

    assert decision["should_research"] is False
    assert decision["should_persist"] is False
    assert decision["category"] == "context_only"
    assert decision["translation"] == "真值表"


def test_should_research_term_uses_gate_category_to_skip_basic_terms() -> None:
    decision = should_research_term(
        "pixel",
        domain="技术",
        gate_item={
            "category": "basic_dictionary",
            "need_rag": False,
            "need_search": False,
            "scope": "none",
            "reason": "basic visual unit",
        },
    )

    assert decision["should_research"] is False
    assert decision["category"] == "basic_dictionary"


def test_should_research_term_uses_gate_category_to_allow_high_value_terms() -> None:
    decision = should_research_term(
        "AWP",
        domain="CS2",
        gate_item={
            "category": "acronym",
            "need_rag": True,
            "need_search": True,
            "scope": "global",
            "reason": "game-specific weapon acronym",
        },
    )

    assert decision["should_research"] is True
    assert decision["should_persist"] is True
    assert decision["category"] == "acronym"


def test_pretranslation_rag_gate_parses_all_returned_terms(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    monkeypatch.setattr(
        rag_module,
        "request_openai_json_object",
        lambda **_kwargs: {
            "terms": [
                {
                    "term": "AWP",
                    "category": "acronym",
                    "domain": "CS2",
                    "need_rag": True,
                    "need_search": True,
                    "scope": "global",
                    "priority": 0.91,
                    "reason": "weapon acronym",
                },
                {
                    "term": "Rush B",
                    "category": "domain_jargon",
                    "domain": "CS2",
                    "need_rag": True,
                    "need_search": True,
                    "scope": "global",
                    "priority": 0.95,
                    "reason": "strategy phrase",
                },
                {
                    "term": "pixel",
                    "category": "basic_dictionary",
                    "domain": "technology",
                    "need_rag": False,
                    "need_search": False,
                    "scope": "none",
                    "priority": 0.1,
                    "reason": "basic term",
                },
            ]
        },
    )

    terms = pretranslation_rag_gate_openai(
        "Rush B, AWP, pixel.",
        target_lang="zh",
        domain_hint="CS2",
        config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    assert [item["term"] for item in terms] == ["Rush B", "AWP", "pixel"]
    assert terms[0]["priority"] == 0.95
    assert terms[2]["need_search"] is False


def test_pretranslation_rag_gate_includes_previous_summary(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    captured: dict[str, str] = {}

    def fake_request(**kwargs):
        captured["user_prompt"] = kwargs["user_prompt"]
        return {"terms": []}

    monkeypatch.setattr(rag_module, "request_openai_json_object", fake_request)

    pretranslation_rag_gate_openai(
        "It contains a Lyman-alpha blob.",
        target_lang="zh",
        domain_hint="astrophysics",
        local_context=[
            {
                "source": "dictionary",
                "term": "Lyman-alpha blob",
                "translation": "莱曼阿尔法斑点",
                "description": "astronomy dictionary entry",
                "score": 0.92,
            }
        ],
        previous_summary="The video is discussing TON618 and distant quasars.",
        config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    assert "前文摘要" in captured["user_prompt"]
    assert "TON618" in captured["user_prompt"]
    assert "当前字幕 block" in captured["user_prompt"]
    assert "已有本地上下文 JSON" in captured["user_prompt"]
    assert "need_search=false" in captured["user_prompt"]


def test_run_research_agents_uses_session_factory_for_parallel_terms(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    created_sessions: list[object] = []
    calls: list[tuple[object, str, str | None]] = []

    class _Session:
        def close(self) -> None:
            return None

    def session_factory() -> _Session:
        session = _Session()
        created_sessions.append(session)
        return session

    def fake_research(db, *, item, **kwargs):
        term = item["term"]
        calls.append((db, term, kwargs.get("parent_agent_run_id")))
        return rag_module.AgentResearchResult(term=term, normalized_term=normalize_term(term))

    monkeypatch.setattr(rag_module, "_research_discovered_term", fake_research)
    rag_cfg = rag_settings_from_translate_settings(
        {
            "rag_enabled": True,
            "rag_agent_parallelism": 2,
            "rag_agent_timeout_seconds": 30,
        }
    )
    emb_cfg = embedding_settings_from_translate_settings({"rag_embedding_provider": "local", "rag_embedding_dimensions": 2})

    results = _run_research_agents(
        db=object(),  # type: ignore[arg-type]
        session_factory=session_factory,  # type: ignore[arg-type]
        items=[{"term": "AWP"}, {"term": "Rush B"}],
        target_lang="zh",
        rag_settings=rag_cfg,
        embedding_settings=emb_cfg,
        chat_config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
        text_value="AWP Rush B",
        llm_context="AWP Rush B",
        previous_summary="",
        existing_term_cards=[],
        gate_duration_ms=1,
        parent_agent_run_id="master-run",
        task_id=None,
        subtitle_job_id=None,
    )

    assert {item.term for item in results} == {"AWP", "Rush B"}
    assert len(created_sessions) == 2
    assert {id(db) for db, _term, _parent in calls} == {id(session) for session in created_sessions}
    assert {parent for _db, _term, parent in calls} == {"master-run"}


def test_fallback_search_queries_include_definition_and_target_language() -> None:
    queries = _fallback_search_queries("Hopper Minecart", domain="Minecraft", target_lang="zh")

    assert queries[0] == "Minecraft Hopper Minecart"
    assert any("definition" in query for query in queries)
    assert any("中文" in query for query in queries)


def test_embedding_settings_from_translate_settings_supports_local_provider() -> None:
    cfg = embedding_settings_from_translate_settings(
        {
            "rag_embedding_provider": "local",
            "rag_embedding_model": "BAAI--bge-small-zh-v1.5",
            "rag_embedding_dimensions": 512,
            "rag_embedding_model_dir": "/models/embeddings",
            "rag_embedding_device": "cpu",
            "openai_api_key": "unused",
            "openai_base_url": "https://example.invalid/v1",
            "openai_timeout_seconds": 30,
        }
    )
    assert cfg.provider == "local"
    assert cfg.model == "BAAI--bge-small-zh-v1.5"
    assert cfg.dimensions == 512


def test_embedding_settings_from_translate_settings_supports_openai_compatible_provider() -> None:
    cfg = embedding_settings_from_translate_settings(
        {
            "rag_embedding_provider": "openai",
            "rag_embedding_model": "vendor-embed",
            "rag_embedding_dimensions": 1024,
            "rag_embedding_api_key": "embed-key",
            "rag_embedding_base_url": "https://embedding.example/v1",
            "rag_embedding_timeout_seconds": 12,
            "openai_api_key": "chat-key",
            "openai_base_url": "https://chat.example/v1",
            "openai_timeout_seconds": 60,
        }
    )

    assert cfg.provider == "openai"
    assert cfg.model == "vendor-embed"
    assert cfg.openai_config.api_key == "embed-key"
    assert cfg.openai_config.base_url == "https://embedding.example/v1"
    assert cfg.openai_config.timeout_seconds == 12


def test_embedding_settings_do_not_reuse_translation_openai_credentials() -> None:
    cfg = embedding_settings_from_translate_settings(
        {
            "rag_embedding_provider": "openai",
            "rag_embedding_model": "vendor-embed",
            "rag_embedding_dimensions": 1024,
            "openai_api_key": "chat-key",
            "openai_base_url": "https://chat.example/v1",
            "openai_timeout_seconds": 60,
        }
    )

    assert cfg.openai_config.api_key is None
    assert cfg.openai_config.base_url == ""
    assert cfg.openai_config.timeout_seconds == 60


def test_local_embedding_openvino_device_uses_openvino_backend(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import embeddings as embeddings_module

    calls: list[tuple[str, str, str]] = []

    def fake_embed(path, text, *, device):
        calls.append((str(path), text, device))
        return [0.1, 0.2]

    monkeypatch.setattr(embeddings_module, "_embed_text_openvino", fake_embed)
    cfg = embedding_settings_from_translate_settings(
        {
            "rag_embedding_provider": "local",
            "rag_embedding_model": "embed-small",
            "rag_embedding_dimensions": 2,
            "rag_embedding_model_dir": "/models/embeddings",
            "rag_embedding_device": "openvino:GPU",
        }
    )

    assert embed_text("hello", settings=cfg) == [0.1, 0.2]
    assert calls == [("/models/embeddings/embed-small", "hello", "GPU")]


def test_safe_embedding_model_name_matches_repo_style() -> None:
    assert safe_embedding_model_name("BAAI/bge-small-zh-v1.5") == "BAAI--bge-small-zh-v1.5"


def test_assert_embedding_dimensions() -> None:
    assert_embedding_dimensions([0.1, 0.2], 2)
    try:
        assert_embedding_dimensions([0.1], 2)
    except RuntimeError as e:
        assert "dimension mismatch" in str(e)
    else:
        raise AssertionError("expected dimension mismatch")


def test_openai_embedding_request_sends_dimensions() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        data = dict(json.loads(request.content.decode("utf-8")))
        requests.append(data)
        assert data["model"] == "Qwen3-Embedding-8B"
        assert data["input"] == "纳米"
        assert data["dimensions"] == 4096
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    vector = request_openai_embedding(
        config=OpenAIChatConfig(
            api_key="embed-key",
            base_url="https://embedding.example/v1",
            model="Qwen3-Embedding-8B",
            embedding_dimensions=4096,
        ),
        text="纳米",
        client=client,
    )

    assert vector == [0.1, 0.2]
    assert requests


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _Row:
    def __init__(self, **kwargs: object) -> None:
        self._mapping = kwargs


class _NestedTransaction:
    def __enter__(self) -> "_NestedTransaction":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakeKnowledgeDb:
    def __init__(self, rows: list[object]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def execute(self, stmt: object, params: dict[str, object] | None = None) -> _Result:
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        if sql.strip().startswith("SELECT"):
            return _Result(self.rows)
        return _Result([])

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()


def test_research_falls_back_to_search_when_wiki_verification_rejects(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    tool_calls: list[str] = []
    steps: list[dict[str, object]] = []
    finished: dict[str, object] = {}

    monkeypatch.setattr(rag_module, "_start_agent_run", lambda *_args, **_kwargs: "run-1")
    monkeypatch.setattr(rag_module, "_append_llm_step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "_append_agent_step", lambda _db, _run_id, step: steps.append(step))
    monkeypatch.setattr(
        rag_module,
        "_finish_agent_run",
        lambda _db, _run_id, **kwargs: finished.update(kwargs),
    )
    monkeypatch.setattr(rag_module, "existing_term_norms", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(rag_module, "generate_search_queries_openai", lambda **_kwargs: ["hobby knife definition"])
    decisions = iter(
        [
            {"action": "wiki_search", "query": "hobby knife definition", "reason": "start with encyclopedic source"},
            {"action": "finish", "reason": "wiki evidence collected", "final_answer_ready": True},
        ]
    )
    monkeypatch.setattr(rag_module, "research_agent_next_action_openai", lambda **_kwargs: next(decisions))

    def fake_wiki(*_args, **_kwargs):
        tool_calls.append("wiki")
        return [
            {
                "title": "Hobby knife",
                "url": "https://en.wikipedia.org/wiki/Utility_knife",
                "snippet": "A precision knife used for hobbies.",
                "content": "A precision knife used for hobbies and model making.",
            }
        ]

    def fake_search(*_args, **_kwargs):
        tool_calls.append("search")
        return [
            {
                "title": "Hobby knife definition",
                "url": "https://example.com/hobby-knife",
                "snippet": "Hobby knives are precision cutting tools.",
                "content": "Hobby knives are precision cutting tools used by model makers.",
            }
        ]

    monkeypatch.setattr(rag_module, "fetch_wikipedia_evidence", fake_wiki)
    monkeypatch.setattr(rag_module, "fetch_search_evidence", fake_search)
    monkeypatch.setattr(
        rag_module,
        "explain_term_from_evidence_openai",
        lambda **_kwargs: {
            "term": "hobby knife",
            "translation": "模型刀",
            "domain": "手工模型",
            "aliases": [],
            "description": "精细切割工具。",
            "sources": [],
            "confidence": 0.8,
        },
    )
    monkeypatch.setattr(
        rag_module,
        "verify_glossary_entry_openai",
        lambda **_kwargs: {
            "supported": False,
            "context_consistent": False,
            "should_write": False,
            "should_auto_approve": False,
            "confidence": 0.0,
            "reason": "unsupported",
            "failure_category": "unsupported_translation_or_no_context",
        },
    )

    result = rag_module._research_discovered_term(
        object(),  # type: ignore[arg-type]
        item={"term": "hobby knife", "category": "domain_jargon", "need_rag": True, "need_search": True, "scope": "global"},
        target_lang="zh",
        rag_settings=rag_settings_from_translate_settings(
            {
                "rag_enabled": True,
                "rag_auto_learn_terms": True,
                "rag_wiki_enabled": True,
                "rag_search_enabled": True,
                "rag_search_url": "https://search.example/search",
                "rag_embedding_dimensions": 2,
            }
        ),
        embedding_settings=embedding_settings_from_translate_settings({"rag_embedding_provider": "local", "rag_embedding_dimensions": 2}),
        chat_config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
        text_value="Use a hobby knife to cut the tape.",
        llm_context="Use a hobby knife to cut the tape.",
        previous_summary="",
        existing_term_cards=[],
        gate_duration_ms=1,
        task_id=None,
        subtitle_job_id=None,
    )

    assert result is not None
    assert tool_calls == ["wiki", "search"]
    assert any(step.get("action") == "evidence_tool_fallback" and step.get("tool") == "search" for step in steps)
    assert finished["status"] == "skipped"
    assert finished["result"]["tools_used"] == ["wikipedia", "search"]  # type: ignore[index]


def test_build_rag_context_skips_research_for_existing_pending_term(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    research_items: list[dict[str, object]] = []

    class _Db:
        def execute(self, stmt: object, params: dict[str, object] | None = None) -> _Result:
            sql = str(stmt)
            if "SELECT DISTINCT normalized_term" in sql:
                return _Result([_Row(normalized_term="hobby knife")])
            return _Result([])

        def rollback(self) -> None:
            return None

    monkeypatch.setattr(
        rag_module,
        "pretranslation_rag_gate_openai",
        lambda *_args, **_kwargs: [
            {
                "term": "hobby knife",
                "category": "domain_jargon",
                "domain": "手工模型",
                "need_rag": True,
                "need_search": True,
                "scope": "global",
                "priority": 0.8,
                "reason": "tool name",
            }
        ],
    )
    monkeypatch.setattr(rag_module, "exact_term_hits", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag_module, "embed_text", lambda *_args, **_kwargs: [0.1, 0.2])
    monkeypatch.setattr(rag_module, "assert_embedding_dimensions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "search_knowledge", lambda *_args, **_kwargs: [])

    def fake_run_research_agents(*_args, **kwargs):
        research_items.extend(kwargs["items"])
        return []

    monkeypatch.setattr(rag_module, "_run_research_agents", fake_run_research_agents)

    ctx = build_rag_context(
        _Db(),  # type: ignore[arg-type]
        segments=[Segment(0, 1, "Use a hobby knife to cut the tape.")],
        target_lang="zh",
        rag_settings=rag_settings_from_translate_settings(
            {
                "rag_enabled": True,
                "rag_auto_discover_terms": True,
                "rag_auto_learn_terms": True,
                "rag_embedding_dimensions": 2,
            }
        ),
        embedding_settings=embedding_settings_from_translate_settings({"rag_embedding_provider": "local", "rag_embedding_dimensions": 2}),
        chat_config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    assert ctx.term_cards == []
    assert research_items == []


def test_build_rag_context_prefetches_dictionary_before_gate_and_skips_child_agent(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    captured_gate: dict[str, object] = {}
    lookup_terms: list[str] = []
    research_items: list[dict[str, object]] = []

    class _Db:
        def execute(self, stmt: object, params: dict[str, object] | None = None) -> _Result:
            return _Result([])

        def rollback(self) -> None:
            return None

    def fake_dictionary_lookup(_db, *, term: str, **_kwargs):
        lookup_terms.append(term)
        if normalize_term(term) != "hobby knife":
            return []
        return [
            {
                "id": "dict-1",
                "source_name": "ECDICT",
                "source_slug": "ecdict",
                "source_url": "https://example.test/ecdict",
                "license": "MIT",
                "license_url": "",
                "term": "hobby knife",
                "translations": ["模型刀", "美工刀"],
                "definition": "A small craft knife.",
                "pos": "n.",
                "domain": "手工模型",
                "aliases": [],
                "quality": 0.95,
            }
        ]

    def fake_gate(*_args, **kwargs):
        captured_gate.update(kwargs)
        return [
            {
                "term": "hobby knife",
                "category": "domain_jargon",
                "domain": "手工模型",
                "need_rag": True,
                "need_search": True,
                "scope": "global",
                "priority": 0.8,
                "reason": "tool name",
            }
        ]

    def fake_run_research_agents(*_args, **kwargs):
        research_items.extend(kwargs["items"])
        return []

    monkeypatch.setattr(rag_module, "lookup_dictionary_entries", fake_dictionary_lookup)
    monkeypatch.setattr(rag_module, "pretranslation_rag_gate_openai", fake_gate)
    monkeypatch.setattr(rag_module, "exact_term_hits", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag_module, "embed_text", lambda *_args, **_kwargs: [0.1, 0.2])
    monkeypatch.setattr(rag_module, "assert_embedding_dimensions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rag_module, "search_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(rag_module, "_run_research_agents", fake_run_research_agents)

    ctx = build_rag_context(
        _Db(),  # type: ignore[arg-type]
        segments=[Segment(0, 1, "Use a hobby knife to cut the tape.")],
        target_lang="zh",
        rag_settings=rag_settings_from_translate_settings(
            {
                "rag_enabled": True,
                "rag_auto_discover_terms": True,
                "rag_auto_learn_terms": True,
                "rag_dictionary_enabled": True,
                "rag_dictionary_top_k": 8,
                "rag_embedding_dimensions": 2,
            }
        ),
        embedding_settings=embedding_settings_from_translate_settings({"rag_embedding_provider": "local", "rag_embedding_dimensions": 2}),
        chat_config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    local_context = captured_gate["local_context"]
    assert "hobby knife" in [normalize_term(term) for term in lookup_terms]
    assert isinstance(local_context, list)
    assert local_context[0]["source"] == "dictionary"  # type: ignore[index]
    assert local_context[0]["translation"] == "模型刀"  # type: ignore[index]
    assert ctx.term_cards[0]["translation"] == "模型刀"
    assert research_items == []


def test_search_knowledge_filters_by_embedding_model() -> None:
    db = _FakeKnowledgeDb([])
    search_knowledge(
        db,  # type: ignore[arg-type]
        query_embedding=[0.1, 0.2],
        target_lang="zh",
        embedding_model="local:embed-small",
    )
    sql, params = db.calls[0]
    assert "embedding_model = :embedding_model" in sql
    assert params["embedding_model"] == "local:embed-small"


def test_search_url_accepts_base_url_and_legacy_search_url() -> None:
    assert (
        _search_url_with_params("https://search.example", query="truth table", json_format=True)
        == "https://search.example/search?q=truth+table&format=json"
    )
    assert (
        _search_url_with_params("https://search.example/search?q=", query="truth table", json_format=True)
        == "https://search.example/search?q=truth+table&format=json"
    )
    assert (
        _search_url_with_params("https://search.example/searxng", query="truth table", json_format=False)
        == "https://search.example/searxng/search?q=truth+table"
    )
    assert (
        _search_url_with_params("https://search.example", query="truth table", json_format=True, extra_params={"engines": "bing,baidu"})
        == "https://search.example/search?engines=bing%2Cbaidu&q=truth+table&format=json"
    )
    assert (
        _search_url_with_params(
            "https://search.example",
            query="VGA 红色信号",
            json_format=True,
            extra_params={
                "categories": "general,it",
                "engines": "bing,baidu",
                "language": "zh-CN",
                "safesearch": "0",
                "time_range": "month",
                "pageno": "2",
            },
        )
        == "https://search.example/search?categories=general%2Cit&engines=bing%2Cbaidu&language=zh-CN&safesearch=0&time_range=month&pageno=2&q=VGA+%E7%BA%A2%E8%89%B2%E4%BF%A1%E5%8F%B7&format=json"
    )


def test_parse_search_json_accepts_searxng_content_results() -> None:
    rows = _parse_search_json(
        {
            "query": "VGA 红色 信号 中文 术语",
            "results": [
                {
                    "url": "https://baike.baidu.com/item/VGA%E6%8E%A5%E5%8F%A3/909309",
                    "title": "VGA接口_百度百科",
                    "content": "视频图形阵列（VGA，Video Graphic Array）是一个使用模拟信号的电脑显示标准。",
                    "engine": "bing",
                },
                {
                    "title": "主板vga接线定义 - 百度文库",
                    "url": "https://wenku.baidu.com/view/demo.html",
                    "content": "红色信号（Red）：用于传输视频信号中的红色分量。",
                    "engine": "baidu",
                },
            ],
        }
    )

    assert len(rows) == 2
    assert rows[0]["title"] == "VGA接口_百度百科"
    assert "Video Graphic Array" in rows[0]["snippet"]
    assert "红色信号" in rows[1]["snippet"]


def test_normalize_wiki_api_url_accepts_wikipedia_page_url() -> None:
    assert normalize_wiki_api_url("https://en.wikipedia.org/wiki/Wiki") == "https://en.wikipedia.org/w/api.php"
    assert normalize_wiki_api_url("https://en.wikipedia.org") == "https://en.wikipedia.org/w/api.php"
    assert normalize_wiki_api_url("https://en.wikipedia.org/w/api.php") == "https://en.wikipedia.org/w/api.php"


def test_fetch_wikipedia_evidence_reads_page_extract(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[dict[str, object]] = []

    class _Response:
        def __init__(self, data: object, *, url: str = "https://en.wikipedia.org/w/api.php") -> None:
            self._data = data
            self.url = url
            self.headers = {"content-type": "application/json"}
            self.text = ""

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self._data

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str, params: dict[str, object] | None = None) -> _Response:
            calls.append({"url": url, "params": params or {}})
            params = params or {}
            if params.get("list") == "search":
                return _Response(
                    {
                        "query": {
                            "search": [
                                {
                                    "title": "Lyman-alpha blob",
                                    "pageid": 123,
                                    "snippet": "A <span>large concentration</span> of gas.",
                                }
                            ]
                        }
                    }
                )
            return _Response(
                {
                    "query": {
                        "pages": [
                            {
                                "pageid": 123,
                                "title": "Lyman-alpha blob",
                                "extract": "A Lyman-alpha blob is a large concentration of gas emitting the Lyman-alpha emission line.",
                            }
                        ]
                    }
                }
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)

    evidence = fetch_wikipedia_evidence("Lyman-alpha blob", domain="astrophysics", queries=["Lyman-alpha blob definition"])

    assert calls[0]["params"]["list"] == "search"  # type: ignore[index]
    assert any(call["params"].get("prop") == "extracts" for call in calls)  # type: ignore[union-attr]
    assert evidence[0]["tool"] == "wiki"
    assert evidence[0]["source"] == "Wikipedia"
    assert evidence[0]["title"] == "Lyman-alpha blob"
    assert evidence[0]["url"] == "https://en.wikipedia.org/wiki/Lyman-alpha_blob"
    assert "large concentration of gas" in evidence[0]["content"]


def test_fetch_search_evidence_reads_result_pages(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Response:
        def __init__(self, text: str, *, url: str, content_type: str = "text/html") -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            raise ValueError("not json")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.calls: list[str] = []

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            self.calls.append(url)
            if "search" in url:
                return _Response(
                    """
                    <html><body>
                      <article class="result">
                        <h3><a href="https://example.com/wiki/awp">AWP - Wiki</a></h3>
                        <p class="content">Counter-Strike sniper rifle.</p>
                      </article>
                    </body></html>
                    """,
                    url=url,
                )
            return _Response(
                """
                <html>
                  <head><title>AWP</title></head>
                  <body><main>In Counter-Strike, the AWP is a powerful sniper rifle.</main></body>
                </html>
                """,
                url=url,
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)
    monkeypatch.setattr(rag_module, "_is_fetchable_url", lambda _url: True)

    evidence = fetch_search_evidence("AWP", domain="CS2", search_url="https://search.example/search?q=")

    assert evidence[0]["title"] == "AWP - Wiki"
    assert evidence[0]["url"] == "https://example.com/wiki/awp"
    assert "powerful sniper rifle" in evidence[0]["content"]


def test_safe_public_get_rejects_redirect_to_private_host() -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"}, request=request)
        return httpx.Response(200, text="internal", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    try:
        try:
            rag_module._safe_public_get(client, "https://public.example/start")
        except RuntimeError as e:
            assert "non-public URL" in str(e)
        else:
            raise AssertionError("expected redirect to private host to be rejected")
    finally:
        client.close()

    assert calls == ["https://public.example/start"]


def test_fetch_search_evidence_skips_pages_when_llm_says_summary_is_enough(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[str] = []

    class _Response:
        def __init__(self, text: str, *, url: str, content_type: str = "text/html") -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            raise ValueError("not json")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            calls.append(url)
            if "search" in url:
                return _Response(
                    """
                    <html><body>
                      <article class="result">
                        <h3><a href="https://example.com/wiki/hopper">Minecraft Hopper</a></h3>
                        <p>Hopper is a block that captures item entities.</p>
                      </article>
                    </body></html>
                    """,
                    url=url,
                )
            return _Response("<html><body>should not fetch</body></html>", url=url)

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)
    monkeypatch.setattr(
        rag_module,
        "request_openai_json_object",
        lambda **_kwargs: {
            "summary_sufficient": True,
            "fetch_urls": [],
            "reason": "snippet is enough",
            "confidence": 0.92,
        },
    )

    evidence = fetch_search_evidence(
        "Hopper",
        domain="Minecraft",
        search_url="https://search.example/search",
        context="This build uses Hopper Minecart.",
        target_lang="zh",
        config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    assert len(calls) == 1
    assert "format=json" in calls[0]
    assert evidence[0]["fetch_skipped"] == "not_selected_by_agent"
    assert "captures item entities" in evidence[0]["snippet"]


def test_fetch_search_evidence_filters_searxng_internal_about(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[str] = []

    class _Response:
        def __init__(self, text: str, *, url: str, content_type: str = "text/html") -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            raise ValueError("not json")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            calls.append(url)
            return _Response(
                """
                <html><body>
                  <article class="result">
                    <h3><a href="https://search.example/info/en/about">About</a></h3>
                    <p>About SearXNG preferences search syntax.</p>
                  </article>
                </body></html>
                """,
                url=url,
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)

    evidence = fetch_search_evidence("truth table", domain="logic", search_url="https://search.example/search")

    assert evidence == []
    assert len(calls) == 4
    assert "engines=bing%2Cbaidu" in calls[2]


def test_fetch_search_evidence_filters_searx_space_ui_result_from_json(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Response:
        def __init__(self, text: str = "", *, url: str, content_type: str = "application/json", data: object | None = None) -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            if self._data is None:
                raise ValueError("not json")
            return self._data

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            if "format=json" in url:
                return _Response(
                    url=url,
                    data={
                        "results": [
                            {
                                "title": "https://searx.space",
                                "url": "https://searx.space",
                                "content": "My SearXNG About Preferences SearXNG clear search general images videos",
                            }
                        ]
                    },
                )
            return _Response("<html><body><a href=\"https://searx.space\">SearXNG</a></body></html>", url=url, content_type="text/html")

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)

    evidence = fetch_search_evidence("Lyman-alpha blob", domain="astrophysics", search_url="https://search.example/search")

    assert evidence == []


def test_fetch_search_evidence_falls_back_to_html_when_json_format_fails(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Response:
        def __init__(self, text: str, *, url: str, content_type: str = "text/html", fail: bool = False) -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}
            self.fail = fail

        def raise_for_status(self) -> None:
            if self.fail:
                raise RuntimeError("403 Forbidden")

        def json(self) -> object:
            raise ValueError("not json")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            if "format=json" in url:
                return _Response("", url=url, fail=True)
            return _Response(
                """
                <html><body>
                  <article class="result">
                    <h3><a href="https://example.com/wiki/lyman-alpha-blob">Lyman-alpha blob</a></h3>
                    <p>A large concentration of gas emitting the Lyman-alpha line.</p>
                  </article>
                </body></html>
                """,
                url=url,
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)

    evidence = fetch_search_evidence("Lyman-alpha blob", domain="astrophysics", search_url="https://search.example/search")

    assert evidence[0]["url"] == "https://example.com/wiki/lyman-alpha-blob"
    assert "Lyman-alpha" in evidence[0]["snippet"]


def test_fetch_search_evidence_retries_with_default_engines_when_default_search_is_empty(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[str] = []

    class _Response:
        def __init__(self, *, url: str, data: object) -> None:
            self.text = ""
            self.url = url
            self.headers = {"content-type": "application/json"}
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            return self._data

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            calls.append(url)
            if "engines=bing%2Cbaidu" in url:
                return _Response(
                    url=url,
                    data={
                        "results": [
                            {
                                "title": "VGA接口_百度百科",
                                "url": "https://baike.baidu.com/item/VGA%E6%8E%A5%E5%8F%A3/909309",
                                "content": "VGA 是一个使用模拟信号的电脑显示标准。",
                            }
                        ],
                        "unresponsive_engines": [],
                    },
                )
            return _Response(
                url=url,
                data={
                    "results": [],
                    "unresponsive_engines": [["google", "Suspended: CAPTCHA"], ["duckduckgo", "timeout"]],
                },
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)

    evidence = fetch_search_evidence("VGA", domain="display", search_url="https://search.example/search")

    assert len(calls) == 3
    assert "engines=bing%2Cbaidu" in calls[1]
    assert evidence[0]["title"] == "VGA接口_百度百科"
    assert "模拟信号" in evidence[0]["snippet"]


def test_fetch_search_evidence_does_not_fallback_when_llm_selects_no_urls(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    calls: list[str] = []

    class _Response:
        def __init__(self, text: str, *, url: str, content_type: str = "text/html") -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            raise ValueError("not json")

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            calls.append(url)
            if "search" in url:
                return _Response(
                    """
                    <html><body>
                      <article class="result">
                        <h3><a href="https://example.com/wiki/truth-table">Truth table</a></h3>
                        <p>Read more...</p>
                      </article>
                    </body></html>
                    """,
                    url=url,
                )
            return _Response("<html><body>should not fetch</body></html>", url=url)

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)
    monkeypatch.setattr(
        rag_module,
        "request_openai_json_object",
        lambda **_kwargs: {
            "summary_sufficient": False,
            "fetch_urls": [],
            "reason": "no useful URL selected",
            "confidence": 0.0,
        },
    )

    evidence = fetch_search_evidence(
        "truth table",
        domain="logic",
        search_url="https://search.example/search",
        context="come up with a truth table",
        target_lang="zh",
        config=OpenAIChatConfig(api_key="x", base_url="https://example.invalid/v1", model="demo"),
    )

    assert len(calls) == 1
    assert evidence[0]["url"] == "https://example.com/wiki/truth-table"
    assert evidence[0]["fetch_skipped"] == "not_selected_by_agent"


def test_fetch_search_evidence_merges_multiple_queries(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    class _Response:
        def __init__(self, text: str = "", *, url: str, content_type: str = "application/json", data: object | None = None) -> None:
            self.text = text
            self.url = url
            self.headers = {"content-type": content_type}
            self._data = data

        def raise_for_status(self) -> None:
            return None

        def json(self) -> object:
            if self._data is None:
                raise ValueError("not json")
            return self._data

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            return None

        def __enter__(self) -> "_Client":
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
            return False

        def get(self, url: str) -> _Response:
            if "search" in url and "awp+cs2" in url:
                return _Response(
                    url=url,
                    data={
                        "results": [
                            {"title": "AWP Wiki", "url": "https://example.com/wiki/awp", "content": "Counter-Strike sniper rifle."},
                        ]
                    },
                )
            if "search" in url:
                return _Response(
                    url=url,
                    data={
                        "results": [
                            {"title": "AWP duplicate", "url": "https://example.com/wiki/awp", "content": "Duplicate result."},
                            {"title": "Valve AWP", "url": "https://developer.valvesoftware.com/wiki/AWP", "content": "AWP weapon reference."},
                        ]
                    },
                )
            return _Response(
                """
                <html>
                  <head><title>AWP</title></head>
                  <body><main>AWP is a sniper rifle.</main></body>
                </html>
                """,
                url=url,
                content_type="text/html",
            )

    monkeypatch.setattr(rag_module.httpx, "Client", _Client)
    monkeypatch.setattr(rag_module, "_is_fetchable_url", lambda _url: True)

    evidence = fetch_search_evidence(
        "AWP",
        domain="CS2",
        search_url="https://search.example/search",
        queries=["awp cs2", "awp sniper rifle"],
    )

    assert [item["url"] for item in evidence] == [
        "https://example.com/wiki/awp",
        "https://developer.valvesoftware.com/wiki/AWP",
    ]


def test_rebuild_knowledge_embeddings_updates_current_model(monkeypatch) -> None:
    from videoroll.apps.subtitle_service import rag as rag_module

    monkeypatch.setattr(rag_module, "embed_text", lambda _text, *, settings: [0.1, 0.2])
    rag_cfg = rag_settings_from_translate_settings(
        {
            "rag_embedding_provider": "local",
            "rag_embedding_model": "embed-small",
            "rag_embedding_dimensions": 2,
        }
    )
    emb_cfg = embedding_settings_from_translate_settings(
        {
            "rag_embedding_provider": "local",
            "rag_embedding_model": "embed-small",
            "rag_embedding_dimensions": 2,
        }
    )
    db = _FakeKnowledgeDb(
        [
            _Row(
                id="00000000-0000-0000-0000-000000000001",
                item_type="term",
                term="AWP",
                translation="AWP 狙击枪",
                target_lang="zh",
                domain="CS2",
                aliases=["Magnum Sniper Rifle"],
                title="",
                content="",
                description="Counter-Strike weapon",
            )
        ]
    )

    result = rebuild_knowledge_embeddings(db, rag_settings=rag_cfg, embedding_settings=emb_cfg)  # type: ignore[arg-type]
    update_params = db.calls[-1][1]

    assert embedding_model_key(rag_cfg) == "local:embed-small"
    assert result["updated"] == 1
    assert result["failed"] == 0
    assert update_params["embedding_model"] == "local:embed-small"
    assert update_params["embedding"] == "[0.1,0.2]"
