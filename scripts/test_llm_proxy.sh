#!/usr/bin/env bash
# Smoke-test the LLM alias proxy: health, model list, and a chat completion
# (plain and streamed) for each alias in LLM_MODEL_ALIASES.
# Usage: scripts/test_llm_proxy.sh [BASE_URL]   (default http://127.0.0.1:${LLM_PROXY_PORT:-8101})
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
BASE="${1:-http://127.0.0.1:${LLM_PROXY_PORT:-8101}}"
ALIASES="${LLM_MODEL_ALIASES:-gpt-6-luna=x,gpt-5-nano=x}"

echo "== health"; curl -fsS "$BASE/health"; echo
echo "== models"; curl -fsS "$BASE/v1/models" | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])'

for alias in $(echo "$ALIASES" | tr ',' '\n' | cut -d= -f1 | grep -v '^\*$'); do
  echo "== chat ($alias)"
  curl -fsS "$BASE/v1/chat/completions" -H 'Content-Type: application/json' -H 'Authorization: Bearer dummy' \
    -d "{\"model\":\"$alias\",\"max_tokens\":400,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: pong\"}]}" |
    python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["model"], "->", repr(r["choices"][0]["message"]["content"]))'
  echo "== stream ($alias)"
  curl -fsSN "$BASE/v1/chat/completions" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$alias\",\"stream\":true,\"max_tokens\":400,\"messages\":[{\"role\":\"user\",\"content\":\"Count 1 to 5.\"}]}" |
    python3 -c '
import json, sys
n, text = 0, ""
for line in sys.stdin:
    if line.startswith("data: ") and line.strip() != "data: [DONE]":
        n += 1
        for c in json.loads(line[6:]).get("choices", []):
            text += c.get("delta", {}).get("content") or ""
print(f"{n} chunks ->", repr(text))'
done
