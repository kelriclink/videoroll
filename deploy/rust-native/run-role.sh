#!/usr/bin/env bash
set -euo pipefail
# Fixed internal bindings; the web gateway is the only browser entry point.
root=/opt/videoroll-rust/current
case "${1:-}" in
  api) export VIDEOROLL_V2_BIND=127.0.0.1:18080; exec "$root/bin/videoroll-v2" api ;;
  worker) exec "$root/bin/videoroll-v2" worker ;;
  bilibili-publisher) export VIDEOROLL_V2_BIND=127.0.0.1:18082; exec "$root/bin/videoroll-v2" service bilibili-publisher ;;
  subtitle-service) export VIDEOROLL_V2_BIND=127.0.0.1:18083; exec "$root/bin/videoroll-v2" service subtitle-service ;;
  youtube-ingest) export VIDEOROLL_V2_BIND=127.0.0.1:18084; exec "$root/bin/videoroll-v2" service youtube-ingest ;;
  egress) export VIDEOROLL_V2_EGRESS_BIND=127.0.0.1:18086; exec "$root/bin/videoroll-egress" ;;
  sau) export VIDEOROLL_V2_SAU_BIND=127.0.0.1:18087; exec "$root/bin/videoroll-sau" ;;
  *) echo 'unsupported native role' >&2; exit 2 ;;
esac
