#!/usr/bin/env bash
# Replay upstream Omi's LLM call shapes (per feature, as llm_gateway sends them)
# against llm-proxy and report PASS / SLOW / FAIL per feature.
#
# Usage: scripts/test_llm_features.sh [BASE_URL] [run.py args...]
#   BASE_URL     default http://127.0.0.1:${LLM_PROXY_PORT:-8101}
#   --only a,b   run only these cases or features (e.g. --only conv_structure,translation_de_en)
#   --skip-heavy skip the ~30 min transcript case
#   --repeat N   run every case N times (latency spread)
#
# Runs in a container (scripts/llm_features/, same langchain/openai/pydantic pins
# as upstream's backend) with the upstream submodule mounted read-only, so the
# real upstream schemas, parsers and prompts are used.
# Output: results/llm_features/<timestamp>/{results.jsonl,summary.txt}
# Exit code 1 if any case FAILs (SLOW does not fail the run).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ -f .env ] && set -a && . ./.env && set +a
BASE="http://127.0.0.1:${LLM_PROXY_PORT:-8101}"
if [[ $# -gt 0 && "$1" != --* ]]; then BASE="$1"; shift; fi
[ -f upstream/omi/backend/llm_gateway/config/generated_route_overrides.yaml ] ||
  { echo "upstream submodule missing: git submodule update --init" >&2; exit 2; }

IMAGE=omi-test-llm-features:latest
docker build -q -t "$IMAGE" scripts/llm_features >/dev/null
OUT="results/llm_features/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
echo "== $BASE -> $OUT"
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e LLM_BASE_URL="$BASE/v1" \
  -v "$ROOT/scripts/llm_features:/app:ro" \
  -v "$ROOT/upstream/omi:/upstream:ro" \
  -v "$ROOT/$OUT:/out" \
  "$IMAGE" --out /out "$@"
