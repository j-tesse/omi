#!/usr/bin/env bash
# Smoke-test the Omi backend + llm_gateway containers against the local stack:
#   1. health of backend and llm-gateway
#   2. auth: ADMIN_KEY<uid> bypass, a real Firebase Auth emulator ID token, and a 401 without auth
#   3. an LLM call through llm-gateway -> llm-proxy -> vLLM
#   4. batch STT: a pendant-style offline sync upload (/v2/sync-local-files):
#      VAD -> stt -> conversation in Firestore, transcript checked
#   5. processing: /v1/conversations/from-segments with a short dialogue
#      (scripts/test_backend/conversation_segments.json): LLM relevance, summary and
#      folder via llm-gateway, embedding via llm-proxy -> embed, Typesense index
# Re-runnable: each run uses a fresh capture timestamp / client session id.
# Needs the seeded users (docker compose run --rm --no-deps backend-seed).
# Usage: scripts/test_backend.sh [BACKEND_URL] [GATEWAY_URL] [AUDIO_WAV]
#   defaults: http://127.0.0.1:8109  http://127.0.0.1:8110  data/samples/jfk_x3_pauses.wav
#   env: TEST_UID (default bob), SKIP_E2E=1 to stop after step 3
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
BACKEND="${1:-http://127.0.0.1:8109}"
GATEWAY="${2:-http://127.0.0.1:8110}"
AUDIO="${3:-data/samples/jfk_x3_pauses.wav}"
TEST_UID="${TEST_UID:-bob}"
: "${ADMIN_KEY:?ADMIN_KEY missing from .env}" "${OMI_LLM_GATEWAY_SERVICE_TOKEN:?missing from .env}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-omi}"
START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

fail() { echo "FAIL: $*" >&2; exit 1; }
json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
api() { curl -fsS -H "Authorization: Bearer ${ADMIN_KEY}${TEST_UID}" "$@"; }

echo "== health"
curl -fsS -o /dev/null "$BACKEND/docs" && echo "backend: ok"
echo "llm-gateway: $(curl -fsS "$GATEWAY/health")"

echo "== auth"
uid=$(api "$BACKEND/v1/users/profile" | json 'd["uid"]')
[ "$uid" = "$TEST_UID" ] || fail "ADMIN_KEY bypass returned uid '$uid'"
echo "ADMIN_KEY<uid> bypass: uid=$uid"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BACKEND/v1/users/profile")
[ "$code" = 401 ] || [ "$code" = 403 ] || fail "unauthenticated request returned HTTP $code"
echo "no auth: HTTP $code"
# Firebase ID token from the Auth emulator (seeded password, see backend-seed)
tok=$(curl -fsS "http://127.0.0.1:8104/identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=local" \
  -H 'Content-Type: application/json' \
  -d '{"email":"alice@local.omi.invalid","password":"alice-local-password-030","returnSecureToken":true}' | json 'd["idToken"]')
uid=$(curl -fsS -H "Authorization: Bearer $tok" "$BACKEND/v1/users/profile" | json 'd["uid"]')
[ "$uid" = alice ] || fail "Firebase ID token resolved to uid '$uid'"
echo "Firebase emulator ID token: uid=$uid"

echo "== LLM through llm-gateway"
t0=$(date +%s.%N)
reply=$(curl -fsS "$GATEWAY/v1/chat/completions" \
  -H "Authorization: Bearer $OMI_LLM_GATEWAY_SERVICE_TOKEN" -H 'x-omi-service-caller: backend' \
  -H "X-Omi-User-Uid: $TEST_UID" -H 'Content-Type: application/json' \
  -d '{"model":"omi:auto:public-shared-conversation-chat","messages":[{"role":"user","content":"Reply with exactly one word: pong"}]}' \
  | json 'd["choices"][0]["message"]["content"]')
echo "reply: '$reply' ($(python3 -c "import time;print(f'{time.time()-$t0:.1f}')") s)"
echo "$reply" | grep -qi pong || fail "unexpected LLM reply"

[ "${SKIP_E2E:-0}" = 1 ] && { echo "OK (E2E skipped)"; exit 0; }

echo "== batch STT: offline sync upload -> VAD -> stt -> conversation in Firestore"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
bin=$(python3 scripts/test_backend/make_sync_bin.py "$AUDIO" "$tmp")
# A recent capture time + device headers put the upload in the inline "fresh" lane.
dev=(-H 'X-App-Platform: android' -H 'X-Device-Id-Hash: 0c0ffee0' -H 'X-App-Version: 1.0.0')
job=$(api "${dev[@]}" -F "files=@$bin" "$BACKEND/v2/sync-local-files" | json 'd["job_id"]')
echo "job $job queued"
for _ in $(seq 1 100); do
  res=$(api "$BACKEND/v2/sync-local-files/$job")
  status=$(echo "$res" | json 'd["status"]')
  case "$status" in queued|processing|running) sleep 3 ;; *) break ;; esac
done
[ "$status" = completed ] || fail "sync job ended '$status': $res"
cid=$(echo "$res" | json '(d["result"]["new_memories"] + d["result"]["updated_memories"])[0]')
api "$BACKEND/v1/conversations/$cid" | python3 -c '
import json, sys
c = json.load(sys.stdin)
text = " ".join(x.get("text", "") for x in c.get("transcript_segments", []))
print("conversation:", c["id"], "| status:", c.get("status"), "| language:", c.get("language"))
print("transcript:", text[:100])
assert c.get("status") == "completed", "conversation not completed"
assert "ask not what your country" in text.lower(), "transcript does not match the audio"
' || fail "synced conversation check"
# No summary is asserted here: Gemma's relevance check (conv-discard) rightly
# classifies the famous JFK quote as media playback, so the row keeps its
# placeholder title. The summary path is checked with a real dialogue below.

echo "== conversation processing: from-segments -> LLM summary + folder + embedding -> Firestore + Typesense"
# Unique session id per run, so retries never return an earlier conversation.
body=$(python3 -c 'import json,sys,uuid; d=json.load(open(sys.argv[1])); d["client_session_id"]="test-"+uuid.uuid4().hex; print(json.dumps(d))' \
  scripts/test_backend/conversation_segments.json)
t0=$(date +%s)
cid=$(api -H 'Content-Type: application/json' -d "$body" "$BACKEND/v1/conversations/from-segments" | json 'd["id"]')
echo "created $cid in $(( $(date +%s) - t0 )) s"
api "$BACKEND/v1/conversations/$cid" | python3 -c '
import json, sys
c = json.load(sys.stdin)
s = c.get("structured") or {}
print("status:", c.get("status"), "| discarded:", c.get("discarded"), "| title provenance:", s.get("title_provenance"))
print("title:", s.get("title"))
print("overview:", (s.get("overview") or "")[:200])
print("action items:", [a.get("description") for a in s.get("action_items") or []])
assert c.get("status") == "completed", "conversation not completed"
assert not c.get("discarded"), "conversation discarded"
assert s.get("title_provenance") != "server_minimum" and (s.get("overview") or "").strip(), "no LLM summary"
' || fail "processed conversation check"

if [ -n "${TYPESENSE_API_KEY:-}" ]; then
  curl -fsS -o /dev/null -H "X-TYPESENSE-API-KEY: $TYPESENSE_API_KEY" \
    "http://127.0.0.1:${TYPESENSE_PORT:-8108}/collections/conversations/documents/$cid" \
    && echo "typesense: conversation indexed" || fail "conversation $cid not in Typesense"
fi

n=$(docker compose logs --since "$START" llm-gateway 2>/dev/null | grep -c 'lane=omi:auto:openai-embeddings .*outcome=success' || true)
[ "$n" -gt 0 ] || fail "no successful embedding call through llm-gateway"
echo "embeddings: $n call(s) via llm-gateway -> llm-proxy -> embed"
echo "OK"
