#!/usr/bin/env bash
set -euo pipefail
: "${VIDEOROLL_DATA_ROOT:?Set data root in /opt/videoroll/config/videoroll.env}"
exec /opt/videoroll/bin/ffplayout --listen 127.0.0.1:8787 --db "$VIDEOROLL_DATA_ROOT/ffplayout/db/ffplayout.db" --log-to-console
