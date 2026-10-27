#!/usr/bin/env bash
# Conservative, fail-closed Redis AOF preflight for VideoRoll.
set -Eeuo pipefail
export LD_LIBRARY_PATH=/opt/videoroll/lib
DATA="$(awk '$1=="dir" {print $2; exit}' /opt/videoroll/config/redis.conf)"
if [[ "$DATA" != /* || "$DATA" == / ]]; then
  echo "videoroll-redis-aof-preflight: invalid Redis data directory in configuration" >&2
  exit 1
fi
MANIFEST="$DATA/appendonlydir/appendonly.aof.manifest"
CHECK=/opt/videoroll/bin/redis-check-aof
MAX_BYTES=4096
# Never attempt offline validation or truncation against a live Redis process.
if [ "${VIDEOROLL_REDIS_AOF_TEST_FIXTURE:-0}" != 1 ] && \
   [ "$(/opt/videoroll/bin/redis-cli-real ping 2>/dev/null || true)" = PONG ]; then
  exit 0
fi
log() { echo "videoroll-redis-aof-preflight: $*" >&2; }
if [ ! -e "$MANIFEST" ]; then
  if [ -d "$DATA/appendonlydir" ] && [ -n "$(find "$DATA/appendonlydir" -maxdepth 1 -type f -print -quit)" ]; then
    log "AOF files exist but manifest is missing: refusing to start"; exit 1
  fi
  exit 0
fi
report="$(mktemp)"
trap 'rm -f "$report"' EXIT
if "$CHECK" "$MANIFEST" > "$report" 2>&1; then
  exit 0
fi
last_incr="$(awk '$1=="file" {last=$2; if($5=="type" && $6=="i") incr=$2} END {if(last==incr) print incr}' "$MANIFEST")"
if [[ ! "$last_incr" =~ ^appendonly\.aof\.[0-9]+\.incr\.aof$ ]]; then
  log "No unambiguous final INCR file; manual recovery required"; tail -12 "$report" >&2; exit 1
fi
if ! grep -Fqx "AOF $last_incr format error" "$report" || ! grep -Eq '^BASE AOF .* is valid$' "$report"; then
  log "Unexpected checker failure; manual recovery required"; tail -12 "$report" >&2; exit 1
fi
bad_count="$(grep -Ec '^AOF .* is not valid\.' "$report" || true)"
if [ "$bad_count" -ne 1 ] || ! grep -Fqx "AOF $last_incr is not valid. Use the --fix option to try fixing it." "$report"; then
  log "Multiple or unexpected AOF errors; refusing automatic repair"; tail -12 "$report" >&2; exit 1
fi
line="$(grep -F "AOF analyzed: filename=$last_incr, " "$report" | tail -n 1 || true)"
size="$(printf '%s\n' "$line" | sed -nE 's/.*size=([0-9]+), ok_up_to=[0-9]+,.*/\1/p')"
ok_up_to="$(printf '%s\n' "$line" | sed -nE 's/.*ok_up_to=([0-9]+),.*/\1/p')"
diff="$(printf '%s\n' "$line" | sed -nE 's/.*diff=([0-9]+).*/\1/p')"
if [[ ! "$size" =~ ^[0-9]+$ || ! "$ok_up_to" =~ ^[0-9]+$ || ! "$diff" =~ ^[0-9]+$ ]]; then
  log "Could not parse AOF offset safely"; exit 1
fi
if (( ok_up_to >= size || diff != size - ok_up_to || diff > MAX_BYTES )) || \
   [ "$(stat -c %s "$DATA/appendonlydir/$last_incr")" != "$size" ]; then
  log "Rejecting repair: size=$size valid=$ok_up_to diff=$diff max=$MAX_BYTES"; exit 1
fi
BACKUP_ROOT="$(dirname "$DATA")/redis-aof-autorecovery"
mkdir -p -m 0700 "$BACKUP_ROOT"
backup="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
mkdir -m 0700 "$backup"
cp -a "$DATA" "$backup/redis-native"
(cd "$backup/redis-native" && find . -type f -print0 | xargs -0 sha256sum) > "$backup/SHA256SUMS"
(cd "$backup/redis-native" && sha256sum -c "$backup/SHA256SUMS" > /dev/null)
# Sync only these newly copied files, not every VideoRoll storage write.
find "$backup/redis-native" -type f -print0 | xargs -0 -r sync
sync "$backup/SHA256SUMS" "$backup/redis-native" "$backup"
log "Backed up original Redis data at $backup; repairing $diff trailing bytes"
if ! printf 'y\n' | "$CHECK" --fix "$MANIFEST" >> "$report" 2>&1; then
  log "AOF fix failed; backup preserved: $backup"; tail -12 "$report" >&2; exit 1
fi
if ! "$CHECK" "$MANIFEST" >> "$report" 2>&1; then
  log "AOF post-repair verification failed; backup preserved: $backup"; tail -12 "$report" >&2; exit 1
fi
log "AOF post-repair verification passed; original backup: $backup"
