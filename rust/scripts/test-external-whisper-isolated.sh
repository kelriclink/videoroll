#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Build first so the disposable database is not kept open during compilation.
case "${VIDEOROLL_TEST_ASR_SCENARIO:-external-whisper}" in
  external-whisper) test_name=media::asr::tests::external_whisper_real_postgres_http_admission ;;
  translation) test_name=media::asr::tests::translation_recovery_real_postgres_http ;;
  live-thinking) test_name=media::translation_trace::tests::live_thinking_real_postgres_http ;;
  thinking) test_name=media::asr::tests::thinking_real_postgres_http ;;
  collection) test_name=bilibili::collection::tests::collection_real_postgres_http ;;
  download) test_name=download_runtime::tests::download_fallback_real_postgres_and_process ;;
  bili-account-binding) test_name=bilibili_account_v1::tests::confirmed_account_real_postgres ;;
  publish-gate) test_name=bilibili::tests::publish_gate_real_postgres ;;
  rag-shared) test_name=agent::provider_cache::tests::shared_cache_real_postgres ;;
  dub-handoff) test_name=media::dubbing_handoff::tests::audio_only_real_postgres_http ;;
  title) test_name=publish_meta::tests::title_translation_real_postgres_http ;;
  *) echo "invalid test scenario" >&2; exit 1 ;;
esac
cargo test --lib "$test_name" --no-run
name="vr2-whisper-test-$(python3 -c 'import uuid;print(uuid.uuid4().hex[:12])')"
volume="$name-db"
tmp="$(mktemp -d)"
cleanup() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  rm -rf -- "$tmp"
  docker volume rm "$volume" >/dev/null
}
docker volume create --label io.videoroll.purpose=isolated-whisper "$volume" >/dev/null
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker run -d --rm --pull=never --name "$name" --memory=512m \
  --mount "type=volume,source=$volume,target=/var/lib/postgresql" \
  -p 127.0.0.1::5432 -e POSTGRES_PASSWORD=videoroll-test-only \
  -e POSTGRES_DB=videoroll_test pgvector/pgvector:pg18 \
  -c idle_in_transaction_session_timeout=100ms >/dev/null
for i in $(seq 1 60); do
  if docker logs "$name" 2>&1 | grep -q 'PostgreSQL init process complete; ready for start up' &&
     docker exec "$name" pg_isready -U postgres -d videoroll_test >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "$name" psql -At -U postgres -d videoroll_test -c 'SELECT 1' | grep -qx 1
port="$(docker port "$name" 5432/tcp | awk -F: '/^127.0.0.1:/ {print $NF}')"
[[ "$port" =~ ^[0-9]+$ ]]
export VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL="postgresql://postgres:videoroll-test-only@127.0.0.1:$port/videoroll_test"
export DATABASE_URL="$VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL"
export VIDEOROLL_V2_STORAGE_ROOT="$tmp" VIDEOROLL_V2_INTERNAL_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
case "${VIDEOROLL_TEST_ASR_SCENARIO:-external-whisper}" in
  translation) export VIDEOROLL_TEST_TRANSLATION_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  live-thinking) export VIDEOROLL_TEST_LIVE_THINKING_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  thinking) export VIDEOROLL_TEST_THINKING_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  collection) export VIDEOROLL_TEST_COLLECTION_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  download) export VIDEOROLL_TEST_DOWNLOAD_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  bili-account-binding) export VIDEOROLL_TEST_BILI_ACCOUNT_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  publish-gate) export VIDEOROLL_TEST_PUBLISH_GATE_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  rag-shared) export VIDEOROLL_TEST_RAG_SHARED_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  dub-handoff) export VIDEOROLL_TEST_DUB_HANDOFF_DB_URL="$DATABASE_URL" VIDEOROLL_V2_YOUDUB_AUDIO_ONLY=1; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  title) export VIDEOROLL_TEST_TITLE_DB_URL="$DATABASE_URL"; unset VIDEOROLL_TEST_EXTERNAL_WHISPER_DB_URL ;;
  external-whisper) export GROQ_ASR_MAX_CONCURRENCY=1 ;;
esac
cargo test --lib "$test_name" -- --nocapture
