from __future__ import annotations

from videoroll.ai.client import OpenAIChatConfig
from videoroll.apps.subtitle_service.rag import (
    _fallback_search_queries,
    _search_url_with_params,
    build_knowledge_embedding_text,
    embedding_model_key,
    fetch_search_evidence,
    normalize_term,
    rag_settings_from_translate_settings,
    rebuild_knowledge_embeddings,
    search_knowledge,
    should_research_term,
)
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
            "rag_auto_discover_terms": True,
            "rag_auto_learn_terms": True,
            "rag_search_enabled": True,
            "rag_search_url": "https://search.example",
            "rag_domain": "CS2",
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
    assert cfg.search_enabled is True
    assert cfg.search_url == "https://search.example"
    assert cfg.domain == "CS2"


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
    assert len(calls) == 2


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
