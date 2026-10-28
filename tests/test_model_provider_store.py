"""Model provider catalog and per-job selection regression tests."""
from __future__ import annotations

from typing import Any

import pytest

from videoroll.apps.subtitle_service import model_provider_store as store


class FakeDB:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.commits = 0

    def get(self, _model: Any, key: str) -> Any:
        return self.rows.get(key)

    def add(self, row: Any) -> None:
        self.rows[row.key] = row

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def legacy() -> dict[str, Any]:
    return {
        "openai_base_url": "https://old.example/v1", "openai_model": "old-model",
        "openai_api_key": "legacy-secret", "openai_api_key_set": True,
        "openai_temperature": 0.2, "openai_timeout_seconds": 180,
        "openai_max_retries": 3, "openai_api_type": "openai",
        "default_batch_size": 50, "default_style": "口语自然",
    }


def payload(**changes: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "name": "实验室 Ollama", "base_url": "http://localhost:11434/v1",
        "api_type": "openai", "api_key": "test-secret",
        "models": [
            {"id": "qwen:test", "context_window": 32768,
             "max_output_tokens": 2048, "temperature": 0.4}
        ],
    }
    values.update(changes)
    return values


def test_legacy_virtual_provider_and_catalog_do_not_expose_key(legacy: dict[str, Any]) -> None:
    db = FakeDB()
    rows = store.list_providers(db, legacy)  # type: ignore[arg-type]
    assert rows[0]["id"] == "legacy"
    assert rows[0]["models"][0]["id"] == "old-model"
    assert "api_key" not in rows[0]
    assert rows[1]["id"] == "index_translate"
    assert rows[1]["models"][0]["id"] == "Index-Translate-35B-A3B"
    assert "api_key" not in rows[1]
    assert db.commits == 0


def test_create_select_update_and_delete_provider(monkeypatch: pytest.MonkeyPatch, legacy: dict[str, Any]) -> None:
    monkeypatch.setattr(store, "encrypt_str", lambda s: "sealed:" + s)
    monkeypatch.setattr(store, "decrypt_str", lambda s: s.removeprefix("sealed:"))
    db = FakeDB()
    saved = store.save_provider(db, None, payload())  # type: ignore[arg-type]
    assert saved["api_key_set"] is True
    assert "api_key" not in saved and "api_key_enc" not in saved
    assert saved["models"][0]["context_window"] == 32768

    resolved = store.resolve_provider_settings(db, legacy, saved["id"], "qwen:test")  # type: ignore[arg-type]
    assert resolved["openai_model"] == "qwen:test"
    assert resolved["openai_api_key"] == "test-secret"
    assert resolved["openai_temperature"] == 0.4
    assert resolved["model_context_window"] == 32768
    assert resolved["max_output_tokens"] == 2048
    assert legacy["openai_model"] == "old-model"

    changed = payload(api_key=None, name="更名后")
    store.save_provider(db, saved["id"], changed)  # type: ignore[arg-type]
    resolved = store.resolve_provider_settings(db, legacy, saved["id"], "qwen:test")  # type: ignore[arg-type]
    assert resolved["openai_api_key"] == "test-secret"
    assert len(store.list_providers(db, legacy)) == 3  # type: ignore[arg-type]

    store.delete_provider(db, saved["id"])  # type: ignore[arg-type]
    assert len(store.list_providers(db, legacy)) == 2  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="已删除"):
        store.resolve_provider_settings(db, legacy, saved["id"], "qwen:test")  # type: ignore[arg-type]


def test_selection_fails_closed_instead_of_using_wrong_default(legacy: dict[str, Any]) -> None:
    db = FakeDB()
    with pytest.raises(ValueError, match="不存在"):
        store.resolve_provider_settings(db, legacy, "missing", "some-model")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="不一致"):
        store.resolve_provider_settings(db, legacy, "legacy", "changed")  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_model", [
    {"id": "x", "context_window": 0, "max_output_tokens": None, "temperature": 0.2},
    {"id": "x", "context_window": 8192, "max_output_tokens": 9000, "temperature": 0.2},
    {"id": "x", "context_window": 8192, "max_output_tokens": 1000, "temperature": 3},
])
def test_reject_invalid_model_settings(bad_model: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        store.save_provider(FakeDB(), None, payload(models=[bad_model]))  # type: ignore[arg-type]


def test_fetch_remote_ids_only(monkeypatch: pytest.MonkeyPatch) -> None:
    db = FakeDB()
    monkeypatch.setattr(store, "encrypt_str", lambda s: "sealed:" + s)
    monkeypatch.setattr(store, "decrypt_str", lambda s: s.removeprefix("sealed:"))
    provider = store.save_provider(db, None, payload())  # type: ignore[arg-type]
    captured: dict[str, Any] = {}

    class Response:
        def raise_for_status(self) -> None:
            pass
        def json(self) -> dict[str, Any]:
            return {"data": [{"id": "qwen3:test"}, {"id": "qwen3:test"}, {"id": "gpt-oss"}]}

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            captured["options"] = kwargs
        def __enter__(self) -> "Client":
            return self
        def __exit__(self, *_args: Any) -> None:
            pass
        def get(self, url: str, *, headers: dict[str, str]) -> Response:
            captured["url"] = url
            captured["headers"] = headers
            return Response()

    monkeypatch.setattr(store.httpx, "Client", Client)
    models = store.fetch_remote_models(db, provider["id"])  # type: ignore[arg-type]
    assert models == ["gpt-oss", "qwen3:test"]
    assert captured["url"] == "http://localhost:11434/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer test-secret"
    assert captured["options"]["follow_redirects"] is False



def test_index_builtin_cannot_be_deleted_or_overwritten() -> None:
    db = FakeDB()
    with pytest.raises(ValueError, match="内置提供方"):
        store.delete_provider(db, "index_translate")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="内置提供方"):
        store.save_provider(db, "index_translate", payload())  # type: ignore[arg-type]
    assert db.commits == 0



def test_long_context_output_256k_can_be_saved_and_resolved(monkeypatch: pytest.MonkeyPatch, legacy: dict[str, Any]) -> None:
    """The API's previous 250k cap rejected valid 1M/256k model configuration."""
    from videoroll.apps.subtitle_service.main import ModelEntryPayload

    entry = ModelEntryPayload(
        id="gpt-6-luna",
        context_window=1_000_000,
        max_output_tokens=256_000,
        temperature=1.0,
    )
    monkeypatch.setattr(store, "encrypt_str", lambda x: "sealed:" + x)
    monkeypatch.setattr(store, "decrypt_str", lambda x: x.removeprefix("sealed:"))
    db = FakeDB()
    provider = store.save_provider(db, None, payload(models=[entry.model_dump()]))  # type: ignore[arg-type]
    assert provider["models"][0]["max_output_tokens"] == 256_000
    resolved = store.resolve_provider_settings(db, legacy, provider["id"], entry.id)  # type: ignore[arg-type]
    assert resolved["max_output_tokens"] == 256_000
    assert resolved["model_context_window"] == 1_000_000


def test_output_limit_must_fit_model_context() -> None:
    from pydantic import ValidationError
    from videoroll.apps.subtitle_service.main import ModelEntryPayload

    # The limit is based on the model's own window, not on a frozen 4M cap.
    entry = ModelEntryPayload(id="long-context", context_window=10_000_000, max_output_tokens=9_999_999)
    assert entry.max_output_tokens == 9_999_999
    assert store._model(entry.model_dump())["max_output_tokens"] == 9_999_999
    with pytest.raises(ValidationError, match="小于上下文窗口"):
        ModelEntryPayload(id="invalid", context_window=256_000, max_output_tokens=256_000)
    with pytest.raises(ValueError, match="小于上下文窗口"):
        store.save_provider(
            FakeDB(), None,
            payload(models=[{"id": "invalid", "context_window": 256_000, "max_output_tokens": 256_000}]),
        )  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ModelEntryPayload(id="invalid", context_window=1_000_000, max_output_tokens=True)
    with pytest.raises(ValidationError):
        ModelEntryPayload(id="invalid", context_window=1_000_000, max_output_tokens=2.5)
    with pytest.raises(ValidationError):
        ModelEntryPayload(id="invalid", max_output_tokens=store.MAX_SAFE_JSON_INTEGER + 1)
    with pytest.raises(ValueError, match="正整数"):
        store._model({"id": "invalid", "context_window": False})
    assert ModelEntryPayload(id="unknown", context_window=None, max_output_tokens=None).max_output_tokens is None



def test_remote_model_metadata_is_optional_and_never_inferred() -> None:
    assert store._remote_model_details({"id": "arbitrary-1m-model"}) == {
        "context_window": None, "max_output_tokens": None,
    }
    assert store._remote_model_details({
        "id": "openrouter/test",
        "context_length": 1_000_000,
        "top_provider": {"context_length": 1_000_000, "max_completion_tokens": 256_000},
    }) == {"context_window": 1_000_000, "max_output_tokens": 256_000}
    assert store._remote_model_details({
        "id": "vllm/test", "max_model_len": 10_000_000, "max_output_tokens": 1_000_000,
    }) == {"context_window": 10_000_000, "max_output_tokens": 1_000_000}
    assert store._remote_model_details({
        "id": "bad-data", "context_length": 4096,
        "top_provider": {"max_completion_tokens": 8192},
    }) == {"context_window": 4096, "max_output_tokens": None}
    assert store._remote_model_details({
        "id": "bad-data", "context_length": True, "max_output_tokens": -1,
    }) == {"context_window": None, "max_output_tokens": None}


def test_remote_catalog_can_import_capacity_without_touching_user_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store, "encrypt_str", lambda x: "sealed:" + x)
    monkeypatch.setattr(store, "decrypt_str", lambda x: x.removeprefix("sealed:"))
    db = FakeDB()
    provider = store.save_provider(db, None, payload())
    original = db.get(store.AppSetting, store.KEY).value_json["providers"][0]["models"].copy()

    class Response:
        def raise_for_status(self) -> None:
            pass
        def json(self) -> dict[str, Any]:
            return {"data": [
                {"id": "large", "context_length": 10_000_000, "max_output_tokens": 1_000_000},
                {"id": "plain"},
                {"id": "large", "context_length": 2048},
            ]}
    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass
        def __enter__(self) -> "Client":
            return self
        def __exit__(self, *_args: Any) -> None:
            pass
        def get(self, *_args: Any, **_kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(store.httpx, "Client", Client)
    catalog = store.fetch_remote_model_catalog(db, provider["id"])  # type: ignore[arg-type]
    assert catalog == {"models": ["large", "plain"], "details": {
        "large": {"context_window": 10_000_000, "max_output_tokens": 1_000_000},
    }}
    assert db.get(store.AppSetting, store.KEY).value_json["providers"][0]["models"] == original
