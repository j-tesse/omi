"""Live streaming session: buffer PCM, cut utterances at pauses, transcribe them.

Every CHECK_EVERY_S of new audio, VAD runs over the not-yet-transcribed buffer:
- no speech           -> drop it (keep a short tail so the next word's onset isn't cut)
- speech, then >= HANGOVER_S of silence -> the utterance is complete: transcribe and emit
- buffer >= MAX_UTTERANCE_S -> cut at the last pause (or hard-cut) so latency stays bounded
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .engines import SAMPLE_RATE, Engine
from .pipeline import pcm16_to_float, resample, speech_spans, transcribe_audio

CHECK_EVERY_S = float(os.getenv("STT_STREAM_CHECK_EVERY_S", "0.5"))
HANGOVER_S = float(os.getenv("STT_STREAM_HANGOVER_S", "0.7"))
MAX_UTTERANCE_S = float(os.getenv("STT_STREAM_MAX_UTTERANCE_S", "15"))
KEEP_TAIL_S = 0.5


class StreamSession:
    def __init__(self, engine: Engine, executor: ThreadPoolExecutor, sample_rate: int = 16000):
        self._engine = engine
        self._executor = executor
        self._sr = sample_rate
        self._odd_byte = b""
        self._buf = np.zeros(0, dtype=np.float32)
        self._buf_start_s = 0.0  # absolute stream time of self._buf[0]
        self._since_check = 0

    async def feed(self, data: bytes) -> list[dict]:
        data = self._odd_byte + data
        if len(data) % 2:
            data, self._odd_byte = data[:-1], data[-1:]
        else:
            self._odd_byte = b""
        chunk = resample(pcm16_to_float(data), self._sr)
        self._buf = np.concatenate([self._buf, chunk])
        self._since_check += len(chunk)
        if self._since_check < CHECK_EVERY_S * SAMPLE_RATE:
            return []
        self._since_check = 0
        return await self._run(self._check)

    async def flush(self) -> list[dict]:
        if len(self._buf) == 0:
            return []
        return await self._run(lambda: self._emit(len(self._buf)))

    async def _run(self, fn) -> list[dict]:
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn)

    def _check(self) -> list[dict]:
        n = len(self._buf)
        spans = speech_spans(self._buf)
        keep = int(KEEP_TAIL_S * SAMPLE_RATE)
        if not spans:
            self._drop(max(0, n - keep))
            return []
        if n - spans[-1][1] >= HANGOVER_S * SAMPLE_RATE:
            return self._emit(spans[-1][1])
        if n >= MAX_UTTERANCE_S * SAMPLE_RATE:
            cut = spans[-2][1] if len(spans) > 1 else n
            return self._emit(cut)
        # Still mid-utterance; trim leading silence to keep the buffer small.
        if spans[0][0] > keep:
            self._drop(spans[0][0] - keep)
        return []

    def _emit(self, cut: int) -> list[dict]:
        segments, language = transcribe_audio(self._engine, self._buf[:cut], self._buf_start_s)
        self._drop(cut)
        return [
            {
                **seg,
                # Single label; Omi's backend re-diarizes conversations from embeddings.
                "speaker": "SPEAKER_0",
                "is_user": False,
                "person_id": None,
                "detected_language": language,
            }
            for seg in segments
        ]

    def _drop(self, n: int) -> None:
        if n <= 0:
            return
        self._buf = self._buf[n:]
        self._buf_start_s += n / SAMPLE_RATE
