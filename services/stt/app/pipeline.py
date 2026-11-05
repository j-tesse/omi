"""Engine-independent audio pipeline: VAD, chunking, language tagging."""

from __future__ import annotations

import os
from typing import Optional

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from langdetect import DetectorFactory, LangDetectException, detect

from .engines import SAMPLE_RATE, Engine, Segment

DetectorFactory.seed = 0  # deterministic langdetect

VAD_THRESHOLD = float(os.getenv("STT_VAD_THRESHOLD", "0.5"))
# Longest clip handed to an engine in one call (Whisper's window is 30 s).
MAX_WINDOW_S = float(os.getenv("STT_MAX_WINDOW_S", "20"))


def speech_spans(audio: np.ndarray, min_silence_ms: int = 500) -> list[tuple[int, int]]:
    """Speech regions as (start, end) sample indices, each <= MAX_WINDOW_S."""
    opts = VadOptions(
        threshold=VAD_THRESHOLD,
        min_silence_duration_ms=min_silence_ms,
        speech_pad_ms=200,
        max_speech_duration_s=MAX_WINDOW_S,
    )
    return [(t["start"], t["end"]) for t in get_speech_timestamps(audio, opts, SAMPLE_RATE)]


def group_spans(spans: list[tuple[int, int]], max_gap_s: float = 1.0) -> list[tuple[int, int]]:
    """Merge nearby speech spans into windows of up to MAX_WINDOW_S, so engines get context."""
    max_len = int(MAX_WINDOW_S * SAMPLE_RATE)
    max_gap = int(max_gap_s * SAMPLE_RATE)
    windows: list[tuple[int, int]] = []
    for s, e in spans:
        if windows and s - windows[-1][1] <= max_gap and e - windows[-1][0] <= max_len:
            windows[-1] = (windows[-1][0], e)
        else:
            windows.append((s, e))
    return windows


def detect_language(text: str, engine_language: Optional[str]) -> str:
    if engine_language:
        return engine_language
    if len(text.strip()) >= 10:
        try:
            return detect(text)
        except LangDetectException:
            pass
    return "en"


def transcribe_audio(engine: Engine, audio: np.ndarray, offset_s: float = 0.0) -> tuple[list[dict], str]:
    """Transcribe arbitrary-length audio. Returns (segments with absolute times, language)."""
    segments: list[dict] = []
    languages: list[str] = []
    for s, e in group_spans(speech_spans(audio)):
        parts, lang = engine.transcribe(audio[s:e])
        if lang:
            languages.append(lang)
        base = offset_s + s / SAMPLE_RATE
        for p in parts:
            segments.append({
                "text": p.text,
                "start": round(base + p.start, 2),
                "end": round(base + p.end, 2),
            })
    text = " ".join(seg["text"] for seg in segments)
    engine_lang = max(set(languages), key=languages.count) if languages else None
    return segments, detect_language(text, engine_lang)


def pcm16_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def resample(audio: np.ndarray, sr: int) -> np.ndarray:
    """Linear resample to 16 kHz (Omi normally sends 16 kHz already)."""
    if sr == SAMPLE_RATE or len(audio) == 0:
        return audio
    n = int(round(len(audio) * SAMPLE_RATE / sr))
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


__all__ = ["Segment", "transcribe_audio", "speech_spans", "detect_language",
           "pcm16_to_float", "resample", "MAX_WINDOW_S"]
