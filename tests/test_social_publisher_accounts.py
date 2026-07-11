from __future__ import annotations

import json

import pytest

from videoroll.apps.social_publisher.account_store import canonicalize_storage_state, validate_account_name


def test_storage_state_is_canonicalized_without_losing_origins() -> None:
    raw = json.dumps({"origins": [{"origin": "https://example.com", "localStorage": []}], "cookies": []}).encode()
    assert json.loads(canonicalize_storage_state(raw)) == {
        "cookies": [],
        "origins": [{"localStorage": [], "origin": "https://example.com"}],
    }


def test_storage_state_requires_cookie_array() -> None:
    with pytest.raises(ValueError, match="cookies array"):
        canonicalize_storage_state(b'{"origins": []}')


@pytest.mark.parametrize("name", ["creator", "creator-2", "creator_xhs", "creator.test"])
def test_account_name_accepts_safe_identifiers(name: str) -> None:
    assert validate_account_name(name) == name


@pytest.mark.parametrize("name", ["", "../creator", "creator/name", "账号", "a" * 65])
def test_account_name_rejects_unsafe_values(name: str) -> None:
    with pytest.raises(ValueError, match="invalid account name"):
        validate_account_name(name)
