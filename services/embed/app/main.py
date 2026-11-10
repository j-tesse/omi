"""OpenAI-compatible CPU embedding server (POST /v1/embeddings).

Upstream Omi calls OpenAI text-embedding-3-large in two ways, and both have to
work here:
- LangChain `OpenAIEmbeddings` tokenizes with tiktoken cl100k and sends token-ID
  arrays as `input`, and the openai SDK asks for base64 output by default. We
  decode the IDs back to text (cl100k_base) and support `encoding_format=base64`.
- llm_gateway posts plain strings.
The requested `model` name is ignored and echoed back; EMBED_MODEL is always used.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import tiktoken
import torch
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("embed")
logging.basicConfig(level=os.getenv("EMBED_LOG_LEVEL", "INFO").upper())

MODEL_NAME = os.getenv("EMBED_MODEL") or "BAAI/bge-m3"
THREADS = int(os.getenv("EMBED_THREADS", "4"))
BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "16"))
MAX_TOKENS = int(os.getenv("EMBED_MAX_TOKENS", "8192"))
MAX_INPUTS = 2048  # same limit as OpenAI and upstream's gateway validator

torch.set_num_threads(THREADS)
executor = ThreadPoolExecutor(max_workers=1)  # serialize inference
state: dict[str, Any] = {"model": None, "error": None}
cl100k = tiktoken.get_encoding("cl100k_base")

app = FastAPI(title="omi-embed")


def _load() -> None:
    try:
        from sentence_transformers import SentenceTransformer

        log.info("loading %s (%d threads)", MODEL_NAME, THREADS)
        model = SentenceTransformer(MODEL_NAME, device="cpu")
        model.max_seq_length = min(MAX_TOKENS, model.max_seq_length or MAX_TOKENS)
        model.encode(["warm-up"], normalize_embeddings=True)
        state["model"] = model
        log.info("ready: dim=%d max_seq_length=%d", _dim(model), model.max_seq_length)
    except Exception as exc:  # surfaced through /health
        log.exception("model load failed")
        state["error"] = str(exc)


def _dim(model) -> int:
    get_dim = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return int(get_dim())


@app.on_event("startup")
def _startup() -> None:
    threading.Thread(target=_load, daemon=True).start()


def _error(status: int, message: str, param: str | None = None) -> JSONResponse:
    err_type = "invalid_request_error" if status == 400 else "server_error"
    return JSONResponse({"error": {"message": message, "type": err_type, "param": param, "code": None}}, status_code=status)


def _texts(raw: Any) -> list[str]:
    """OpenAI `input`: str | list[str] | list[int] | list[list[int]] -> texts."""
    if isinstance(raw, str):
        return [raw]
    if not isinstance(raw, list) or not raw:
        raise ValueError("input must be a non-empty string or array")
    if all(isinstance(x, int) for x in raw):
        return [cl100k.decode(raw)]
    texts = []
    for item in raw:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, list) and all(isinstance(x, int) for x in item):
            texts.append(cl100k.decode(item))
        else:
            raise ValueError("input items must be strings or arrays of token IDs")
    return texts


def _encode(texts: list[str], dimensions: int | None) -> tuple[np.ndarray, int]:
    model = state["model"]
    texts = [t.replace("\x00", "") or " " for t in texts]
    vecs = model.encode(texts, batch_size=BATCH_SIZE, normalize_embeddings=True,
                        convert_to_numpy=True, show_progress_bar=False)
    vecs = np.asarray(vecs, dtype=np.float32)
    if dimensions:
        vecs = vecs[:, :dimensions]
        vecs /= np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12)
    tokens = sum(len(ids) for ids in model.tokenizer(texts, add_special_tokens=True, truncation=True,
                                                      max_length=model.max_seq_length)["input_ids"])
    return vecs, tokens


@app.get("/health")
def health():
    if state["model"] is not None:
        return {"status": "ok", "model": MODEL_NAME}
    status = "error" if state["error"] else "loading"
    return JSONResponse({"status": status, "detail": state["error"]}, status_code=503)


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "omi-embed"}]}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    model = state["model"]
    if model is None:
        return _error(503, "model is still loading" if not state["error"] else f"model failed to load: {state['error']}")
    try:
        body = await request.json()
    except ValueError:
        return _error(400, "request body must be JSON")
    if not isinstance(body, dict):
        return _error(400, "request body must be a JSON object")

    try:
        texts = _texts(body.get("input"))
    except ValueError as exc:
        return _error(400, str(exc), "input")
    if len(texts) > MAX_INPUTS:
        return _error(400, f"input must contain at most {MAX_INPUTS} items", "input")

    fmt = body.get("encoding_format") or "float"
    if fmt not in ("float", "base64"):
        return _error(400, "encoding_format must be 'float' or 'base64'", "encoding_format")

    dimensions = body.get("dimensions")
    full = _dim(model)
    if dimensions is not None and (not isinstance(dimensions, int) or not 0 < dimensions <= full):
        return _error(400, f"dimensions must be an integer between 1 and {full}", "dimensions")

    loop = asyncio.get_running_loop()
    vecs, tokens = await loop.run_in_executor(executor, _encode, texts, dimensions)

    def fmt_vec(v: np.ndarray):
        return base64.b64encode(v.astype("<f4").tobytes()).decode() if fmt == "base64" else v.tolist()

    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": fmt_vec(v)} for i, v in enumerate(vecs)],
        "model": body.get("model") or MODEL_NAME,
        "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
    }
