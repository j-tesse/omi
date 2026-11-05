# Omi AI Local Setup

This project aims to run as much as possible of the Omi AI transcription pendant's backend and processing pipeline locally on this server.

## 🎯 Goals
- Local transcription and processing
- Maximum privacy by minimizing cloud dependency
- Full control over the AI pipeline
- Integration with existing vLLM container for model inference

## 🖥 Host Environment
- **GPU:** 1× NVIDIA RTX 5090 (32 GB) — reserved for vLLM (~31.7 GB used). **Decision: vLLM keeps the GPU; STT and everything else run on CPU.**
- **CPU:** AMD Ryzen 9 9900X (Zen 5, 12 cores / 24 threads, AVX-512 incl. VNNI/BF16). Mostly idle at baseline.
- **RAM:** 91 GB total; vLLM container holds ~54 GB, ~56 GB available for new services.
- **Existing vLLM container:** `vllm_gemma4-31b-nvfp4-mtp-ram`
  - Endpoint: `http://localhost:8000/v1` (OpenAI-compatible)
  - Model: `RedHatAI/gemma-4-31B-it-NVFP4`, `max_model_len` 90000
- **Ports already in use** (do not reuse):
  | Port(s) | Service |
  |---|---|
  | 80, 443 | `webserver_nginx` (reverse proxy — use it for TLS/exposure of new services) |
  | 8000 | vLLM |
  | 8080 | Open WebUI (`owui`) |
  | 3978, 18789–18790 | openclaw gateway |
  | 3010, 3011 (172.17.0.1) | buzz relays |
  | 8100 (127.0.0.1) | **this project:** local STT service (`stt`) |

## ☁️ Cloud Dependencies → Local Replacements
All supporting services run as Docker containers. Vector search performance is not a priority — choose the simplest option that works.

| Upstream dependency | Purpose | Local replacement | Status |
|---|---|---|---|
| Deepgram / Soniox / Modulate | Speech-to-text | `stt` service (`services/stt/`): Parakeet TDT v3 now, Whisper large-v3-turbo selectable | Running; not yet wired to the Omi backend |
| OpenAI | LLM (summaries, chat, extraction) | Local vLLM (see above) | TBD |
| Pinecone | Vector search | Simple local container (e.g. Qdrant or pgvector) | TBD |
| Redis | Cache / queues | Redis container | TBD |
| Firebase (Auth, Firestore) | Auth & database | TBD | TBD |
| Google Cloud Storage | Audio/file storage | TBD (e.g. MinIO container or local disk) | TBD |
| Hugging Face models | Speaker diarization / VAD | Locally downloaded models | TBD |

## 🛠 Setup & Installation
1. `cp .env.example .env` and adjust (all variables are documented in `.env.example`; never commit `.env`).
2. `docker compose up -d --build` — models download into `.cache/models/` on first start.

### Speech-to-text service (`services/stt/`)
- CPU-only FastAPI server, **wire-compatible with upstream Omi's self-hosted Parakeet service** (`backend/parakeet/` upstream), so the Omi backend uses it through its built-in Parakeet provider with no code changes: `STT_SERVICE_MODELS=parakeet`, `HOSTED_PARAKEET_API_URL=http://stt:8000`.
- Endpoints: `GET /health`, `POST /v1/transcribe` and `/v2/transcribe` (multipart `file`), `WS /v3/stream` (PCM16 in, segment JSON out, text `finalize` to end). `/v2/language` returns 503, which the backend treats as "language gate unavailable".
- **Swapping the model:** set `STT_ENGINE=parakeet|whisper` (optionally `STT_MODEL`) in `.env`, then `docker compose up -d stt`. Both engines are in the same image. VAD, chunking and streaming are shared (`app/pipeline.py`, `app/stream.py`); engines only implement `transcribe(clip)` (`app/engines.py`). To add an engine, subclass `Engine` and register it in `ENGINES`.
- Speaker labels: streaming sends `SPEAKER_0` for every segment; batch sends no speaker, so the backend clusters speakers itself. Server-side diarization is not implemented.
- Inference is serialized (one worker, `STT_THREADS` cores), which is enough for a single pendant.

## 🚀 Commands
- **Build:** `docker compose build`
- **Run:** `docker compose up -d` (logs: `docker compose logs -f stt`)
- **Test STT service:** `scripts/test_stt.sh [AUDIO_FILE] [STREAM_SPEED]` — health, `/v1` + `/v2` batch, and `/v3/stream` replay (via `services/stt/tools/stream_client.py`).
- **Benchmark STT on CPU:** `scripts/bench_stt.sh [AUDIO_FILE]` — runs faster-whisper and Parakeet (onnx-asr) in a Docker container across thread counts; writes `results/bench_stt/<timestamp>/` (JSONL, summary table, transcripts). Env overrides: `THREADS`, `MODELS`, `LANGUAGE`, `MIN_DURATION`, `REFERENCE` (correct transcript → word error rate), `NOISE` (`white`/`pink`/noise recording) and `SNRS` (e.g. `"clean 10 5 0 noise-only"`) for noise robustness. Without an audio file it loops a short English sample with a built-in reference. Models cache in `.cache/models/`.

## 📂 Project Structure
- `/` : Git repository root containing all necessary scripts and configurations.
- `docker-compose.yml`, `.env.example` : Service definitions and documented configuration.
- `services/stt/` : Local STT service (`app/` server code, `tools/` test clients, `Dockerfile`).
- `scripts/` : Persisted helper scripts (`bench_stt.sh` with its container in `scripts/bench_stt/`, `test_stt.sh`).
- `.cache/`, `data/`, `results/` : Local model cache, sample audio and benchmark output (git-ignored).
- Upstream code (e.g. `BasedHardware/omi`) is to be added as a git submodule, not copied.

## 📌 Status / Decisions
*(Record what currently runs locally vs. in the cloud, and why key choices were made.)*

- **STT requirements:** mostly English, occasionally German; **must tolerate noise** (pendant audio: background noise, silences, clothing rustle).
- **STT candidates (CPU, int8, 4 threads):** Parakeet TDT 0.6B v3 (~45× real-time, ~0.09 cores/stream) vs. faster-whisper large-v3-turbo (~3.6× real-time, ~1.1 cores/stream). Both are affordable.
- **Preliminary noise test** (JFK sample + synthetic pink noise): turbo held 0% WER down to 0 dB SNR, while Parakeet degraded (6% at 10 dB, 13% at 0 dB). Neither invented words from noise alone. *Caveat:* the JFK quote is famous and is probably in Whisper's training data, and pink noise isn't realistic, so confirm with real pendant recordings before choosing.
- **Decision: run Parakeet v3 now, keep Whisper turbo one setting away** (`STT_ENGINE=whisper`). Revisit after testing real noisy pendant audio. Measured streaming delay after a sentence ends, at 4 threads: Parakeet ~1 s, turbo ~4–5 s.
- The STT service mirrors the upstream protocol as of Nov 2026 (`backend/parakeet/main.py`, `backend/utils/stt/streaming.py` `ParakeetWebSocketSocket`, `backend/utils/stt/pre_recorded.py`). Re-check it when bumping the upstream submodule.

## 📝 Guidelines
- **Living Document:** This file must be kept accurate and in sync with the actual state of the project.
- Prefer local-first solutions.
- Run supporting services as Docker containers.
- Document all environment variable requirements.
- Keep the setup reproducible.
- **Coding Preferences:**
    - Prefer git submodules over copying files/folders.
    - Prefer creating persisted scripts over executing many ad-hoc queries.
