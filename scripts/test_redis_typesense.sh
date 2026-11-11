#!/usr/bin/env bash
# Smoke-test Redis and Typesense: health, a Redis SET/GET/DEL (authenticating
# the way the backend does: AUTH default ""), and a Typesense collection list
# (the `conversations` collection must exist) plus a document
# upsert/search/delete in that collection. With --persist, also checks that a
# Redis key and a Typesense document survive
# `docker compose restart redis typesense`.
# Usage: scripts/test_redis_typesense.sh [--persist] [REDIS_HOST:PORT] [TYPESENSE_URL]
#   defaults: 127.0.0.1:8107  http://127.0.0.1:8108
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
PERSIST=0
[ "${1:-}" = "--persist" ] && PERSIST=1 && shift
REDIS="${1:-127.0.0.1:8107}"
TS="${2:-http://127.0.0.1:8108}"
: "${TYPESENSE_API_KEY:?TYPESENSE_API_KEY is not set (see .env.example)}"
ID="smoke-$(date +%s)-$$"
COLL=conversations

# Send inline Redis commands (one per argument) over /dev/tcp; prints the raw replies.
redis() {
  local cmds="AUTH default \"\"\r\n" c
  for c in "$@"; do cmds+="$c\r\n"; done
  exec 3<>"/dev/tcp/${REDIS%:*}/${REDIS##*:}"
  printf "${cmds}QUIT\r\n" >&3
  tr -d '\r' <&3
  exec 3<&-
}
ts() { curl -fsS -H "X-TYPESENSE-API-KEY: $TYPESENSE_API_KEY" -H 'Content-Type: application/json' "$@"; }
field() { python3 -c 'import json,sys; print(json.load(sys.stdin)'"$1"')'; }
fail() { echo "$*" >&2; exit 1; }
doc() { echo "{\"id\":\"$1\",\"userId\":\"$ID\",\"created_at\":$(date +%s),\"structured\":{\"title\":\"$2\",\"overview\":\"smoke test\",\"category\":\"other\"}}"; }

echo "== health"
out=$(redis PING)
grep -qx '+PONG' <<<"$out" || fail "redis: unexpected reply: $out"
echo "redis: ok ($(redis 'INFO server' | sed -n 's/^redis_version://p'))"
echo "typesense: $(curl -fsS "$TS/health")"

echo "== redis set/get/del"
out=$(redis "SET $ID hello EX 60" "GET $ID" "DEL $ID" "EXISTS $ID")
[ "$(tr '\n' ' ' <<<"$out")" = "+OK +OK \$5 hello :1 :0 +OK " ] || fail "redis: unexpected replies: $out"
echo "ok"

echo "== typesense collections"
names=$(ts "$TS/collections" | python3 -c 'import json,sys; print(" ".join(c["name"] for c in json.load(sys.stdin)))')
echo "collections: ${names:-<none>}"
grep -qw "$COLL" <<<"$names" || fail "collection '$COLL' missing (did typesense-init run?)"

echo "== typesense upsert/search/delete"
ts -X POST "$TS/collections/$COLL/documents?action=upsert" -d "$(doc "$ID" 'Zebra smoke pancake')" >/dev/null
q="q=pancake&query_by=structured.title&filter_by=userId:=$ID"
found=$(ts "$TS/collections/$COLL/documents/search?$q" | field '["found"]')
[ "$found" = 1 ] || fail "search found $found documents, expected 1"
ts -X DELETE "$TS/collections/$COLL/documents/$ID" >/dev/null
if ts "$TS/collections/$COLL/documents/$ID" >/dev/null 2>&1; then fail "document still exists after delete"; fi
echo "ok"

if [ "$PERSIST" = 1 ]; then
  echo "== persistence across restart"
  redis "SET $ID-persist kept" >/dev/null
  ts -X POST "$TS/collections/$COLL/documents?action=upsert" -d "$(doc "$ID-persist" 'kept')" >/dev/null
  COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-omi}" docker compose restart redis typesense >/dev/null
  for _ in $(seq 1 30); do
    redis PING 2>/dev/null | grep -qx '+PONG' && curl -fsS "$TS/health" 2>/dev/null | grep -q '"ok":true' && break
    sleep 2
  done
  got=$(redis "GET $ID-persist" "DEL $ID-persist" | sed -n 3p)
  title=$(ts "$TS/collections/$COLL/documents/$ID-persist" | field '["structured"]["title"]')
  ts -X DELETE "$TS/collections/$COLL/documents/$ID-persist" >/dev/null
  [ "$got" = kept ] || fail "redis key lost across restart"
  [ "$title" = kept ] || fail "typesense document lost across restart"
  echo "ok"
fi
echo "all checks passed"
