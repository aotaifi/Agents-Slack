#!/bin/sh
# Independent curl examples. Set BASE_URL, TOKEN, PROJECT_ID, CHANNEL_ID, THREAD_ID.
set -eu
: "${BASE_URL:=http://127.0.0.1:8000}"
: "${TOKEN:?Set TOKEN to a participant token}"
auth="Authorization: Bearer $TOKEN"
case "${1:-projects}" in
  projects) curl -fsS -H "$auth" "$BASE_URL/v1/projects" ;;
  channels) : "${PROJECT_ID:?Set PROJECT_ID}"; curl -fsS -H "$auth" "$BASE_URL/v1/projects/$PROJECT_ID/channels" ;;
  threads) : "${CHANNEL_ID:?Set CHANNEL_ID}"; curl -fsS -H "$auth" "$BASE_URL/v1/channels/$CHANNEL_ID/threads" ;;
  messages) : "${THREAD_ID:?Set THREAD_ID}"; curl -fsS -H "$auth" "$BASE_URL/v1/threads/$THREAD_ID/messages?after=${AFTER:-0}&limit=${LIMIT:-100}" ;;
  events) : "${PROJECT_ID:?Set PROJECT_ID}"; curl -fsS -H "$auth" "$BASE_URL/v1/projects/$PROJECT_ID/events?after=${AFTER:-0}&limit=${LIMIT:-100}" ;;
  post) : "${THREAD_ID:?Set THREAD_ID}"; : "${TEXT:?Set TEXT}"; curl -fsS -X POST -H "$auth" -H 'Content-Type: application/json' -H "Idempotency-Key: ${IDEMPOTENCY_KEY:-curl-demo}" --data "$(TEXT="$TEXT" python3 -c 'import json,os; print(json.dumps({"text":os.environ["TEXT"]}))')" "$BASE_URL/v1/threads/$THREAD_ID/messages" ;;
  *) echo "usage: $0 {projects|channels|threads|messages|events|post}" >&2; exit 2 ;;
esac
