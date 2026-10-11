from __future__ import annotations

from videoroll.utils.db_only import strip_legacy_encrypted_values


def test_strip_legacy_secret_fields_preserves_database_plaintext() -> None:
    value = {
        "openai": {
            "api_key_enc": "gAAAAABlegacy",
            "model": "qwen",
        },
        "rag_embedding_openai": {
            "api_key_enc": "dbplain:v1:new-key",
        },
        "plain": "keep-me",
    }

    cleaned, removed = strip_legacy_encrypted_values(value)

    assert removed == 1
    assert "api_key_enc" not in cleaned["openai"]
    assert cleaned["openai"]["model"] == "qwen"
    assert cleaned["rag_embedding_openai"]["api_key_enc"] == "dbplain:v1:new-key"
    assert cleaned["plain"] == "keep-me"


def test_strip_legacy_secret_fields_is_recursive_for_lists() -> None:
    value = {
        "profiles": [
            {"token_enc": "gAAAAABold"},
            {"token_enc": "dbplain:v1:still-valid"},
        ]
    }

    cleaned, removed = strip_legacy_encrypted_values(value)

    assert removed == 1
    assert cleaned["profiles"][0] == {}
    assert cleaned["profiles"][1]["token_enc"] == "dbplain:v1:still-valid"
