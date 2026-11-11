"""Test-side stand-in for upstream `utils.llm.clients.get_llm`, wired through the gateway emulation.

Upstream's managed `get_llm(feature)` returns a ChatOpenAI with model
"omi:auto:<feature>" pointed at llm_gateway (utils/llm/providers.py
`get_or_create_omi_gateway_llm`), binds prompt_cache_key / prompt_cache_options,
and the gateway forwards to OPENAI_BASE_URL. Here the ChatOpenAI talks to
llm-proxy directly through `gateway_shape.GatewayTransport`, which applies the
gateway's request rewrite, and every provider exchange is recorded.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

import gateway_shape

BASE_URL = os.getenv("LLM_BASE_URL", "http://127.0.0.1:8101/v1").rstrip("/")


@dataclass
class Exchange:
    feature: str
    request: dict
    status: int = 0
    seconds: float = 0.0
    ttfb: float = 0.0
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    content: str = ""
    reasoning_chars: int = 0
    error: str = ""


class RecordingTransport(gateway_shape.GatewayTransport):
    def __init__(self, route, exchanges: list[Exchange]):
        super().__init__(route, log=[])
        self.exchanges = exchanges

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not (request.method == "POST" and request.url.path.endswith("/chat/completions")):
            return self.inner.handle_request(request)
        body = gateway_shape.shape(self.route, json.loads(request.read()))
        ex = Exchange(feature=self.route.feature, request=body)
        self.exchanges.append(ex)
        headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
        out = httpx.Request("POST", request.url, headers=headers, content=json.dumps(body).encode(), extensions=request.extensions)
        t0 = time.monotonic()
        resp = self.inner.handle_request(out)
        ex.ttfb = time.monotonic() - t0
        ex.status = resp.status_code
        hdrs = [(k, v) for k, v in resp.headers.items() if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")]
        if "text/event-stream" in resp.headers.get("content-type", ""):
            return httpx.Response(resp.status_code, headers=hdrs, stream=_SSERecorder(resp, ex, t0), extensions=resp.extensions)
        raw = resp.read()
        resp.close()
        ex.seconds = time.monotonic() - t0
        _record_json(ex, raw)
        return httpx.Response(resp.status_code, headers=hdrs, content=raw, extensions=resp.extensions)


def _record_json(ex: Exchange, raw: bytes) -> None:
    try:
        data = json.loads(raw)
    except ValueError:
        ex.error = raw[:300].decode(errors="replace")
        return
    if "error" in data or ex.status >= 400:
        ex.error = json.dumps(data.get("error", data))[:500]
        return
    ch = (data.get("choices") or [{}])[0]
    msg = ch.get("message") or {}
    ex.finish_reason = ch.get("finish_reason")
    ex.usage = data.get("usage") or {}
    ex.content = msg.get("content") or ""
    if msg.get("tool_calls"):
        ex.content += json.dumps(msg["tool_calls"])
    ex.reasoning_chars = len(msg.get("reasoning") or msg.get("reasoning_content") or "")


class _SSERecorder(httpx.SyncByteStream):
    def __init__(self, resp: httpx.Response, ex: Exchange, t0: float):
        self.resp, self.ex, self.t0 = resp, ex, t0
        self.first: float | None = None

    def __iter__(self):
        buf = b""
        for chunk in self.resp.iter_raw():
            buf += chunk
            yield chunk
        self.ex.seconds = time.monotonic() - self.t0
        for line in buf.decode(errors="replace").splitlines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            try:
                d = json.loads(line[6:])
            except ValueError:
                continue
            if d.get("usage"):
                self.ex.usage = d["usage"]
            for c in d.get("choices", []):
                delta = c.get("delta") or {}
                if (delta.get("content") or delta.get("tool_calls")) and self.first is None:
                    self.first = time.monotonic() - self.t0
                    self.ex.ttfb = self.first  # time to first content/tool token
                self.ex.content += delta.get("content") or ""
                if delta.get("tool_calls"):
                    self.ex.content += json.dumps(delta["tool_calls"])
                self.ex.reasoning_chars += len(delta.get("reasoning") or delta.get("reasoning_content") or "")
                self.ex.finish_reason = c.get("finish_reason") or self.ex.finish_reason

    def close(self):
        self.resp.close()


class Session:
    """Per-case context: hands out get_llm clients and collects their exchanges."""

    def __init__(self):
        self.exchanges: list[Exchange] = []
        self.routes: list[gateway_shape.Route] = []

    def get_llm(
        self,
        feature: str,
        streaming: bool = False,
        cache_key: str | None = None,
        prompt_cache_options: dict | None = None,
        request_timeout: float | None = None,
        max_retries: int | None = None,
        *,
        uid: str | None = None,
    ):
        route = gateway_shape.route_for(feature, streaming=streaming, client_timeout_s=request_timeout)
        self.routes.append(route)
        # Generous transport timeout (3x upstream's budget, >= 120 s): the harness measures latency
        # and compares it to the budget afterwards instead of aborting at it.
        timeout = max(3 * route.budget_s, 120.0)
        http = httpx.Client(transport=RecordingTransport(route, self.exchanges), timeout=httpx.Timeout(timeout, connect=5))
        kwargs: dict = dict(
            model=route.lane,
            base_url=BASE_URL,
            api_key=SecretStr("omi-gateway"),
            http_client=http,
            max_retries=0,
        )
        if streaming:
            kwargs.update(streaming=True, stream_usage=True)
        llm = ChatOpenAI(**kwargs)
        params: dict = {}
        if cache_key:  # supports_prompt_cache: gpt-5* and gpt-6-luna
            params["prompt_cache_key"] = cache_key
        if prompt_cache_options and route.model == "gpt-6-luna":
            params["extra_body"] = {"prompt_cache_options": prompt_cache_options}
        return llm.bind(**params) if params else llm
