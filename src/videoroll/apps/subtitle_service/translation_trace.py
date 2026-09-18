from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sqlalchemy.orm import Session

from videoroll.apps.subtitle_service.processing import Segment


StartRun = Callable[..., str]
AppendStep = Callable[[Session, str | None, dict[str, Any]], None]
FinishRun = Callable[..., None]


def _trace_blocks(
    segments: Iterable[Segment],
    *,
    start_index: int,
    character_limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for offset, segment in enumerate(segments):
        text_value = str(segment.text or "")
        remaining = max(0, int(character_limit) - used)
        if remaining <= 0:
            truncated = True
            break
        if len(text_value) > remaining:
            text_value = text_value[:remaining]
            truncated = True
        rows.append({"idx": int(start_index) + offset, "text": text_value})
        used += len(text_value)
    return rows, truncated


@dataclass(slots=True)
class TranslationTraceRecorder:
    """Stable translation telemetry facade over the generic agent-run store."""

    db: Session
    start_run: StartRun
    append_step: AppendStep
    finish_run: FinishRun

    def start_thinking_run(
        self,
        *,
        task_id: str,
        subtitle_job_id: str,
        target_lang: str,
        model: str,
        segment_count: int,
    ) -> str:
        run_id = self.start_run(
            self.db,
            agent_type="subtitle_translation_thinking",
            term="字幕翻译 Think",
            domain="subtitle_translation",
            target_lang=target_lang,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
            query=f"{segment_count} segments · {model or 'OpenAI-compatible model'}",
        )
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "llm",
                "action": "translation_thinking.started",
                "status": "running",
                "model": model,
                "input": {"segment_count": max(0, int(segment_count)), "thinking": True, "stream": True},
                "metadata": {"thinking": True, "stream": True},
            },
        )
        return run_id

    def append_thinking_delta(
        self,
        run_id: str | None,
        *,
        model: str,
        delta: str,
        batch_start: int,
        batch_size: int,
        truncated: bool = False,
    ) -> None:
        clean_delta = str(delta or "")
        if not clean_delta:
            return
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "llm",
                "action": "translation_thinking.delta",
                "status": "running",
                "model": model,
                "input": {"batch_start": max(1, int(batch_start)), "batch_size": max(1, int(batch_size))},
                "output": {"thinking_delta": clean_delta[:8000]},
                "metadata": {"thinking": True, "stream": True, "truncated": bool(truncated)},
            },
        )

    def finish_thinking_run(
        self,
        run_id: str | None,
        *,
        status: str,
        completed_segments: int,
        thought_characters: int,
        error: str = "",
    ) -> None:
        self.finish_run(
            self.db,
            run_id,
            status=status,
            error=error,
            result={
                "completed_segments": max(0, int(completed_segments)),
                "thought_characters": max(0, int(thought_characters)),
                "thinking": True,
            },
        )

    def start_session(
        self,
        *,
        task_id: str,
        subtitle_job_id: str,
        target_lang: str,
        model: str,
        segment_count: int,
        resumed_segments: int = 0,
        retry_attempt: int = 0,
        thinking_enabled: bool = False,
    ) -> str:
        run_id = self.start_run(
            self.db,
            agent_type="subtitle_translation_session",
            term=f"字幕翻译 Session · {max(0, int(segment_count))} 段",
            domain="subtitle_translation",
            target_lang=target_lang,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
            query=f"{max(0, int(segment_count))} segments · {model or 'OpenAI-compatible model'}",
        )
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "agent",
                "action": "translation_session.started",
                "status": "running",
                "model": model,
                "input": {
                    "segment_count": max(0, int(segment_count)),
                    "resumed_segments": max(0, int(resumed_segments)),
                    "retry_attempt": max(0, int(retry_attempt)),
                    "thinking": bool(thinking_enabled),
                },
                "metadata": {"stream": bool(thinking_enabled), "thinking": bool(thinking_enabled)},
            },
        )
        return run_id

    def start_batch(
        self,
        *,
        parent_session_run_id: str,
        task_id: str,
        subtitle_job_id: str,
        target_lang: str,
        model: str,
        batch_number: int,
        segment_start: int,
        source_segments: list[Segment],
        previous_summary: str = "",
        thinking_enabled: bool = False,
    ) -> str:
        segment_count = len(source_segments)
        segment_end = max(int(segment_start), int(segment_start) + max(0, segment_count) - 1)
        run_id = self.start_run(
            self.db,
            agent_type="subtitle_translation_batch",
            term=f"Batch {max(1, int(batch_number))} · 字幕 {max(1, int(segment_start))}–{segment_end}",
            domain="subtitle_translation",
            target_lang=target_lang,
            task_id=task_id,
            subtitle_job_id=subtitle_job_id,
            query=f"{segment_count} segments · {model or 'OpenAI-compatible model'}",
            parent_agent_run_id=parent_session_run_id,
        )
        source_blocks, source_truncated = _trace_blocks(
            source_segments,
            start_index=max(1, int(segment_start)),
            character_limit=12_000,
        )
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "agent",
                "action": "translation_batch.started",
                "status": "running",
                "model": model,
                "input": {
                    "batch_number": max(1, int(batch_number)),
                    "segment_start": max(1, int(segment_start)),
                    "segment_end": segment_end,
                    "segment_count": segment_count,
                    "source_blocks": source_blocks,
                    "previous_summary": str(previous_summary or "")[:500],
                },
                "metadata": {
                    "thinking": bool(thinking_enabled),
                    "stream": bool(thinking_enabled),
                    "source_truncated": source_truncated,
                },
            },
        )
        self.append_step(
            self.db,
            parent_session_run_id,
            {
                "kind": "agent",
                "action": "translation_session.batch_started",
                "status": "running",
                "input": {
                    "batch_run_id": run_id,
                    "batch_number": max(1, int(batch_number)),
                    "segment_start": max(1, int(segment_start)),
                    "segment_end": segment_end,
                },
            },
        )
        return run_id

    def append_batch_thinking_delta(
        self,
        run_id: str | None,
        *,
        model: str,
        delta: str,
        batch_start: int,
        batch_size: int,
        truncated: bool = False,
    ) -> None:
        self.append_thinking_delta(
            run_id,
            model=model,
            delta=delta,
            batch_start=batch_start,
            batch_size=batch_size,
            truncated=truncated,
        )

    def finish_batch(
        self,
        run_id: str | None,
        *,
        parent_session_run_id: str | None,
        status: str,
        batch_number: int,
        segment_start: int,
        requested_segments: int,
        translated_segments: list[Segment] | None = None,
        completed_segments: int = 0,
        updated_summary: str = "",
        thought_characters: int = 0,
        thought_truncated: bool = False,
        duration_ms: int | None = None,
        error: str = "",
    ) -> None:
        translated = list(translated_segments or [])
        translation_blocks, translation_truncated = _trace_blocks(
            translated,
            start_index=max(1, int(segment_start)),
            character_limit=32_000,
        )
        action = "translation_batch.completed" if status == "succeeded" else "translation_batch.failed"
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "llm",
                "action": action,
                "status": "ok" if status == "succeeded" else "failed",
                "duration_ms": duration_ms,
                "output": {
                    "translations": translation_blocks,
                    "updated_summary": str(updated_summary or "")[:500],
                    "completed_segments": max(0, int(completed_segments)),
                },
                "error": str(error or "")[:1000],
                "metadata": {
                    "requested_segments": max(0, int(requested_segments)),
                    "translated_segments": len(translated),
                    "translation_truncated": translation_truncated,
                    "thought_characters": max(0, int(thought_characters)),
                    "thought_truncated": bool(thought_truncated),
                },
            },
        )
        self.finish_run(
            self.db,
            run_id,
            status=status,
            error=error,
            result={
                "batch_number": max(1, int(batch_number)),
                "segment_start": max(1, int(segment_start)),
                "segment_end": max(0, int(segment_start) + len(translated) - 1),
                "requested_segments": max(0, int(requested_segments)),
                "translated_segments": len(translated),
                "completed_segments": max(0, int(completed_segments)),
                "thought_characters": max(0, int(thought_characters)),
                "thought_truncated": bool(thought_truncated),
                "updated_summary": str(updated_summary or "")[:500],
            },
        )
        self.append_step(
            self.db,
            parent_session_run_id,
            {
                "kind": "agent",
                "action": "translation_session.batch_completed" if status == "succeeded" else "translation_session.batch_failed",
                "status": "ok" if status == "succeeded" else "failed",
                "output": {
                    "batch_run_id": run_id,
                    "batch_number": max(1, int(batch_number)),
                    "completed_segments": max(0, int(completed_segments)),
                    "translated_segments": len(translated),
                },
                "error": str(error or "")[:1000],
            },
        )

    def finish_session(
        self,
        run_id: str | None,
        *,
        status: str,
        total_segments: int,
        completed_segments: int,
        resumed_segments: int,
        batch_count: int,
        succeeded_batches: int,
        failed_batches: int,
        thought_characters: int,
        error: str = "",
    ) -> None:
        self.append_step(
            self.db,
            run_id,
            {
                "kind": "agent",
                "action": "translation_session.completed" if status == "succeeded" else "translation_session.failed",
                "status": "ok" if status == "succeeded" else "failed",
                "output": {
                    "total_segments": max(0, int(total_segments)),
                    "completed_segments": max(0, int(completed_segments)),
                    "batch_count": max(0, int(batch_count)),
                    "succeeded_batches": max(0, int(succeeded_batches)),
                    "failed_batches": max(0, int(failed_batches)),
                    "thought_characters": max(0, int(thought_characters)),
                },
                "error": str(error or "")[:1000],
            },
        )
        self.finish_run(
            self.db,
            run_id,
            status=status,
            error=error,
            result={
                "total_segments": max(0, int(total_segments)),
                "completed_segments": max(0, int(completed_segments)),
                "resumed_segments": max(0, int(resumed_segments)),
                "batch_count": max(0, int(batch_count)),
                "succeeded_batches": max(0, int(succeeded_batches)),
                "failed_batches": max(0, int(failed_batches)),
                "thought_characters": max(0, int(thought_characters)),
            },
        )
