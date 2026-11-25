# Cloudflare Workers AI native workflow (2026-11-25)

Original subtitle.asr default engine/model, nested account ID/model/api_key_enc and original environment fallback. Explicit manual engine/model supported. Official HTTPS API with strictly opt-in loopback test override, redirects disabled for Cloudflare. No Python ASR service, Redis, or new database table. 30-second non-overlapping mono PCM16 16kHz WAV chunks, bounded base64 request; original language, segment/word/text response shapes, actual chunk offsets, time ranges clamped to chunk bounds. Existing durable checkpoints include model, endpoint, media digest, language, chunk geometry and engine identity. Lease checks before/after provider calls and before checkpoint/final asset writes. Original assets table stores subtitle output.

CLOUDFLARE_ASR_MAX_CONCURRENCY (default 2, bounded 1..8) uses PostgreSQL transaction-scoped advisory lock slots shared across Rust processes. Set the same cap for all workers. Admission deadline 120s plus DB acquisition overhead; no Redis rate gate, no global cooldown or Retry-After support yet. Request timeout 180s, existing 3-attempt chunk retry; selected permanent HTTP errors and invalid responses stop early. This is not a complete Python provider retry parity; configuration test endpoint is separate from workflow admission. Holding a transaction during HTTP requires adequate pool capacity and no premature server idle transaction timeout. Other ASR engines retain existing behavior.

Validation:
- all-target check passed.
- isolated actual Rust worker test with two independent worker processes, two synthetic 65s videos, original provider settings, loopback mock API, 30+30+5s WAV, original language/model/Bearer, four injected 429 responses, word-span and text fallback with exact SRT offsets, max concurrent calls 1, no publish jobs. Initial final assertion counted unrelated scheduler advisory locks; narrowed to actual provider namespace and rerun passed.
- unit test covers segments, numeric timestamp strings, words, text fallback, bounded end, provider failure and silence.
- No production changes, real Cloudflare calls, deployment, push, or full 217 API business equivalence verification. Long-video checkpoint recovery after process kill, real provider response variants and original global rate cooldown need further tests. Translation context/memory and advanced RAG not part of this change.

Logs: target/cloudflare-asr-check.log, cloudflare-asr-worker-r2.log, cloudflare-asr-lib.log.
