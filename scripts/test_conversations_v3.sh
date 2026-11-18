#!/usr/bin/env bash
# End-to-end test of the local conversations-v3 path (upstream "recordings and
# conversations (v3)", built by Cloud Tasks upstream, by conversations-v3-worker here):
#   1. the worker is running and the backend has the v3 queue patch
#   2. GET /v1/episodes/live marks TEST_UID as a v3-capable client (upstream's gate)
#   3. a pendant-style offline sync upload of AUDIO_WAV -> hidden v3 "recording"
#      (v3_pending in Firestore, uid queued in Redis, not visible in the API)
#   4. after upstream's 5 min silence deadline the worker reconciles: the recording's
#      v3_pending clears and a completed conversation with a title shows up in the API
# Takes ~6-7 min. Needs the seeded users (backend-seed).
# Usage: scripts/test_conversations_v3.sh [BACKEND_URL] [AUDIO_WAV]
#   defaults: http://127.0.0.1:8109  data/samples/meeting_en.wav
#   env: TEST_UID (default bob), TIMEOUT_SECONDS (default 720)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
BACKEND="${1:-http://127.0.0.1:8109}"
AUDIO="${2:-data/samples/meeting_en.wav}"
TEST_UID="${TEST_UID:-bob}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-720}"
FIRESTORE="http://127.0.0.1:8103/v1/projects/${FIREBASE_PROJECT_ID:-demo-omi-local}/databases/(default)/documents"
QUEUE_KEY=local:conversations_v3:due
: "${ADMIN_KEY:?ADMIN_KEY missing from .env}"
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-omi}"

fail() { echo "FAIL: $*" >&2; exit 1; }
json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
api() { curl -fsS -H "Authorization: Bearer ${ADMIN_KEY}${TEST_UID}" "$@"; }
recording_field() {  # recording_field <conversation id> <field> -> JSON value or null
  curl -fsS -H 'Authorization: Bearer owner' "$FIRESTORE/users/$TEST_UID/conversations/$1" | python3 -c '
import json, sys
f = json.load(sys.stdin).get("fields", {}).get(sys.argv[1])
print(json.dumps(next(iter(f.values())) if f else None))' "$2"
}
[ -f "$AUDIO" ] || scripts/test_pusher/make_sample.sh

echo "== worker + patches"
# Capture first: grep -q closing the pipe early would fail the pipeline under pipefail.
running=$(docker compose ps --status running --services)
grep -qx conversations-v3-worker <<<"$running" || fail "conversations-v3-worker is not running"
backend_log=$(docker compose logs backend 2>&1)
grep -q 'patched v3 triggers.enqueue' <<<"$backend_log" || fail "backend did not load the v3 queue patch"
echo "worker running, backend patched"

echo "== mark $TEST_UID as a v3 client"
# Only beta-channel clients set the marker (routers/episodes.py), like our dev app build.
api -o /dev/null -H "X-Omi-App-Channel: beta" "$BACKEND/v1/episodes/live" || fail "GET /v1/episodes/live"
# Drop the marker on exit so TEST_UID's later syncs (test_backend.sh) take the legacy path
# again (after the backend's 120 s gate cache).
cleanup_marker() { docker compose exec -T redis redis-cli DEL "episodes:client_seen:$TEST_UID" >/dev/null || true; }
trap cleanup_marker EXIT
docker compose exec -T redis redis-cli EXISTS "episodes:client_seen:$TEST_UID" | grep -qx 1 || fail "client_seen marker not set"
echo "episodes API called"

echo "== offline sync upload -> v3 recording"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"; cleanup_marker' EXIT
bin=$(python3 scripts/test_backend/make_sync_bin.py "$AUDIO" "$tmp")
dev=(-H 'X-App-Platform: android' -H 'X-Device-Id-Hash: 0c0ffee0' -H 'X-App-Version: 1.0.0')
job=$(api "${dev[@]}" -F "files=@$bin" "$BACKEND/v2/sync-local-files" | json 'd["job_id"]')
for _ in $(seq 1 100); do
  res=$(api "$BACKEND/v2/sync-local-files/$job")
  status=$(echo "$res" | json 'd["status"]')
  case "$status" in queued|processing|running) sleep 3 ;; *) break ;; esac
done
[ "$status" = completed ] || fail "sync job ended '$status': $res"
rid=$(echo "$res" | json '(d["result"]["new_memories"] + d["result"]["updated_memories"])[0]')
started=$(recording_field "$rid" started_at | tr -d '"')
echo "recording $rid started_at=$started"
[ "$(recording_field "$rid" recording)" = true ] || fail "upload did not take the v3 path (recording != true); is the gate set?"
[ "$(recording_field "$rid" v3_pending)" = true ] || fail "recording is not v3_pending"
# The worker polls every few seconds, so the first (still open) reconcile may already be
# done; it then queues the continuation at the end of the silence window.
# Poll briefly: between claiming a uid and re-queueing it the worker holds it out of the set.
for _ in $(seq 1 15); do
  queued=$(docker compose exec -T redis redis-cli ZSCORE "$QUEUE_KEY" "$TEST_UID")
  [ -n "$queued" ] && break
  sleep 2
done
[ -n "$queued" ] || fail "$TEST_UID not queued in $QUEUE_KEY"
echo "v3_pending, queued in Redis (due in $(python3 -c "import time;print(int($queued-time.time()))") s)"

echo "== waiting for the worker (5 min silence deadline + outline + summary)"
t0=$(date +%s)
while :; do
  elapsed=$(( $(date +%s) - t0 ))
  [ "$elapsed" -lt "$TIMEOUT_SECONDS" ] || fail "no v3 conversation after ${TIMEOUT_SECONDS}s"
  if [ "$(recording_field "$rid" v3_pending)" != true ]; then
    found=$(api "$BACKEND/v1/conversations?limit=20&include_discarded=true" | python3 -c '
import json, sys
start = sys.argv[1]
for c in json.load(sys.stdin):
    s = c.get("structured") or {}
    if c.get("started_at", "") >= start[:19] and c.get("status") == "completed" and s.get("title"):
        print(c["id"], "|", s.get("emoji", ""), s["title"], "|", (s.get("overview") or "")[:100].replace("\n", " "))
        break' "$started")
    if [ -n "$found" ]; then
      echo "after ${elapsed}s: $found"
      break
    fi
  fi
  sleep 15
done
docker compose logs --since "$((elapsed + 60))s" conversations-v3-worker 2>&1 | grep "reconcile uid=$TEST_UID" | tail -3
echo "OK"
