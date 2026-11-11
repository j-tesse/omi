"""Reproduce what upstream's llm_gateway sends to OPENAI_BASE_URL for a feature.

Managed backend calls never go to the provider directly: get_llm(feature)
returns a ChatOpenAI pointed at llm_gateway with model "omi:auto:<feature>",
and the gateway rebuilds the provider request (backend/llm_gateway/gateway/
executor.py `_provider_request`, cache_policy.py, providers.py). This module
applies the same rewrite to the request a real langchain/openai client
produced, as an httpx transport, so the harness exercises the exact wire shape
vLLM will see once the gateway is wired up:

  * model           -> route model from config/generated_route_overrides.yaml
  * reasoning_effort-> route default (none|minimal|low|medium|high), caller value wins
  * prompt cache    -> per config/cache_policies.yaml: `explicit` lanes on gpt-6-luna get
                       prompt_cache_options {mode: explicit, ttl: 30m} and, for a
                       >=1024-token system prefix, a `prompt_cache_breakpoint` on the last
                       prefix content part plus prompt_cache_key; `off` lanes get the options
                       only; gpt-5-nano gets neither
  * gpt-6-luna sanitizer -> drop top_p/stop, temperature != 1, force effort `none` with tools
  * timeouts        -> gateway->provider deadline: route request_timeout_ms, else 30 s
                       (120 s for streaming lanes); backend->gateway: 15 s first byte
                       (OMI_LLM_GATEWAY_FIRST_BYTE_TIMEOUT_SECONDS), 60 s for the
                       foreground features (model_config._FOREGROUND_TIMEOUT_FEATURES)

The emulation is reviewed against the submodule pin in AGENTS.md; re-check on bumps.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

GATEWAY_CONFIG = Path("/upstream/backend/llm_gateway/config")
EXPLICIT_CACHE_OPTIONS = {"mode": "explicit", "ttl": "30m"}
EXPLICIT_CACHE_BREAKPOINT = {"mode": "explicit"}
FOREGROUND_FEATURES = {"conv_structure", "conv_app_result", "daily_summary", "memory_l1"}
CLIENT_FIRST_BYTE_S = 15.0
FOREGROUND_S = 60.0


@dataclass(frozen=True)
class Route:
    feature: str
    model: str
    effort: str | None
    cache_policy: str
    gateway_timeout_s: float
    client_timeout_s: float

    @property
    def lane(self) -> str:
        return "omi:auto:" + self.feature.replace("_", "-")

    @property
    def budget_s(self) -> float:
        """Latency the backend tolerates before one of the two hops times out."""
        return min(self.gateway_timeout_s, self.client_timeout_s)


def _overrides() -> dict[str, dict]:
    data = yaml.safe_load((GATEWAY_CONFIG / "generated_route_overrides.yaml").read_text())
    return {o["feature"]: o for o in data["generated_route_overrides"]}


def _cache_policy(lane: str) -> str:
    p = yaml.safe_load((GATEWAY_CONFIG / "cache_policies.yaml").read_text())
    if lane in p.get("lanes", {}):
        return p["lanes"][lane]
    for prefix, policy in p.get("prefixes", {}).items():
        if lane.startswith(prefix):
            return policy
    return p["default"]


def route_for(feature: str, *, streaming: bool = False, client_timeout_s: float | None = None) -> Route:
    o = _overrides()[feature]
    lane = "omi:auto:" + feature.replace("_", "-")
    gw = o.get("request_timeout_ms") or (120000 if streaming else 30000)
    client = client_timeout_s or (FOREGROUND_S if feature in FOREGROUND_FEATURES else CLIENT_FIRST_BYTE_S)
    if streaming:  # the client read timeout is the max *silent* interval while streaming
        client = max(client, gw / 1000)
    return Route(
        feature=feature,
        model=o["primary"]["model"],
        effort=(o.get("provider_options") or {}).get("reasoning_effort"),
        cache_policy=_cache_policy(lane),
        gateway_timeout_s=gw / 1000,
        client_timeout_s=client,
    )


def _prefix_tokens(prefix: list, tools) -> int:  # gateway heuristic: serialized chars / 4
    return len(json.dumps([prefix, tools], ensure_ascii=False, separators=(",", ":"))) // 4


def shape(route: Route, body: dict) -> dict:
    """Turn a client request body into the gateway's provider request body."""
    req = copy.deepcopy(body)
    caller_effort = req.get("reasoning_effort")
    req["model"] = route.model
    if route.effort is not None:
        req["reasoning_effort"] = route.effort
    if caller_effort is not None:  # forwarded params override provider options
        req["reasoning_effort"] = caller_effort
    luna = route.model == "gpt-6-luna"
    msgs = req.get("messages", [])
    if luna and route.cache_policy == "off":
        req["prompt_cache_options"] = dict(EXPLICIT_CACHE_OPTIONS)
        req.pop("prompt_cache_key", None)
    elif luna and route.cache_policy == "explicit" and "prompt_cache_options" not in req and "prompt_cache_key" not in req:
        req["prompt_cache_options"] = dict(EXPLICIT_CACHE_OPTIONS)
        boundary = 0
        while boundary < len(msgs) and msgs[boundary].get("role") == "system":
            boundary += 1
        prefix = msgs[:boundary]
        if prefix and _prefix_tokens(prefix, req.get("tools")) >= 1024:
            last = prefix[-1]
            if isinstance(last.get("content"), str):
                last["content"] = [
                    {"type": "text", "text": last["content"], "prompt_cache_breakpoint": dict(EXPLICIT_CACHE_BREAKPOINT)}
                ]
            elif isinstance(last.get("content"), list) and last["content"]:
                last["content"][-1]["prompt_cache_breakpoint"] = dict(EXPLICIT_CACHE_BREAKPOINT)
            key = "gateway-" + hashlib.sha256(json.dumps([prefix, req.get("tools")], sort_keys=True).encode()).hexdigest()
            # provider_cache_request scopes the key to the owner
            req["prompt_cache_key"] = hashlib.sha256(json.dumps(["[\"local\",\"uid\"]", key]).encode()).hexdigest()
    if not luna:
        for k in ("prompt_cache_options", "prompt_cache_key", "prompt_cache_retention"):
            req.pop(k, None)
    if luna:  # _sanitize_openai_chat_completions_request
        req.pop("top_p", None)
        req.pop("stop", None)
        if req.get("tools") and req.get("reasoning_effort") not in (None, "none"):
            req["reasoning_effort"] = "none"
        t = req.get("temperature")
        if "temperature" in req and (isinstance(t, bool) or t != 1):
            req.pop("temperature")
    return req


class GatewayTransport(httpx.BaseTransport):
    """httpx transport that rewrites outgoing chat-completion bodies like the gateway does."""

    def __init__(self, route: Route, log: list):
        self.route = route
        self.log = log  # receives every provider request body sent
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/chat/completions"):
            body = shape(self.route, json.loads(request.read()))
            self.log.append(body)
            headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
            request = httpx.Request("POST", request.url, headers=headers, content=json.dumps(body).encode())
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()

