"""Speech-to-text engines behind one small interface.

An engine only has to turn a short (<= ~30 s) mono 16 kHz float32 clip of
speech into text segments. Voice-activity detection, chunking, streaming and
the HTTP/WebSocket protocol live elsewhere and are shared by all engines, so
swapping engines is just `STT_ENGINE=...`.

To add an engine: subclass `Engine`, implement `transcribe`, register it in
`ENGINES`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000


@dataclass
class Segment:
    text: str
    start: float  # seconds, relative to the clip passed to transcribe()
    end: float


class Engine:
    name: str = "base"

    def __init__(self, model: str, threads: int, quant: str, language: Optional[str]):
        self.model = model
        self.threads = threads
        self.quant = quant
        self.language = language

    def transcribe(self, audio: np.ndarray) -> tuple[list[Segment], Optional[str]]:
        """Return (segments, detected_language or None if the engine can't tell)."""
        raise NotImplementedError


class ParakeetEngine(Engine):
    """NVIDIA Parakeet TDT via onnx-asr (ONNX Runtime, CPU). 25 European languages."""

    name = "parakeet"
    default_model = "nemo-parakeet-tdt-0.6b-v3"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        import onnx_asr
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        quant = None if self.quant in ("", "none") else self.quant
        self._model = onnx_asr.load_model(self.model, quantization=quant, sess_options=opts)

    def transcribe(self, audio):
        text = self._model.recognize(audio, sample_rate=SAMPLE_RATE).strip()
        if not text:
            return [], None
        return [Segment(text, 0.0, len(audio) / SAMPLE_RATE)], None


class WhisperEngine(Engine):
    """OpenAI Whisper via faster-whisper (CTranslate2, CPU)."""

    name = "whisper"
    default_model = "large-v3-turbo"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from faster_whisper import WhisperModel

        compute_type = "float32" if self.quant in ("", "none") else self.quant
        self._model = WhisperModel(
            self.model, device="cpu", compute_type=compute_type, cpu_threads=self.threads
        )

    def transcribe(self, audio):
        segments, info = self._model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=False,  # speech is already isolated by the shared VAD
            condition_on_previous_text=False,  # limits runaway repetition/hallucination
        )
        out = [Segment(s.text.strip(), s.start, s.end) for s in segments if s.text.strip()]
        return out, info.language


ENGINES: dict[str, type[Engine]] = {e.name: e for e in (ParakeetEngine, WhisperEngine)}


def load_engine_from_env() -> Engine:
    name = os.getenv("STT_ENGINE", "parakeet").strip().lower()
    if name not in ENGINES:
        raise ValueError(f"STT_ENGINE={name!r}; expected one of {sorted(ENGINES)}")
    cls = ENGINES[name]
    model = os.getenv("STT_MODEL", "").strip() or cls.default_model
    engine = cls(
        model=model,
        threads=int(os.getenv("STT_THREADS", "4")),
        quant=os.getenv("STT_QUANT", "int8").strip().lower(),
        language=os.getenv("STT_LANGUAGE", "").strip() or None,
    )
    logger.info("Loaded STT engine=%s model=%s threads=%s quant=%s",
                engine.name, engine.model, engine.threads, engine.quant)
    return engine
