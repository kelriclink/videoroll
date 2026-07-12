from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PublishAllResult:
    """多平台投稿的结果汇总。"""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(
            r.get("status") == "ok" for r in self.results.values()
        )

    @property
    def has_any_ok(self) -> bool:
        return any(r.get("status") == "ok" for r in self.results.values())

    @property
    def errors(self) -> dict[str, str]:
        return {
            platform: str(r.get("detail") or r.get("error") or "unknown")
            for platform, r in self.results.items()
            if r.get("status") != "ok"
        }

    @property
    def platform_count(self) -> int:
        return len(self.results)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results.values() if r.get("status") == "ok")

    @property
    def error_count(self) -> int:
        return self.platform_count - self.ok_count
