#!/usr/bin/env bash
# Benchmark CPU speech-to-text candidates (faster-whisper, Parakeet via onnx-asr)
# for speed and, optionally, accuracy and noise robustness.
#
# Usage:
#   scripts/bench_stt.sh [AUDIO_FILE]
#
# Without AUDIO_FILE, a short public sample (JFK, 11 s, English) is downloaded
# and looped to MIN_DURATION seconds, with its reference transcript filled in
# automatically. For real decisions, pass a representative pendant recording
# (any format ffmpeg reads) plus REFERENCE=<its correct transcript>.
#
# Environment overrides:
#   THREADS="4 8 12"        thread counts to test (container is also capped to this many CPUs)
#   MODELS="..."            space-separated engine:model[:quant] entries (see default below)
#   LANGUAGE=en             force Whisper language (default: auto-detect)
#   MIN_DURATION=300        loop audio shorter than this (seconds)
#   REFERENCE=file.txt      correct transcript of AUDIO_FILE -> enables WER
#   NOISE=pink              noise to mix in: 'white', 'pink', or a noise recording
#                           (e.g. street/cafe/pocket-rustle audio without speech)
#   SNRS="clean 10 5 0"     conditions to test when NOISE is set. Each is 'clean',
#                           a signal-to-noise ratio in dB (lower = noisier), or
#                           'noise-only' (noise alone: every output word is invented)
#
# Output: results/bench_stt/<timestamp>/{results.jsonl,summary.txt,*.txt transcripts}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="omi-bench-stt:latest"
CACHE_DIR="$ROOT/.cache/models"
THREADS="${THREADS:-4 8 12}"
MODELS="${MODELS:-faster-whisper:small faster-whisper:large-v3-turbo faster-whisper:large-v3 onnx-asr:nemo-parakeet-tdt-0.6b-v3}"
LANGUAGE="${LANGUAGE:-}"
MIN_DURATION="${MIN_DURATION:-300}"
REFERENCE="${REFERENCE:-}"
NOISE="${NOISE:-}"
if [[ -n "$NOISE" ]]; then
  SNRS="${SNRS:-clean 10 5 0 noise-only}"
else
  SNRS="clean"
fi

AUDIO="${1:-}"
if [[ -z "$AUDIO" ]]; then
  SAMPLE_DIR="$ROOT/data/samples"
  AUDIO="$SAMPLE_DIR/jfk.wav"
  if [[ ! -f "$AUDIO" ]]; then
    mkdir -p "$SAMPLE_DIR"
    echo ">> Downloading sample audio to $AUDIO"
    curl -fsSL -o "$AUDIO" https://github.com/ggml-org/whisper.cpp/raw/master/samples/jfk.wav
  fi
  if [[ -z "$REFERENCE" ]]; then
    REFERENCE="$SAMPLE_DIR/jfk.txt"
    echo "And so, my fellow Americans, ask not what your country can do for you, ask what you can do for your country." >"$REFERENCE"
  fi
fi
AUDIO="$(realpath "$AUDIO")"
[[ -f "$AUDIO" ]] || { echo "Audio file not found: $AUDIO" >&2; exit 1; }

# Optional inputs are mounted read-only into the container.
extra_mounts=()
ref_args=()
if [[ -n "$REFERENCE" ]]; then
  REFERENCE="$(realpath "$REFERENCE")"
  extra_mounts+=(-v "$REFERENCE:/input/reference.txt:ro")
  ref_args=(--reference /input/reference.txt)
fi
noise_args=()
if [[ -n "$NOISE" ]]; then
  if [[ "$NOISE" == "white" || "$NOISE" == "pink" ]]; then
    noise_args=(--noise "$NOISE")
  else
    NOISE="$(realpath "$NOISE")"
    [[ -f "$NOISE" ]] || { echo "Noise file not found: $NOISE" >&2; exit 1; }
    extra_mounts+=(-v "$NOISE:/input/noise:ro")
    noise_args=(--noise /input/noise)
  fi
fi
lang_args=()
[[ -n "$LANGUAGE" ]] && lang_args=(--language "$LANGUAGE")

OUT_DIR="$ROOT/results/bench_stt/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR" "$CACHE_DIR"

echo ">> Building $IMAGE"
docker build -q -t "$IMAGE" "$ROOT/scripts/bench_stt" >/dev/null

echo ">> Audio: $AUDIO"
[[ -n "$REFERENCE" ]] && echo ">> Reference: $REFERENCE"
[[ -n "$NOISE" ]] && echo ">> Noise: $NOISE (conditions: $SNRS)"
echo ">> Results: $OUT_DIR"

for entry in $MODELS; do
  IFS=: read -r engine model quant <<<"$entry"
  quant="${quant:-int8}"
  safe_model="${model//\//_}"
  for t in $THREADS; do
    for snr in $SNRS; do
      echo ">> $engine $model ($quant) threads=$t condition=$snr"
      cond_args=(--snr "$snr")
      [[ "$snr" != "clean" ]] && cond_args+=("${noise_args[@]}")
      # First run of a model downloads it into $CACHE_DIR; download time is
      # only counted in load_s, never in wall_s.
      if ! docker run --rm \
          --cpus="$t" \
          --user "$(id -u):$(id -g)" \
          -e HOME=/tmp \
          -v "$CACHE_DIR:/cache" \
          -v "$AUDIO:/input/audio:ro" \
          -v "$OUT_DIR:/out" \
          "${extra_mounts[@]}" \
          "$IMAGE" \
          --engine "$engine" --model "$model" --quant "$quant" --threads "$t" \
          --audio /input/audio --min-duration "$MIN_DURATION" \
          --transcript-out "/out/${engine}_${safe_model}_${quant}_t${t}_${snr}.txt" \
          "${cond_args[@]}" "${ref_args[@]}" "${lang_args[@]}" \
          | tee -a "$OUT_DIR/results.jsonl"; then
        echo "!! $engine $model threads=$t condition=$snr failed" >&2
      fi
    done
  done
done

echo
python3 -I "$ROOT/scripts/bench_stt/summarize.py" "$OUT_DIR/results.jsonl" | tee "$OUT_DIR/summary.txt"
echo
echo "x_rt = times faster than real-time; cores/stream = CPU cores needed for one live stream;"
echo "snr = signal-to-noise in dB (lower = noisier); wer = word error rate (lower = better);"
echo "for 'noise-only' rows, 'words' counts invented words (ideally 0)."
