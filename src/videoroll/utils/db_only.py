from __future__ import annotations

from typing import Any

from videoroll.utils.fernet import is_legacy_encrypted_token


def strip_legacy_encrypted_values(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        removed = 0
        for key, child in value.items():
            if key.endswith("_enc") and isinstance(child, str) and is_legacy_encrypted_token(child):
                removed += 1
                continue
            cleaned, child_removed = strip_legacy_encrypted_values(child)
            output[key] = cleaned
            removed += child_removed
        return output, removed
    if isinstance(value, list):
        output_list: list[Any] = []
        removed = 0
        for child in value:
            cleaned, child_removed = strip_legacy_encrypted_values(child)
            output_list.append(cleaned)
            removed += child_removed
        return output_list, removed
    return value, 0
