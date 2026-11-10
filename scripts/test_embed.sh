#!/usr/bin/env bash
# Smoke-test the embedding service (directly or through llm-proxy): health, model
# list, and /v1/embeddings with every input shape upstream Omi sends (strings,
# cl100k token IDs, base64 output), plus `dimensions` and an EN/DE similarity check.
# Usage: scripts/test_embed.sh [BASE_URL] [MODEL]
#   default BASE_URL http://127.0.0.1:${EMBED_PORT:-8102}; via proxy: http://127.0.0.1:8101
#   default MODEL text-embedding-3-large (the name upstream sends; the server ignores it)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a
BASE="${1:-http://127.0.0.1:${EMBED_PORT:-8102}}"
MODEL="${2:-text-embedding-3-large}"

echo "== health"; curl -fsS "$BASE/health"; echo
echo "== models"; curl -fsS "$BASE/v1/models" | python3 -c 'import json,sys; print([m["id"] for m in json.load(sys.stdin)["data"]])'

# Token IDs below are cl100k_base for "The cat sleeps on the sofa." (as LangChain sends them).
BASE="$BASE" MODEL="$MODEL" python3 -I - <<'EOF'
import base64, json, os, struct, time, urllib.request

base, model = os.environ["BASE"], os.environ["MODEL"]

def embed(inp, **kw):
    req = urllib.request.Request(f"{base}/v1/embeddings", json.dumps({"model": model, "input": inp, **kw}).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer dummy"})
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.load(r)
    assert body["object"] == "list" and body["data"], body
    return body, time.perf_counter() - t

def cos(a, b):
    return sum(x * y for x, y in zip(a, b)) / (sum(x * x for x in a) ** .5 * sum(y * y for y in b) ** .5)

ok = True
def check(name, cond, info=""):
    global ok
    ok &= bool(cond)
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {info}")

text = "The cat sleeps on the sofa."
b, dt = embed(text)
ref = b["data"][0]["embedding"]
check("string", len(ref) > 0, f"dim={len(ref)} model={b['model']} usage={b['usage']} {dt*1000:.0f} ms")

b, _ = embed([text, "", "Die Katze schläft auf dem Sofa.", "Quarterly tax filing deadline."])
v = [d["embedding"] for d in b["data"]]
check("list of strings (incl. empty)", len(v) == 4 and [d["index"] for d in b["data"]] == [0, 1, 2, 3])
check("same text, same vector", cos(ref, v[0]) > 0.999, f"cos={cos(ref, v[0]):.4f}")
check("EN/DE similarity", cos(v[0], v[2]) > cos(v[0], v[3]) + 0.1,
      f"en~de={cos(v[0], v[2]):.3f} en~unrelated={cos(v[0], v[3]):.3f}")

ids = [791, 8415, 72490, 389, 279, 32169, 13]
b, _ = embed([ids])
check("cl100k token IDs (list of lists)", cos(ref, b["data"][0]["embedding"]) > 0.999,
      f"cos={cos(ref, b['data'][0]['embedding']):.4f}")
b, _ = embed(ids)
check("cl100k token IDs (flat list)", cos(ref, b["data"][0]["embedding"]) > 0.999)

b, _ = embed(text, encoding_format="base64")
raw = base64.b64decode(b["data"][0]["embedding"])
dec = struct.unpack(f"<{len(raw)//4}f", raw)
check("base64", len(dec) == len(ref) and cos(ref, dec) > 0.999, f"{len(dec)} floats")

b, _ = embed(text, dimensions=256)
short = b["data"][0]["embedding"]
check("dimensions=256", len(short) == 256 and abs(sum(x * x for x in short) - 1) < 1e-3)

long_text = " ".join(["The pendant recorded a long meeting about the product roadmap."] * 100)
b, dt = embed(long_text)
check("~1.1k-token input", b["data"], f"{dt*1000:.0f} ms, usage={b['usage']}")

raise SystemExit(0 if ok else 1)
EOF
