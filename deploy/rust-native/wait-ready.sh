#!/usr/bin/env bash
set -euo pipefail
for attempt in {1..60}; do
  all=true
  for url in "$@"; do
    if ! curl --noproxy '*' -fsS --max-time 2 "$url" >/dev/null 2>&1; then all=false; break; fi
  done
  if [[ "$all" == true ]]; then exit 0; fi
  sleep 1
done
echo 'native internal services did not become ready' >&2
exit 1
