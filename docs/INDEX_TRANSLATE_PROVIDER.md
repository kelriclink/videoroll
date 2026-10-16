# VideoRoll: isolated Index-Translate public API provider

## How this provider works
VideoRoll adds an opt-in Index-Translate subtitle translator separate from OpenAI, RAG and
native tool-calling agents. The automatic runtime box reads current translate profile at
translation stage entry; manual subtitle tasks preserve the selected provider.

The Index provider does not invoke a different LLM or RAG for subtitle translation,
title translation or fallback tags. Other independently configured publishing actions
(e.g. classification/review) are outside the translation-provider scope.

## Official public API
Base URL: https://index-translate.bilibili.com/v1
Model: Index-Translate-35B-A3B
Path: POST /chat/completions
HTTP User-Agent: Index-Translate-Client/1.0
No real API key is required. Thinking disabled, default temperature zero.
This public free endpoint has no assured long-term availability or throughput.

## Dedicated settings (separate from OpenAI)
Provider ID: index_translate
index_temperature: 0
index_batch_size: 12 source cues/request
index_max_tokens: 4096 tokens/request
index_timeout_seconds: 120
index_context_chars: 6000 (public 16,384-token input limit; requests are capped at 6000 chars)
index_domain: 视频字幕
index_glossary: fixed terminology (hard constraint)
index_instruction: freeform style/context notes (soft constraint)

Index settings persist under the existing subtitle.translate AppSetting record, using
dedicated index_* keys. General OpenAI API credentials/settings are untouched.

## Original transcript as context
For each batch, the source transcript is provided as soft reference context when
it fits the configured character limit; only selected subtitle cues should be translated.
For longer videos a head/current neighborhood/tail excerpt is supplied instead and
truncation is logged. Original cue indices and timestamps are preserved, JSON output
is validated, and malformed batches are subdivided. Successful batches are checkpointed
and labeled with provider ID, so an old OpenAI checkpoint cannot pollute Index output.

## Title translation and fallback
The corresponding title translation uses the same Index endpoint with a small video
context excerpt, requires a single JSON title field, and rejects multiline/overlong
outputs; if malformed it retries without context. If both fail, existing best-effort
title metadata handling preserves the source title instead of using other LLMs.

The optional Index path cannot perform general OpenAI Tool Calling. Do not send
sensitive/private transcripts to this external public endpoint.

## Safe deployment
VideoRoll native release updates are guarded against switching while queued/active
pipeline work is pending. Do not override the idle check or cancel jobs merely to
activate this provider. Stage a verified build; deploy when the system is safely idle.
