from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, Iterable, TypeVar


THit = TypeVar("THit")


@dataclass(frozen=True)
class RetrievalStage:
    name: str
    query: str = ""
    count: int = 0
    duration_ms: int = 0
    error_type: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error_type

    def to_trace_step(self) -> dict[str, Any]:
        step: dict[str, Any] = {
            "kind": "retrieval",
            "action": self.name,
            "status": "ok" if self.ok else "failed",
            "query": self.query,
            "count": self.count,
            "duration_ms": self.duration_ms,
        }
        if self.error_type:
            step["error_type"] = self.error_type
        if self.error:
            step["error"] = self.error
        return step


@dataclass
class RetrievalResult(Generic[THit]):
    hits: list[THit] = field(default_factory=list)
    stages: list[RetrievalStage] = field(default_factory=list)


def _duration_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def merge_hits_by_id(items: Iterable[THit], *, id_getter: Callable[[THit], str]) -> list[THit]:
    out: list[THit] = []
    seen: set[str] = set()
    for item in items:
        key = id_getter(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


class RetrievalPipeline(Generic[THit]):
    """Small explicit retrieval pipeline inspired by RAG frameworks.

    The pipeline owns stage timing, dedupe, and trace events. The actual database
    operations stay injected so the subtitle service can keep its current storage
    layer and avoid a heavy framework dependency.
    """

    def __init__(
        self,
        *,
        id_getter: Callable[[THit], str],
        trace_recorder: Callable[[dict[str, Any]], None] | None = None,
        error_handler: Callable[[Exception], None] | None = None,
    ) -> None:
        self._id_getter = id_getter
        self._trace_recorder = trace_recorder
        self._error_handler = error_handler
        self._hits: list[THit] = []
        self._seen_ids: set[str] = set()
        self.stages: list[RetrievalStage] = []

    @property
    def hits(self) -> list[THit]:
        return list(self._hits)

    def add_hits(self, hits: Iterable[THit]) -> int:
        added = 0
        for hit in hits:
            key = self._id_getter(hit)
            if not key or key in self._seen_ids:
                continue
            self._seen_ids.add(key)
            self._hits.append(hit)
            added += 1
        return added

    def run_stage(self, name: str, query: str, fn: Callable[[], list[THit]]) -> list[THit]:
        started = time.perf_counter()
        try:
            hits = fn()
            added = self.add_hits(hits)
            stage = RetrievalStage(name=name, query=query, count=added, duration_ms=_duration_ms(started))
        except Exception as e:
            if self._error_handler is not None:
                try:
                    self._error_handler(e)
                except Exception:
                    pass
            hits = []
            stage = RetrievalStage(
                name=name,
                query=query,
                count=0,
                duration_ms=_duration_ms(started),
                error_type=type(e).__name__,
                error=str(e)[:500],
            )
        self.stages.append(stage)
        if self._trace_recorder is not None:
            self._trace_recorder(stage.to_trace_step())
        return hits

    def result(self) -> RetrievalResult[THit]:
        return RetrievalResult(hits=self.hits, stages=list(self.stages))
