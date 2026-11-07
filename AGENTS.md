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
| OpenAI chat (`gpt-6-luna`, `gpt-5-nano`) | LLM (summaries, chat, extraction) | Local vLLM, reached through an alias layer (model names are hardcoded upstream, see below) | TBD |
| OpenAI embeddings (`text-embedding-3-large`) | Memory/conversation vectors | Local CPU embedding server (vLLM serves Gemma only) | TBD |
| Pinecone | Vector search | **Optional upstream**: without `PINECONE_API_KEY` the index is disabled. Keep it off at first; add Qdrant/pgvector only if semantic search turns out to be needed | Off |
| Typesense | Conversation search | `typesense/typesense:27.1` container (upstream harness pin) | TBD |
| Redis | Cache, rate limits, locks | Redis container | TBD |
| Firebase Auth + Firestore | Auth & main database | **Firebase Emulator Suite** (upstream supports it natively via `FIRESTORE_EMULATOR_HOST` / `FIREBASE_AUTH_EMULATOR_HOST`) | TBD |
| Google Cloud Storage | Audio/file storage | **Upstream local filesystem storage** (`OMI_LOCAL_STORAGE_ROOT`, served at `/_local/storage`) | TBD |
| Hugging Face models / diarizer | Speaker diarization / embeddings | Upstream `backend/diarizer/` (GPU) — CPU variant TBD; optional | TBD |
| VAD (Modal GPU) | Voice activity detection | Upstream already falls back to a bundled local ONNX VAD | No action |
| Perplexity, Gemini TTS, Stripe, Twilio, PostHog, LangSmith, OAuth integrations | Optional features | Leave unset (features disabled) | Off |

## 🛠 Setup & Installation
1. `cp .env.example .env` and adjust (all variables are documented in `.env.example`; never commit `.env`).
2. `docker compose up -d --build` — models download into `.cache/models/` on first start.

### Speech-to-text service (`services/stt/`)
- CPU-only FastAPI server, **wire-compatible with upstream Omi's self-hosted Parakeet service** (`backend/parakeet/` upstream), so the Omi backend uses it through its built-in Parakeet provider with no code changes: `STT_SERVICE_MODELS=parakeet`, `HOSTED_PARAKEET_API_URL=http://stt:8000`.
- Endpoints: `GET /health`, `POST /v1/transcribe` and `/v2/transcribe` (multipart `file`), `WS /v3/stream` (PCM16 in, segment JSON out, text `finalize` to end). `/v2/language` returns 503, which the backend treats as "language gate unavailable".
- **Swapping the model:** set `STT_ENGINE=parakeet|whisper` (optionally `STT_MODEL`) in `.env`, then `docker compose up -d stt`. Both engines are in the same image. VAD, chunking and streaming are shared (`app/pipeline.py`, `app/stream.py`); engines only implement `transcribe(clip)` (`app/engines.py`). To add an engine, subclass `Engine` and register it in `ENGINES`.
- Speaker labels: streaming sends `SPEAKER_0` for every segment; batch sends no speaker, so the backend clusters speakers itself. Server-side diarization is not implemented.
- Inference is serialized (one worker, `STT_THREADS` cores), which is enough for a single pendant.

### Upstream Omi (`upstream/omi/`, shallow submodule)
`git submodule update --init` to fetch it. Pinned at `9f2ddcc` (2026-11-06). Findings from mapping `backend/` (re-check them when bumping the submodule):
- **Upstream ships a local emulator harness** (`make dev-up`, `scripts/dev-harness/dev_harness/`, runbook `backend/docs/runbooks/local-emulator-manual-qa.md`). It runs the Firebase emulators, `redis-server`, Typesense, `llm_gateway`, `backend` and `desktop-backend` as **host processes** and injects the env in `config.py` `_harness_service_extra()` / `child_env_for()`. This is the reference for our env: we reproduce that env in Docker containers instead of running the harness. Its default ports (backend **8000** = vLLM's port, Firestore 8085, Auth 9099, Redis 6380, Typesense 8108, gateway 9080, desktop 10201) must be remapped.
- **Env stages** (`OMI_ENV_STAGE`, `backend/utils/env_loader.py`): `local` = emulators plus real providers, which is what we want. `offline` = fake LLMs and stub STT (`OMI_STT_STUB=1`), useful as a boot smoke test.
- **Firebase:** with the emulator env vars set, Firestore and Auth need no Google credentials. Composite indexes come from `firestore.indexes.json` (the emulator doesn't enforce them). The harness seeds test users (`alice@local.omi.invalid` etc.). Signing in the real mobile app against the Auth emulator is still an open question: the app has to be built pointing at the emulator (see `app/README.md`, `docs/doc/developer/AppSetup.mdx`).
- **LLM routing** (`backend/utils/llm/model_config.py`): every feature maps to a hardcoded model, `gpt-6-luna` or `gpt-5-nano`, with provider `openai`, and there is no per-feature env override. Calls go either directly through `ChatOpenAI` or through `llm_gateway` (`OMI_LLM_GATEWAY_FEATURE_MODE=gateway`, the harness default). The gateway reads `OPENAI_BASE_URL`, and its lanes only use those two OpenAI models. `web_search` → Perplexity (leave off). **Plan:** set `OPENAI_BASE_URL` to an alias layer that maps `gpt-6-luna` / `gpt-5-nano` → Gemma on vLLM and `text-embedding-3-large` → a local embedding model. Options: vLLM `--served-model-name` aliases, which would mean changing the vLLM container, or a small LiteLLM-style proxy container. Gemma has to cope with the structured-output features (`_STRUCTURED_OUTPUT_FEATURES`).
- **STT:** `STT_SERVICE_MODELS=parakeet` + `HOSTED_PARAKEET_API_URL` uses our `stt` service, the same keyless chain the offline harness pins. Deepgram was retired upstream on 2026-08-27.
- **Required secrets:** `ENCRYPTION_SECRET` (≥32 bytes, encrypts conversations, memories and chat) and `ADMIN_KEY` (local auth bypass `ADMIN_KEY<uid>`).
- **Open questions:** `HOSTED_PUSHER_API_URL` / `backend/pusher/` isn't started by the harness, but `.env.template` says conversations stay `in_progress` without it. Check whether we need to run the pusher. `desktop-backend` (Rust/desktop) is probably not needed for the pendant.

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
- `upstream/omi/` : `BasedHardware/omi` as a shallow git submodule. Never copy or edit upstream code here; configure it from the outside.

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
