"""OpenAI-compatible pass-through that rewrites model names.

Upstream Omi hardcodes OpenAI model names (gpt-6-luna, gpt-5-nano, ...). This
proxy sits at OPENAI_BASE_URL, replaces the `model` field of each JSON request
according to LLM_MODEL_ALIASES, and forwards the request unchanged otherwise to
LLM_UPSTREAM_URL (the local vLLM), or /v1/embeddings to LLM_EMBEDDINGS_URL
(the local embedding server). Responses, including SSE streams, are
relayed byte for byte.
"""

import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

log = logging.getLogger("llm-proxy")
logging.basicConfig(level=os.getenv("LLM_PROXY_LOG_LEVEL", "INFO").upper())

UPSTREAM = os.getenv("LLM_UPSTREAM_URL", "http://host.docker.internal:8000/v1").rstrip("/")
UPSTREAM_KEY = os.getenv("LLM_UPSTREAM_API_KEY", "")
TIMEOUT = float(os.getenv("LLM_PROXY_TIMEOUT_S", "600"))
# /v1/embeddings goes to the local embedding server (vLLM serves chat only).
# Empty = send embeddings to LLM_UPSTREAM_URL like everything else.
EMBEDDINGS = os.getenv("LLM_EMBEDDINGS_URL", "http://embed:8000/v1").rstrip("/")


def parse_aliases(spec: str) -> dict[str, str]:
    """"a=x,b=y" -> {"a": "x", "b": "y"}. Key "*" is the fallback for unknown names."""
    aliases = {}
    for item in spec.split(","):
        if not item.strip():
            continue
        alias, sep, target = item.partition("=")
        if not sep or not alias.strip() or not target.strip():
            raise ValueError(f"bad LLM_MODEL_ALIASES entry: {item!r} (expected alias=target)")
        aliases[alias.strip()] = target.strip()
    return aliases


ALIASES = parse_aliases(os.getenv("LLM_MODEL_ALIASES", ""))
log.info("upstream %s, embeddings %s, aliases %s", UPSTREAM, EMBEDDINGS or UPSTREAM, ALIASES)

# Hop-by-hop / recomputed headers that must not be copied across.
DROP_REQ = {"host", "content-length", "connection", "accept-encoding", "transfer-encoding"}
DROP_RESP = {"content-length", "connection", "transfer-encoding", "content-encoding"}

client = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=5.0))
app = FastAPI(title="omi-llm-proxy")


@app.on_event("shutdown")
async def _close():
    await client.aclose()


def resolve(model: str) -> str:
    return ALIASES.get(model) or ALIASES.get("*") or model


@app.get("/health")
async def health():
    try:
        r = await client.get(f"{UPSTREAM}/models", headers=_auth({}), timeout=5.0)
        ok = r.status_code == 200
    except httpx.HTTPError:
        ok = False
    return JSONResponse({"status": "ok" if ok else "upstream_unavailable"}, status_code=200 if ok else 503)


@app.get("/v1/models")
async def models():
    """Upstream models plus the aliases, so clients that list models see the names they expect."""
    r = await client.get(f"{UPSTREAM}/models", headers=_auth({}))
    body = r.json()
    if EMBEDDINGS and EMBEDDINGS != UPSTREAM:
        try:  # best effort: chat keeps working while the embedding server is down
            e = await client.get(f"{EMBEDDINGS}/models", timeout=5.0)
            body.setdefault("data", []).extend(e.json().get("data", []))
        except (httpx.HTTPError, ValueError):
            pass
    known = {m["id"] for m in body.get("data", [])}
    for alias, target in ALIASES.items():
        if alias != "*" and alias not in known:
            body.setdefault("data", []).append(
                {"id": alias, "object": "model", "owned_by": "omi-llm-proxy", "root": target}
            )
    return JSONResponse(body, status_code=r.status_code)


def _auth(headers: dict) -> dict:
    # Clients send their own (dummy) OpenAI key; vLLM gets the configured one, if any.
    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
    if UPSTREAM_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_KEY}"
    return headers


@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    body = await request.body()
    headers = _auth({k: v for k, v in request.headers.items() if k.lower() not in DROP_REQ})

    if body and "json" in request.headers.get("content-type", ""):
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("model"), str):
            original = payload["model"]
            payload["model"] = resolve(original)
            if payload["model"] != original:
                log.debug("%s: %s -> %s", path, original, payload["model"])
            body = json.dumps(payload).encode()

    base = EMBEDDINGS if path == "embeddings" and EMBEDDINGS else UPSTREAM
    upstream_req = client.build_request(
        request.method, f"{base}/{path}", params=request.query_params, headers=headers, content=body
    )
    try:
        r = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        log.warning("upstream error on %s: %s", path, exc)
        return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)

    resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in DROP_RESP}
    if "text/event-stream" in r.headers.get("content-type", ""):
        return StreamingResponse(
            r.aiter_raw(), status_code=r.status_code, headers=resp_headers, background=BackgroundTask(r.aclose)
        )
    content = await r.aread()
    await r.aclose()
    return Response(content, status_code=r.status_code, headers=resp_headers)
