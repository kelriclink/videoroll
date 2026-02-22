#!/usr/bin/env bash
set -euo pipefail

ORCH="${ORCH:-http://localhost:8000}"
SUB="${SUB:-$ORCH/subtitle-service}"

VIDEO_PATH="${1:-}"
if [[ -z "$VIDEO_PATH" ]]; then
  if command -v ffmpeg >/dev/null 2>&1; then
    mkdir -p tmp
    VIDEO_PATH="tmp/sample.mp4"
    ffmpeg -y \
      -f lavfi -i testsrc=size=1280x720:rate=30 \
      -f lavfi -i sine=frequency=1000:sample_rate=44100 \
      -t 3 \
      -c:v libx264 -pix_fmt yuv420p \
      -c:a aac \
      "$VIDEO_PATH" >/dev/null 2>&1
    echo "Generated sample video: $VIDEO_PATH"
  else
    echo "Usage: $0 /path/to/video.mp4"
    echo "Tip: install ffmpeg to auto-generate a sample video."
    exit 2
  fi
fi

if [[ ! -f "$VIDEO_PATH" ]]; then
  echo "Video not found: $VIDEO_PATH"
  exit 2
fi

TASK_ID="$(
  curl -fsS "$ORCH/tasks" \
    -H "content-type: application/json" \
    -d '{"source_type":"local","source_license":"own","priority":0,"created_by":"smoke"}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["id"])'
)"
echo "task_id=$TASK_ID"

curl -fsS "$ORCH/tasks/$TASK_ID/upload/video" \
  -F "file=@${VIDEO_PATH}" >/dev/null
echo "uploaded raw video"

JOB_ID="$(
  curl -fsS "$ORCH/tasks/$TASK_ID/actions/subtitle" \
    -H "content-type: application/json" \
    -d '{"formats":["srt","ass"],"burn_in":true,"soft_sub":false,"ass_style":"clean_white","translate_enabled":false,"translate_provider":"mock","target_lang":"zh","bilingual":false}' \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["job_id"])'
)"
echo "subtitle_job_id=$JOB_ID"

echo "polling subtitle-service..."
for i in $(seq 1 60); do
  STATUS_JSON="$(curl -fsS "$SUB/subtitle/jobs/$JOB_ID")"
  STATUS="$(echo "$STATUS_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')"
  PROGRESS="$(echo "$STATUS_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["progress"])')"
  echo "  status=$STATUS progress=${PROGRESS}%"
  if [[ "$STATUS" == "succeeded" || "$STATUS" == "failed" ]]; then
    break
  fi
  sleep 2
done

echo ""
echo "assets:"
curl -fsS "$ORCH/tasks/$TASK_ID/assets" \
  | python3 -c 'import json,sys; assets=json.load(sys.stdin); [print(f"- {a['"'"'kind'"'"']}: {a['"'"'storage_key'"'"']}") for a in assets]'

echo ""
echo "Done. Open Web UI and search the task_id:"
echo "  http://localhost:3000/tasks/$TASK_ID"
