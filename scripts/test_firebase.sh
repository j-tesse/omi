#!/usr/bin/env bash
# Smoke-test the Firebase emulators: health, a Firestore REST write/read/delete,
# and an Auth sign-up / sign-in / delete. With --persist, also checks that a
# document survives `docker compose restart firebase` (export on exit + import).
# Usage: scripts/test_firebase.sh [--persist] [FIRESTORE_URL] [AUTH_URL]
#   defaults: http://127.0.0.1:8103  http://127.0.0.1:8104
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
PERSIST=0
[ "${1:-}" = "--persist" ] && PERSIST=1 && shift
FS="${1:-http://127.0.0.1:8103}"
AUTH="${2:-http://127.0.0.1:8104}"
PROJECT="${FIREBASE_PROJECT_ID:-demo-omi-local}"
DOCS="$FS/v1/projects/$PROJECT/databases/(default)/documents"
ID="smoke-$(date +%s)-$$"
# "Bearer owner" makes the Firestore emulator bypass security rules (like the Admin SDK).
fs() { curl -fsS -H 'Authorization: Bearer owner' -H 'Content-Type: application/json' "$@"; }
field() { python3 -c 'import json,sys; print(json.load(sys.stdin)'"$1"')'; }

echo "== health"
echo "firestore: $(curl -fsS "$FS/")"
curl -fsS "$AUTH/" >/dev/null && echo "auth: ok"

echo "== firestore write/read/delete"
fs -X PATCH "$DOCS/smoke/$ID" -d '{"fields":{"msg":{"stringValue":"hello"}}}' >/dev/null
got=$(fs "$DOCS/smoke/$ID" | field '["fields"]["msg"]["stringValue"]')
[ "$got" = hello ] || { echo "read back '$got', expected 'hello'" >&2; exit 1; }
fs -X DELETE "$DOCS/smoke/$ID" >/dev/null
if fs "$DOCS/smoke/$ID" >/dev/null 2>&1; then echo "document still exists after delete" >&2; exit 1; fi
echo "ok"

echo "== auth sign-up/sign-in/delete"
IDT="$AUTH/identitytoolkit.googleapis.com/v1"
EMAIL="$ID@local.omi.invalid"
uid=$(curl -fsS "$IDT/accounts:signUp?key=fake" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"smoke-pass-123\",\"returnSecureToken\":true}" | field '["localId"]')
tok=$(curl -fsS "$IDT/accounts:signInWithPassword?key=fake" -H 'Content-Type: application/json' \
  -d "{\"email\":\"$EMAIL\",\"password\":\"smoke-pass-123\",\"returnSecureToken\":true}" | field '["idToken"]')
curl -fsS "$IDT/accounts:delete?key=fake" -H 'Content-Type: application/json' -d "{\"idToken\":\"$tok\"}" >/dev/null
echo "ok (uid $uid)"

if [ "$PERSIST" = 1 ]; then
  echo "== persistence across restart"
  fs -X PATCH "$DOCS/smoke/$ID-persist" -d '{"fields":{"msg":{"stringValue":"kept"}}}' >/dev/null
  docker compose restart firebase >/dev/null
  for _ in $(seq 1 40); do curl -fsS "$FS/" >/dev/null 2>&1 && curl -fsS "$AUTH/" >/dev/null 2>&1 && break; sleep 2; done
  got=$(fs "$DOCS/smoke/$ID-persist" | field '["fields"]["msg"]["stringValue"]')
  fs -X DELETE "$DOCS/smoke/$ID-persist" >/dev/null
  [ "$got" = kept ] || { echo "document lost across restart" >&2; exit 1; }
  echo "ok"
fi
echo "all checks passed"
