"""Local CPU STT server, wire-compatible with Omi's self-hosted Parakeet service.

Omi's backend talks to it through its built-in Parakeet provider:
    STT_SERVICE_MODELS=parakeet
    HOSTED_PARAKEET_API_URL=http://<this service>:8000

Endpoints (same shapes as upstream backend/parakeet/main.py):
    GET  /health
    POST /v1/transcribe   multipart 'file' -> {"text", "segments": [{text,start,end}]}
    POST /v2/transcribe   same + "detected_language" (no speaker labels: the
                          backend clusters speakers itself when none are given)
    POST /v2/language     503 -> backend treats the language gate as unavailable
    WS   /v3/stream       ?sample_rate=16000; server sends {"type":"ready"}, client
                          sends PCM16 mono bytes and finally the text "finalize";
                          server sends segment JSON objects.

Which model runs is chosen by STT_ENGINE (parakeet | whisper); see engines.py.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import FastAPI, File, Form, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from faster_whisper import decode_audio

from .engines import SAMPLE_RATE, Engine, load_engine_from_env
from .pipeline import transcribe_audio
from .stream import StreamSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("stt")

app = FastAPI(title="omi-local-stt")

# One worker: inference is serialized so CPU use stays at STT_THREADS cores.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
_engine: Optional[Engine] = None
_load_error: Optional[str] = None


def _load() -> None:
    global _engine, _load_error
    try:
        _engine = load_engine_from_env()
    except Exception as e:  # surfaced via /health
        logger.exception("Failed to load STT engine")
        _load_error = f"{type(e).__name__}: {e}"


@app.on_event("startup")
def _startup() -> None:
    # Load in the background so /health can report 'loading' during model download.
    threading.Thread(target=_load, daemon=True).start()


def _not_ready() -> Optional[JSONResponse]:
    if _engine is None:
        status = "unhealthy" if _load_error else "loading"
        return JSONResponse(status_code=503, content={"detail": f"Model {status}", "error": _load_error})
    return None


@app.get("/health")
def health():
    if (resp := _not_ready()) is not None:
        return resp
    return {"status": "healthy", "ready": True, "engine": _engine.name, "model": _engine.model}


async def _transcribe_upload(file: UploadFile) -> tuple[list[dict], str]:
    data = await file.read()
    loop = asyncio.get_running_loop()

    def work():
        audio = decode_audio(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
        return transcribe_audio(_engine, audio)

    return await loop.run_in_executor(_executor, work)


@app.post("/v1/transcribe")
async def transcribe_v1(file: UploadFile = File(...)):
    if (resp := _not_ready()) is not None:
        return resp
    segments, _ = await _transcribe_upload(file)
    return {"text": " ".join(s["text"] for s in segments), "segments": segments}


@app.post("/v2/transcribe")
async def transcribe_v2(
    file: UploadFile = File(...),
    diarize: bool = Form(True),  # accepted for compatibility; diarization is left to the backend
    num_speakers: Optional[int] = Form(None),
    min_speakers: Optional[int] = Form(None),
    max_speakers: Optional[int] = Form(None),
):
    if (resp := _not_ready()) is not None:
        return resp
    segments, language = await _transcribe_upload(file)
    return {
        "text": " ".join(s["text"] for s in segments),
        "segments": segments,
        "detected_language": language,
    }


@app.post("/v2/language")
async def language_v2(file: UploadFile = File(...)):
    return JSONResponse(status_code=503, content={"detail": "Language gate unavailable", "error": "lid_unavailable"})


@app.websocket("/v3/stream")
async def stream(websocket: WebSocket, sample_rate: int = Query(16000)):
    await websocket.accept()
    if _engine is None:
        await websocket.close(code=1013, reason="service_not_ready")
        return
    session = StreamSession(_engine, _executor, sample_rate=sample_rate)
    finalize = False
    await websocket.send_json({"type": "ready"})
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("bytes"):
                for seg in await session.feed(msg["bytes"]):
                    await websocket.send_json(seg)
            elif msg.get("text") == "finalize":
                finalize = True
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("v3/stream error")
    try:
        for seg in await session.flush():
            await websocket.send_json(seg)
        if finalize:
            await websocket.close(code=1000)
    except Exception:
        pass  # client already gone
