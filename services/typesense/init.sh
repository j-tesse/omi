#!/bin/sh
# One-shot, idempotent Typesense bootstrap (run by the `typesense-init` compose
# service). Creates every collection whose schema file is mounted in /schemas
# (*.schema, Typesense collection JSON) and that doesn't exist yet. Existing
# collections are left untouched, so their data survives.
#
# Upstream reads the `conversations` collection for search and upserts into it,
# but never creates it (backend/typesense/conversations.schema + the setup docs
# say to create it by hand). The memory collection (`canonical_memory_atoms`)
# is created by the backend itself on first write, so it isn't needed here.
set -eu
URL="${TYPESENSE_URL:-http://typesense:8108}"
: "${TYPESENSE_API_KEY:?TYPESENSE_API_KEY is not set}"
H="X-TYPESENSE-API-KEY: $TYPESENSE_API_KEY"

for f in /schemas/*.schema; do
  [ -e "$f" ] || { echo "no schema files in /schemas" >&2; exit 1; }
  name=$(sed -n 's/^ *"name": *"\([^"]*\)".*/\1/p' "$f" | head -n1)
  [ -n "$name" ] || { echo "$f: no collection name found" >&2; exit 1; }
  code=$(curl -sS -o /dev/null -w '%{http_code}' -H "$H" "$URL/collections/$name")
  case "$code" in
    200) echo "collection '$name' exists, leaving it as is" ;;
    404)
      curl -fsS -H "$H" -H 'Content-Type: application/json' -X POST "$URL/collections" \
        --data-binary "@$f" >/dev/null
      echo "collection '$name' created from $(basename "$f")" ;;
    *) echo "GET /collections/$name returned HTTP $code" >&2; exit 1 ;;
  esac
done
