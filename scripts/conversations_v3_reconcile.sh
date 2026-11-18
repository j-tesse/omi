#!/usr/bin/env bash
# Run upstream's conversations-v3 reconcile once for the given uids (default
# local-dev-user) in a throwaway backend container. Recovers recordings stuck
# at v3_pending because Cloud Tasks isn't available locally.
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -gt 0 ] || set -- local-dev-user
exec docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/conversations_v3:/opt/conversations_v3:ro" \
  backend python /opt/conversations_v3/reconcile_once.py "$@"
