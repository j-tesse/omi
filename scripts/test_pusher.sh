#!/usr/bin/env bash
# End-to-end test of live conversation finalization through the pusher:
# pusher/backend health, then a pendant-style /v4/listen session on the backend
# (ADMIN_KEY<uid> auth bypass) that streams a WAV in real time, holds the socket
# open with silence until the 120 s conversation timeout fires, and waits for the
# pusher to finalize the conversation. Checks the result through the backend API
# (status completed + structured title/overview) and in the Firestore emulator.
# Takes ~3-4 min (audio + 120 s timeout + LLM processing).
#
# Usage: scripts/test_pusher.sh [AUDIO_FILE] [UID]
#   AUDIO_FILE  16-bit mono WAV, default data/samples/jfk_x3_pauses.wav
#   UID         default pusher-test-<timestamp> (a fresh user each run)
# Env: CONVERSATION_TIMEOUT (default 120, the minimum), WAIT (default 600 s),
#      SPEED (default 1 = real time).
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
: "${ADMIN_KEY:?ADMIN_KEY must be set in .env}"
AUDIO="$(realpath "${1:-data/samples/jfk_x3_pauses.wav}")"
TEST_UID="${2:-pusher-test-$(date +%s)}"
PROJECT="${FIREBASE_PROJECT_ID:-demo-omi-local}"
FS="http://127.0.0.1:8103/v1/projects/$PROJECT/databases/(default)/documents"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-omi}"
compose() { docker compose "$@"; }

echo "== health"
echo "pusher:  $(curl -fsS "http://127.0.0.1:${PUSHER_PORT:-8111}/health")"
echo "backend: $(curl -fsS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${BACKEND_PORT:-8109}/docs")"
pusher_url=$(compose exec -T backend printenv HOSTED_PUSHER_API_URL || true)
[ -n "$pusher_url" ] || { echo "backend has no HOSTED_PUSHER_API_URL: conversations will stay in_progress" >&2; exit 1; }
echo "backend HOSTED_PUSHER_API_URL=$pusher_url"

echo "== live session (uid $TEST_UID)"
compose exec -T pusher sh -c 'cat > /tmp/test_pusher.wav' <"$AUDIO"
log=$(mktemp)
trap 'rm -f "$log"' EXIT
set +e
compose exec -T pusher python - /tmp/test_pusher.wav \
  --uid "$TEST_UID" --admin-key "$ADMIN_KEY" \
  --conversation-timeout "${CONVERSATION_TIMEOUT:-120}" --wait "${WAIT:-600}" --speed "${SPEED:-1}" \
  <scripts/test_pusher/listen_client.py | tee "$log"
rc=${PIPESTATUS[0]}
set -e
result=$(grep '^RESULT ' "$log" | tail -1 | cut -d' ' -f2-)
[ -n "$result" ] || { echo "client produced no result (exit $rc)" >&2; exit 1; }
echo "$result" | python3 -m json.tool --no-ensure-ascii
[ "$rc" -eq 0 ] || { echo "FAIL: conversation was not finalized" >&2; exit 1; }

echo "== firestore emulator"
cid=$(echo "$result" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
curl -fsS -H 'Authorization: Bearer owner' "$FS/users/$TEST_UID/conversations/$cid" | python3 -c '
import json, sys
doc = json.load(sys.stdin)["fields"]
status = doc.get("status", {}).get("stringValue")
structured = doc.get("structured", {})
kind = "map" if "mapValue" in structured else ("encrypted string" if "stringValue" in structured else "missing")
print(f"status={status} structured={kind} fields={sorted(doc)}")
sys.exit(0 if status == "completed" and kind != "missing" else 1)
'
echo "OK: conversation $cid finalized by the pusher"
