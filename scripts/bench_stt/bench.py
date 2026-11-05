"""Benchmark one CPU speech-to-text model on one audio file.

Runs a single (engine, model, threads) combination per process so that
CPU time and peak memory are measured in isolation. Prints one JSON line
with the results and writes the transcript to --transcript-out.
"""

import argparse
import json
import re
import resource
import sys
import time

import numpy as np
from faster_whisper import decode_audio

SAMPLE_RATE = 16000


def cpu_seconds() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def load_audio(path: str, min_duration: float) -> tuple[np.ndarray, int]:
    """Return (audio, reps): audio looped `reps` times to reach min_duration."""
    audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
    reps = 1
    if min_duration and len(audio) / SAMPLE_RATE < min_duration:
        reps = int(np.ceil(min_duration * SAMPLE_RATE / len(audio)))
        audio = np.tile(audio, reps)
    return audio.astype(np.float32), reps


def make_noise(spec: str, n: int, seed: int = 0) -> np.ndarray:
    """'white', 'pink', or a path to a noise recording (looped/cropped to n samples)."""
    rng = np.random.default_rng(seed)
    if spec == "white":
        return rng.standard_normal(n).astype(np.float32)
    if spec == "pink":
        # 1/f spectrum: shape white noise in the frequency domain.
        spectrum = np.fft.rfft(rng.standard_normal(n))
        freqs = np.fft.rfftfreq(n)
        freqs[0] = freqs[1]
        return np.fft.irfft(spectrum / np.sqrt(freqs), n).astype(np.float32)
    noise = decode_audio(spec, sampling_rate=SAMPLE_RATE)
    if len(noise) < n:
        noise = np.tile(noise, int(np.ceil(n / len(noise))))
    start = rng.integers(0, len(noise) - n + 1)
    return noise[start:start + n].astype(np.float32)


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)) + 1e-12)


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """Scale noise so that speech-RMS / noise-RMS equals snr_db, then mix."""
    noise = noise * (rms(speech) / (rms(noise) * 10 ** (snr_db / 20)))
    mixed = speech + noise
    peak = np.max(np.abs(mixed))
    return (mixed / peak * 0.99 if peak > 1.0 else mixed).astype(np.float32)


def normalize_words(text: str) -> list[str]:
    text = re.sub(r"[^\w\s']", " ", text.lower())
    return text.split()


def word_error_rate(ref: list[str], hyp: list[str]) -> float:
    """Levenshtein distance over words / reference length."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return prev[-1] / max(len(ref), 1)


def run_faster_whisper(model_name, audio, threads, language, compute_type):
    from faster_whisper import WhisperModel

    t0 = time.perf_counter()
    model = WhisperModel(
        model_name, device="cpu", compute_type=compute_type, cpu_threads=threads
    )
    load_s = time.perf_counter() - t0

    def transcribe():
        segments, info = model.transcribe(
            audio, language=language, beam_size=5, vad_filter=True
        )
        # Segments are a lazy generator; consuming it does the actual work.
        text = " ".join(s.text.strip() for s in segments)
        return text, info.language

    return load_s, transcribe


def run_onnx_asr(model_name, audio, threads, language, quantization):
    import onnx_asr
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = threads
    opts.inter_op_num_threads = 1

    t0 = time.perf_counter()
    model = onnx_asr.load_model(
        model_name, quantization=quantization, sess_options=opts
    )
    vad = onnx_asr.load_vad("silero", sess_options=opts)
    model = model.with_vad(vad)
    load_s = time.perf_counter() - t0

    def transcribe():
        segments = model.recognize(audio, sample_rate=SAMPLE_RATE)
        text = " ".join(s.text.strip() for s in segments)
        return text, language or "auto"

    return load_s, transcribe


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--engine", required=True, choices=["faster-whisper", "onnx-asr"])
    p.add_argument("--model", required=True)
    p.add_argument("--audio", required=True)
    p.add_argument("--threads", type=int, required=True)
    p.add_argument("--language", default=None, help="e.g. nl, en; default auto")
    p.add_argument(
        "--quant",
        default="int8",
        help="faster-whisper compute_type / onnx-asr quantization ('none' = fp32)",
    )
    p.add_argument("--min-duration", type=float, default=0.0,
                   help="Loop short audio until at least this many seconds")
    p.add_argument("--transcript-out", default=None)
    p.add_argument("--noise", default=None,
                   help="'white', 'pink', or path to a noise recording")
    p.add_argument("--snr", default="clean",
                   help="'clean', an SNR in dB (e.g. 5), or 'noise-only' "
                        "(transcribe the noise alone: any output is hallucinated)")
    p.add_argument("--reference", default=None,
                   help="Reference transcript (text file) for WER")
    args = p.parse_args()

    quant = None if args.quant == "none" else args.quant
    audio, reps = load_audio(args.audio, args.min_duration)
    audio_s = len(audio) / SAMPLE_RATE

    if args.snr != "clean":
        if not args.noise:
            p.error("--snr other than 'clean' requires --noise")
        noise = make_noise(args.noise, len(audio))
        if args.snr == "noise-only":
            # Same loudness as the speech would have had, but no speech at all.
            audio = (noise * (rms(audio) / rms(noise))).astype(np.float32)
            peak = np.max(np.abs(audio))
            if peak > 1.0:
                audio = audio / peak * 0.99
        else:
            audio = mix_at_snr(audio, noise, float(args.snr))

    if args.engine == "faster-whisper":
        load_s, transcribe = run_faster_whisper(
            args.model, audio, args.threads, args.language, quant or "float32"
        )
    else:
        load_s, transcribe = run_onnx_asr(
            args.model, audio, args.threads, args.language, quant
        )

    cpu0 = cpu_seconds()
    t0 = time.perf_counter()
    text, lang = transcribe()
    wall_s = time.perf_counter() - t0
    cpu_s = cpu_seconds() - cpu0

    if args.transcript_out:
        with open(args.transcript_out, "w") as f:
            f.write(text + "\n")

    wer = None
    if args.snr == "noise-only":
        wer = None  # no reference: the 'words' count is the hallucination measure
    elif args.reference:
        ref = normalize_words(open(args.reference).read()) * reps
        wer = round(word_error_rate(ref, normalize_words(text)), 4)

    result = {
        "engine": args.engine,
        "model": args.model,
        "quant": args.quant,
        "threads": args.threads,
        "noise": args.noise if args.snr != "clean" else None,
        "snr": args.snr,
        "wer": wer,
        "language": lang,
        "audio_s": round(audio_s, 1),
        "load_s": round(load_s, 2),
        "wall_s": round(wall_s, 2),
        "rtf": round(wall_s / audio_s, 4),
        "x_realtime": round(audio_s / wall_s, 1),
        "avg_cores_used": round(cpu_s / wall_s, 2),
        # Cores needed to keep up with one live stream (CPU-seconds per audio-second).
        "cores_per_live_stream": round(cpu_s / audio_s, 3),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "words": len(text.split()),
    }
    print(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
