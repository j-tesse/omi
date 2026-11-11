"""OpenAI-compatible pass-through that rewrites model names.

Upstream Omi hardcodes OpenAI model names (gpt-6-luna, gpt-5-nano, ...). This
proxy sits at OPENAI_BASE_URL, replaces the `model` field of each JSON request
according to LLM_MODEL_ALIASES, and forwards the request unchanged otherwise to
LLM_UPSTREAM_URL (the local vLLM), or /v1/embeddings to LLM_EMBEDDINGS_URL
(the local embedding server). Responses, including SSE streams, are
relayed byte for byte.

Explicit request rewrites on /v1/chat/completions (all logged at DEBUG, each
switchable off; see AGENTS.md "LLM alias proxy"):
  * reasoning_effort values Gemma's chat template doesn't know are mapped per
    LLM_REASONING_EFFORT_MAP (default minimal=none, xhigh=high, max=high).
  * response_format json_schema is compiled into a whitespace-free grammar and
    sent as vLLM `structured_outputs.grammar` (compact_json.py; LLM_COMPACT_JSON),
    which stops Gemma's whitespace loop under strict schemas.
  * a leading system/developer message is folded into the first user message
    (LLM_FOLD_SYSTEM_PROMPT). vLLM hands this chat template every content as a
    list of parts, and the template renders the first system turn with `| trim`,
    i.e. as a Python list repr (`[{'type': 'text', 'text': '...'}]`, newlines as
    literal \n). Later turns iterate the parts and render fine.
  * requests without max_tokens / max_completion_tokens get
    max_completion_tokens=LLM_DEFAULT_MAX_TOKENS, so a degenerate generation
    can't run to the 90k context limit and hold one of vLLM's two slots.
A non-streamed request whose client disconnects is aborted upstream.
"""

import asyncio

import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app.compact_json import Unsupported, grammar_for

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
EFFORT_MAP = parse_aliases(os.getenv("LLM_REASONING_EFFORT_MAP", "minimal=none,xhigh=high,max=high"))
DEFAULT_MAX_TOKENS = int(os.getenv("LLM_DEFAULT_MAX_TOKENS", "8192") or 0)
FOLD_SYSTEM = os.getenv("LLM_FOLD_SYSTEM_PROMPT", "1").strip().lower() not in ("0", "false", "no", "off", "")
COMPACT_JSON = os.getenv("LLM_COMPACT_JSON", "1").strip().lower() not in ("0", "false", "no", "off", "")
log.info(
    "upstream %s, embeddings %s, aliases %s, effort map %s, default max tokens %s, compact json %s",
    UPSTREAM, EMBEDDINGS or UPSTREAM, ALIASES, EFFORT_MAP, DEFAULT_MAX_TOKENS or "off", COMPACT_JSON,
)

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
            if path == "chat/completions":
                rewrite_chat(payload)
            body = json.dumps(payload).encode()

    base = EMBEDDINGS if path == "embeddings" and EMBEDDINGS else UPSTREAM
    upstream_req = client.build_request(
        request.method, f"{base}/{path}", params=request.query_params, headers=headers, content=body
    )
    # Race the upstream call against the client going away (e.g. after its own timeout):
    # closing the upstream connection makes vLLM cancel the request instead of generating
    # on for minutes in one of its few slots. vLLM sends a non-streamed response's headers
    # only when generation is done, so the race has to cover send() as well as the body.
    gone = asyncio.ensure_future(_client_disconnect(request))
    r = None
    try:
        send = asyncio.ensure_future(client.send(upstream_req, stream=True))
        if not await _first(send, gone):
            return _aborted(path)
        try:
            r = send.result()
        except httpx.HTTPError as exc:
            log.warning("upstream error on %s: %s", path, exc)
            return JSONResponse({"error": {"message": "upstream unavailable", "type": "proxy_error"}}, status_code=502)

        resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in DROP_RESP}
        if "text/event-stream" in r.headers.get("content-type", ""):
            gone.cancel()  # StreamingResponse watches for the disconnect itself
            stream, r = r, None
            return StreamingResponse(
                stream.aiter_raw(), status_code=stream.status_code, headers=resp_headers,
                background=BackgroundTask(stream.aclose),
            )
        read = asyncio.ensure_future(r.aread())
        if not await _first(read, gone):
            return _aborted(path)
        content = read.result()
    finally:
        gone.cancel()
        if r is not None:
            await r.aclose()
    return Response(content, status_code=r.status_code, headers=resp_headers)


async def _first(work: asyncio.Future, gone: asyncio.Future) -> bool:
    """Wait for work or the disconnect; cancel work and return False on disconnect."""
    await asyncio.wait({work, gone}, return_when=asyncio.FIRST_COMPLETED)
    if work.done():
        return True
    work.cancel()
    return False


def _aborted(path: str) -> Response:
    log.info("client disconnected on %s; aborted upstream request", path)
    return Response(status_code=499)


async def _client_disconnect(request: Request) -> None:
    # The body is already consumed, so the next ASGI message is the disconnect.
    while (await request.receive())["type"] != "http.disconnect":
        pass


def _text(content) -> str | None:
    """Plain text of a message content, or None if it has non-text parts (images, ...)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text" for p in content):
        return "\n\n".join(p.get("text") or "" for p in content)
    return None


def fold_system_prompt(messages: list) -> None:
    if not messages or not isinstance(messages[0], dict) or messages[0].get("role") not in ("system", "developer"):
        return
    system = _text(messages[0].get("content"))
    if system is None:
        return
    user = next((m for m in messages[1:] if isinstance(m, dict) and m.get("role") == "user"), None)
    del messages[0]
    if user is None:
        messages.insert(0, {"role": "user", "content": system})
    elif isinstance(user.get("content"), str):
        user["content"] = f"{system}\n\n{user['content']}"
    elif isinstance(user.get("content"), list):
        user["content"] = [{"type": "text", "text": system + "\n\n"}, *user["content"]]
    else:
        user["content"] = system


def rewrite_chat(payload: dict) -> None:
    """The explicit, documented chat-completion rewrites (see module docstring)."""
    if FOLD_SYSTEM and isinstance(payload.get("messages"), list):
        fold_system_prompt(payload["messages"])
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort in EFFORT_MAP:
        payload["reasoning_effort"] = EFFORT_MAP[effort]
        log.debug("reasoning_effort %s -> %s", effort, payload["reasoning_effort"])
    rf = payload.get("response_format")
    if COMPACT_JSON and isinstance(rf, dict) and rf.get("type") == "json_schema" and "structured_outputs" not in payload:
        schema = (rf.get("json_schema") or {}).get("schema")
        try:
            grammar = grammar_for(schema)
        except (Unsupported, KeyError, TypeError) as exc:
            log.info("json_schema passed through unchanged (%s)", exc)
        else:
            del payload["response_format"]
            payload["structured_outputs"] = {"grammar": grammar}
            log.debug("json_schema %s -> compact grammar", (rf.get("json_schema") or {}).get("name"))
    if DEFAULT_MAX_TOKENS and payload.get("max_tokens") is None and payload.get("max_completion_tokens") is None:
        payload["max_completion_tokens"] = DEFAULT_MAX_TOKENS
