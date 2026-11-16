#!/usr/bin/env bash
# Check the runtime fixes in services/backend-patches/ against upstream's real code,
# inside a throwaway container of the backend image (same env + mounts as `backend`).
# Usage: scripts/test_backend_patches.sh [SERVICE]   (default: backend; pusher also works)
set -euo pipefail
cd "$(dirname "$0")/.."
service="${1:-backend}"

docker compose run --rm --no-deps -T \
  -v "$PWD/scripts/test_backend_patches:/opt/test_backend_patches:ro" \
  -e PYTHONPATH=/opt/omi-local-patches:/app \
  --entrypoint python "$service" /opt/test_backend_patches/check_local_storage.py
