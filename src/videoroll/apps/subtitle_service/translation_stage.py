from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from videoroll.ai.client import is_retryable_openai_error, openai_chat_config_from_settings
from videoroll.ai.service import AIService
from videoroll.apps.subtitle_service.embeddings import embedding_settings_from_translate_settings
from videoroll.apps.subtitle_service.processing import (
    Segment,
    translate_segments_mock,
    translate_segments_openai_with_summary,
)
from videoroll.apps.subtitle_service.rag import build_rag_context, rag_settings_from_translate_settings
from videoroll.apps.subtitle_service.translation_checkpoint import TranslationCheckpointStore
from videoroll.apps.subtitle_service.translation_memory import (
    recall_translation_examples,
    remember_translation_pairs,
)
from videoroll.apps.subtitle_service.translation_trace import TranslationTraceRecorder
from videoroll.db.session import get_autocommit_sessionmaker


FreshTranslateSettings = Callable[[], dict[str, Any]]
AIServiceFactory = Callable[[], AIService]
LogLine = Callable[[str], None]


@dataclass(slots=True)
class TranslationStageResult:
    enabled: bool
    provider: str
    target_lang: str
    bilingual: bool
    segments: list[Segment]
    summary: str = ""
    style: str = ""
    ai_service: AIService | None = None


class TranslationRetryRequired(RuntimeError):
    def __init__(
        self,
        *,
        cause: Exception,
        retry_no: int,
        max_retries: int,
        countdown: float,
    ) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.retry_no = retry_no
        self.max_retries = max_retries
        self.countdown = countdown


def is_retryable_translation_error(error: Exception) -> bool:
    return is_retryable_openai_error(error)


def translation_retry_countdown(attempt: int) -> float:
    return min(30.0, float(2 ** max(0, int(attempt))))


def run_translation_stage(
    *,
    db: Session,
    task_id: str,
    subtitle_job_id: str,
    segments: list[Segment],
    source_segments_key: str | None,
    translate_cfg: dict[str, Any],
    checkpoint: TranslationCheckpointStore,
    trace: TranslationTraceRecorder,
    retry_attempt: int,
    database_url: str,
    fresh_translate_settings: FreshTranslateSettings,
    ai_service_factory: AIServiceFactory,
    log: LogLine,
) -> TranslationStageResult:
    enabled = bool(translate_cfg.get("enabled"))
    target_lang = str(translate_cfg.get("target_lang") or "zh").strip() or "zh"
    provider = str(translate_cfg.get("provider") or "mock").strip() or "mock"
    bilingual = bool(translate_cfg.get("bilingual"))
    if not enabled:
        return TranslationStageResult(
            enabled=False,
            provider=provider,
            target_lang=target_lang,
            bilingual=bilingual,
            segments=segments,
        )

    log(f"translate: provider={provider} target_lang={target_lang} bilingual={bilingual}")
    ai_service = ai_service_factory()
    translate_settings = fresh_translate_settings()
    default_style = str(translate_settings["default_style"])
    style = str(translate_cfg.get("style") or default_style).strip() or default_style
    batch_size = int(translate_cfg.get("batch_size") or translate_settings["default_batch_size"])
    enable_summary_value = translate_cfg.get("enable_summary")
    enable_summary = (
        bool(translate_settings["default_enable_summary"])
        if enable_summary_value is None
        else bool(enable_summary_value)
    )

    max_retries = max(0, int(translate_settings.get("default_max_retries") or 0))
    thinking_enabled = provider == "openai" and bool(translate_settings.get("openai_enable_thinking"))
    thinking_model = str(translate_settings.get("openai_model") or "").strip()
    session_run_id: str | None = None
    resumed_segments = 0
    batch_count = 0
    succeeded_batches = 0
    failed_batches = 0
    completed_segments = 0
    thought_characters = 0

    def flush_batch_thinking(batch_context: Any, *, force: bool = False) -> None:
        if not isinstance(batch_context, dict):
            return
        pending = str(batch_context.get("thinking_pending") or "")
        run_id = str(batch_context.get("run_id") or "")
        if not run_id or not pending:
            return
        last_flush_at = float(batch_context.get("thinking_last_flush_at") or 0.0)
        if not force and len(pending) < 800 and time.monotonic() - last_flush_at < 0.25:
            return
        batch_context["thinking_pending"] = ""
        batch_context["thinking_last_flush_at"] = time.monotonic()
        trace.append_batch_thinking_delta(
            run_id,
            model=thinking_model,
            delta=pending,
            batch_start=int(batch_context.get("segment_start") or 1),
            batch_size=int(batch_context.get("requested_segments") or 1),
            truncated=bool(batch_context.get("thinking_storage_limited")),
        )
        batch_context["thinking_stored_characters"] = (
            int(batch_context.get("thinking_stored_characters") or 0) + len(pending)
        )

    def on_batch_start(
        batch_segments: list[Segment],
        start_idx: int,
        summary: str,
    ) -> dict[str, Any]:
        nonlocal batch_count
        batch_count += 1
        batch_context: dict[str, Any] = {
            "run_id": None,
            "batch_number": batch_count,
            "segment_start": start_idx + 1,
            "requested_segments": len(batch_segments),
            "source_segments": list(batch_segments),
            "started_at": time.monotonic(),
            "thinking_pending": "",
            "thinking_last_flush_at": time.monotonic(),
            "thinking_received_characters": 0,
            "thinking_stored_characters": 0,
            "thinking_storage_limited": False,
            "finished": False,
        }
        if session_run_id:
            try:
                batch_context["run_id"] = trace.start_batch(
                    parent_session_run_id=session_run_id,
                    task_id=task_id,
                    subtitle_job_id=subtitle_job_id,
                    target_lang=target_lang,
                    model=thinking_model,
                    batch_number=batch_count,
                    segment_start=start_idx + 1,
                    source_segments=batch_segments,
                    previous_summary=summary,
                    thinking_enabled=thinking_enabled,
                )
            except Exception as trace_error:
                db.rollback()
                log(f"translate batch trace unavailable: {type(trace_error).__name__}: {trace_error}")
        return batch_context

    def on_thinking(
        delta: str,
        batch_start: int,
        batch_size_value: int,
        batch_context: Any,
    ) -> None:
        nonlocal thought_characters
        del batch_start, batch_size_value
        if not isinstance(batch_context, dict):
            return
        clean_delta = str(delta or "")
        if not clean_delta:
            return
        thought_characters += len(clean_delta)
        batch_context["thinking_received_characters"] = (
            int(batch_context.get("thinking_received_characters") or 0) + len(clean_delta)
        )
        max_stored_characters = 32_000
        stored = int(batch_context.get("thinking_stored_characters") or 0)
        pending = str(batch_context.get("thinking_pending") or "")
        remaining = max_stored_characters - stored - len(pending)
        if remaining <= 0:
            batch_context["thinking_storage_limited"] = True
            return
        if len(clean_delta) > remaining:
            clean_delta = clean_delta[:remaining]
            batch_context["thinking_storage_limited"] = True
        batch_context["thinking_pending"] = pending + clean_delta
        flush_batch_thinking(batch_context)

    try:
        if provider == "mock":
            segments_out = translate_segments_mock(segments, target_lang=target_lang)
            translation_summary = ""
        elif provider in {"noop", "none"}:
            segments_out = segments
            translation_summary = ""
        elif provider == "openai":
            resume_prefix, resume_summary = checkpoint.load(
                segments,
                source_segments_key=source_segments_key,
            )
            resumed_segments = len(resume_prefix)
            completed_segments = len(resume_prefix)
            if resume_prefix:
                log(f"translate: resuming from checkpoint at segment {len(resume_prefix)}/{len(segments)}")

            try:
                session_run_id = trace.start_session(
                    task_id=task_id,
                    subtitle_job_id=subtitle_job_id,
                    target_lang=target_lang,
                    model=thinking_model,
                    segment_count=len(segments),
                    resumed_segments=len(resume_prefix),
                    retry_attempt=retry_attempt,
                    thinking_enabled=thinking_enabled,
                )
            except Exception as trace_error:
                db.rollback()
                log(f"translate session trace unavailable: {type(trace_error).__name__}: {trace_error}")

            checkpoint_segments = list(resume_prefix)
            initial_rag_settings = rag_settings_from_translate_settings(translate_settings)
            if initial_rag_settings.enabled:
                log(
                    "translate rag: "
                    f"enabled top_k={initial_rag_settings.top_k} min_score={initial_rag_settings.min_score} "
                    f"embedding_provider={initial_rag_settings.embedding_provider} "
                    f"embedding_model={initial_rag_settings.embedding_model} "
                    f"domain={initial_rag_settings.domain or '(any)'}"
                )

            def rag_context_provider(
                batch_segments: list[Segment],
                start_idx: int,
                summary: str,
                batch_context: Any,
            ) -> dict[str, Any] | None:
                current_settings = fresh_translate_settings()
                current_rag_settings = rag_settings_from_translate_settings(current_settings)
                if isinstance(batch_context, dict):
                    batch_context["translation_domain"] = current_rag_settings.domain

                translation_examples: list[dict[str, Any]] = []
                try:
                    translation_examples = recall_translation_examples(
                        db,
                        source_segments=batch_segments,
                        start_idx=start_idx,
                        target_lang=target_lang,
                        task_id=task_id,
                        domain=current_rag_settings.domain,
                    )
                except (AttributeError, NotImplementedError):
                    # Lightweight test/fallback sessions may not provide SQL
                    # execution. Translation memory is an optimization, not a
                    # prerequisite for translation.
                    translation_examples = []
                except Exception as memory_error:
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    log(f"translate memory recall unavailable: {type(memory_error).__name__}: {memory_error}")

                # Translation-memory reads above use the job session. End that
                # transaction before any RAG agent performs web/LLM I/O so the
                # job session cannot sit idle-in-transaction while waiting on
                # the network.
                try:
                    db.commit()
                except Exception:
                    db.rollback()

                payload: dict[str, Any] = {}
                if current_rag_settings.enabled:
                    rag_session_factory = get_autocommit_sessionmaker(database_url)
                    rag_db = rag_session_factory()
                    try:
                        ctx = build_rag_context(
                            rag_db,
                            segments=batch_segments,
                            target_lang=target_lang,
                            rag_settings=current_rag_settings,
                            embedding_settings=embedding_settings_from_translate_settings(current_settings),
                            chat_config=openai_chat_config_from_settings(current_settings),
                            previous_summary=summary,
                            session_factory=rag_session_factory,
                            task_id=task_id,
                            subtitle_job_id=subtitle_job_id,
                            parent_agent_run_id=(
                                str(batch_context.get("run_id") or "")
                                if isinstance(batch_context, dict) and batch_context.get("run_id")
                                else None
                            ),
                        )
                    finally:
                        rag_db.close()
                    if ctx.term_cards:
                        payload["term_cards"] = ctx.term_cards
                    if ctx.knowledge_cards:
                        payload["knowledge_cards"] = ctx.knowledge_cards
                if translation_examples:
                    payload["translation_examples"] = translation_examples
                return payload or None

            def on_batch_done(
                batch_context: Any,
                batch_segments: list[Segment],
                updated_summary: str,
                completed_count: int,
            ) -> None:
                nonlocal completed_segments, succeeded_batches
                checkpoint_segments.extend(batch_segments)
                checkpoint.save(
                    source_segments_key,
                    checkpoint_segments,
                    summary=updated_summary,
                )
                completed_segments = completed_count
                succeeded_batches += 1
                flush_batch_thinking(batch_context, force=True)
                if isinstance(batch_context, dict):
                    trace.finish_batch(
                        str(batch_context.get("run_id") or "") or None,
                        parent_session_run_id=session_run_id,
                        status=(
                            "succeeded"
                            if len(batch_segments) == int(batch_context.get("requested_segments") or 0)
                            else "partial"
                        ),
                        batch_number=int(batch_context.get("batch_number") or 1),
                        segment_start=int(batch_context.get("segment_start") or 1),
                        requested_segments=int(batch_context.get("requested_segments") or len(batch_segments)),
                        translated_segments=batch_segments,
                        completed_segments=completed_count,
                        updated_summary=updated_summary,
                        thought_characters=int(batch_context.get("thinking_received_characters") or 0),
                        thought_truncated=bool(batch_context.get("thinking_storage_limited")),
                        duration_ms=int(
                            (
                                time.monotonic()
                                - float(batch_context.get("started_at") or time.monotonic())
                            )
                            * 1000
                        ),
                    )
                    batch_context["finished"] = True
                    source_segments = batch_context.get("source_segments")
                    if isinstance(source_segments, list) and source_segments:
                        try:
                            written = remember_translation_pairs(
                                db,
                                source_segments=[
                                    segment
                                    for segment in source_segments[: len(batch_segments)]
                                    if isinstance(segment, Segment)
                                ],
                                translated_segments=batch_segments,
                                target_lang=target_lang,
                                task_id=task_id,
                                subtitle_job_id=subtitle_job_id,
                                domain=str(batch_context.get("translation_domain") or ""),
                            )
                            if written:
                                db.commit()
                        except (AttributeError, NotImplementedError):
                            pass
                        except Exception as memory_error:
                            try:
                                db.rollback()
                            except Exception:
                                pass
                            log(
                                "translate memory write unavailable: "
                                f"{type(memory_error).__name__}: {memory_error}"
                            )

            def on_batch_error(batch_context: Any, error: Exception) -> None:
                nonlocal failed_batches
                if not isinstance(batch_context, dict) or bool(batch_context.get("finished")):
                    return
                failed_batches += 1
                flush_batch_thinking(batch_context, force=True)
                trace.finish_batch(
                    str(batch_context.get("run_id") or "") or None,
                    parent_session_run_id=session_run_id,
                    status="failed",
                    batch_number=int(batch_context.get("batch_number") or 1),
                    segment_start=int(batch_context.get("segment_start") or 1),
                    requested_segments=int(batch_context.get("requested_segments") or 0),
                    completed_segments=completed_segments,
                    thought_characters=int(batch_context.get("thinking_received_characters") or 0),
                    thought_truncated=bool(batch_context.get("thinking_storage_limited")),
                    duration_ms=int(
                        (
                            time.monotonic()
                            - float(batch_context.get("started_at") or time.monotonic())
                        )
                        * 1000
                    ),
                    error=str(error),
                )
                batch_context["finished"] = True

            segments_out, translation_summary = translate_segments_openai_with_summary(
                segments,
                target_lang=target_lang,
                style=style,
                api_key=None,
                base_url="",
                model="",
                temperature=translate_settings["openai_temperature"],
                timeout_seconds=translate_settings["openai_timeout_seconds"],
                batch_size=batch_size,
                enable_summary=enable_summary,
                resume_from=resume_prefix,
                initial_summary=resume_summary,
                ai_service=ai_service,
                enable_thinking=thinking_enabled,
                on_batch_start=on_batch_start,
                rag_context_provider_with_context=rag_context_provider,
                on_batch_done_with_context=on_batch_done,
                on_batch_error=on_batch_error,
                on_thinking_delta_with_context=on_thinking if thinking_enabled else None,
            )
        else:
            raise ValueError(f"unsupported translate provider: {provider}")

        completed_segments = len(segments_out)
        if session_run_id:
            trace.finish_session(
                session_run_id,
                status="succeeded",
                total_segments=len(segments),
                completed_segments=completed_segments,
                resumed_segments=resumed_segments,
                batch_count=batch_count,
                succeeded_batches=succeeded_batches,
                failed_batches=failed_batches,
                thought_characters=thought_characters,
            )
        return TranslationStageResult(
            enabled=True,
            provider=provider,
            target_lang=target_lang,
            bilingual=bilingual,
            segments=segments_out,
            summary=translation_summary,
            style=style,
            ai_service=ai_service,
        )
    except Exception as error:
        if session_run_id:
            trace.finish_session(
                session_run_id,
                status="failed",
                total_segments=len(segments),
                completed_segments=completed_segments,
                resumed_segments=resumed_segments,
                batch_count=batch_count,
                succeeded_batches=succeeded_batches,
                failed_batches=failed_batches,
                thought_characters=thought_characters,
                error=str(error),
            )
        retry_no = max(0, int(retry_attempt)) + 1
        if provider != "openai" or retry_no > max_retries or not is_retryable_translation_error(error):
            raise
        raise TranslationRetryRequired(
            cause=error,
            retry_no=retry_no,
            max_retries=max_retries,
            countdown=translation_retry_countdown(retry_no),
        ) from error
