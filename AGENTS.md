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
| 8101 (127.0.0.1) | **this project:** LLM alias proxy (`llm-proxy`) |
| 8102 (127.0.0.1) | **this project:** CPU embedding server (`embed`) |
| 8103–8106 (127.0.0.1) | **this project:** Firebase emulators (`firebase`): Firestore 8103, Auth 8104, UI 8105, UI websocket 8106 |
| 8107 (127.0.0.1) | **this project:** Redis (`redis`) |
| 8108 (127.0.0.1) | **this project:** Typesense (`typesense`) |

## ☁️ Cloud Dependencies → Local Replacements
All supporting services run as Docker containers. Vector search performance is not a priority — choose the simplest option that works.

| Upstream dependency | Purpose | Local replacement | Status |
|---|---|---|---|
| Deepgram / Soniox / Modulate | Speech-to-text | `stt` service (`services/stt/`): Parakeet TDT v3 now, Whisper large-v3-turbo selectable | Running; not yet wired to the Omi backend |
| OpenAI chat (`gpt-6-luna`, `gpt-5-nano`) | LLM (summaries, chat, extraction) | Local vLLM through the `llm-proxy` alias pass-through (`services/llm-proxy/`), since model names are hardcoded upstream (see below) | Running; not yet wired to the Omi backend |
| OpenAI embeddings (`text-embedding-3-large`) | Memory/conversation vectors | `embed` service (`services/embed/`, BGE-M3 on CPU), reached through `llm-proxy` `/v1/embeddings` | Running; not yet wired to the Omi backend |
| Pinecone | Vector search | **Optional upstream**: without `PINECONE_API_KEY` the index is disabled. Keep it off at first; add Qdrant/pgvector only if semantic search turns out to be needed | Off |
| Typesense | Conversation + memory keyword search | `typesense` service (`typesense/typesense:27.1`, upstream harness pin); `typesense-init` creates the `conversations` collection | Running; not yet wired to the Omi backend |
| Redis | Cache, rate limits, locks | `redis` service (`redis:7.4.12-alpine`, AOF persistence) | Running; not yet wired to the Omi backend |
| Firebase Auth + Firestore | Auth & main database | **Firebase Emulator Suite** (`firebase` service, `services/firebase/`). Upstream supports it natively via `FIRESTORE_EMULATOR_HOST` / `FIREBASE_AUTH_EMULATOR_HOST` | Running; not yet wired to the Omi backend |
| Google Cloud Storage | Audio/file storage | **Upstream local filesystem storage** (`OMI_LOCAL_STORAGE_ROOT`, served at `/_local/storage`) | TBD |
| Hugging Face models / diarizer | Speaker diarization / embeddings | Upstream `backend/diarizer/` (GPU) — CPU variant TBD; optional | TBD |
| VAD (Modal GPU) | Voice activity detection | Upstream already falls back to a bundled local ONNX VAD | No action |
| Perplexity, Gemini TTS, Stripe, Twilio, PostHog, LangSmith, OAuth integrations | Optional features | Leave unset (features disabled) | Off |

## 🛠 Setup & Installation
1. `cp .env.example .env` and adjust (all variables are documented in `.env.example`; never commit `.env`).
2. `mkdir -p state/redis state/typesense` (bind-mount targets must exist and be owned by you, see Redis + Typesense below).
3. `docker compose up -d --build` — models download into `.cache/models/` on first start.

### Speech-to-text service (`services/stt/`)
- CPU-only FastAPI server, **wire-compatible with upstream Omi's self-hosted Parakeet service** (`backend/parakeet/` upstream), so the Omi backend uses it through its built-in Parakeet provider with no code changes: `STT_SERVICE_MODELS=parakeet`, `HOSTED_PARAKEET_API_URL=http://stt:8000`.
- Endpoints: `GET /health`, `POST /v1/transcribe` and `/v2/transcribe` (multipart `file`), `WS /v3/stream` (PCM16 in, segment JSON out, text `finalize` to end). `/v2/language` returns 503, which the backend treats as "language gate unavailable".
- **Swapping the model:** set `STT_ENGINE=parakeet|whisper` (optionally `STT_MODEL`) in `.env`, then `docker compose up -d stt`. Both engines are in the same image. VAD, chunking and streaming are shared (`app/pipeline.py`, `app/stream.py`); engines only implement `transcribe(clip)` (`app/engines.py`). To add an engine, subclass `Engine` and register it in `ENGINES`.
- Speaker labels: streaming sends `SPEAKER_0` for every segment; batch sends no speaker, so the backend clusters speakers itself. Server-side diarization is not implemented.
- Inference is serialized (one worker, `STT_THREADS` cores), which is enough for a single pendant.

### LLM alias proxy (`services/llm-proxy/`)
- Small FastAPI pass-through in front of vLLM (and, for `/v1/embeddings`, in front of the `embed` service via `LLM_EMBEDDINGS_URL`). It replaces the `model` field of every JSON request on `/v1/*` per `LLM_MODEL_ALIASES` (default `gpt-6-luna` and `gpt-5-nano` → `RedHatAI/gemma-4-31B-it-NVFP4`) and forwards everything else unchanged. Responses, including SSE streams, are relayed as-is, so they report the real model name.
- Unlisted model names pass through untouched. A `*=<target>` entry catches all of them. `text-embedding-3-large=BAAI/bge-m3` is in the default aliases, though that's only cosmetic because the embed server ignores the model name.
- `GET /v1/models` lists the upstream models, the embed server's model (best effort) and the aliases. `GET /health` checks that the upstream is reachable.
- It reaches vLLM at `http://host.docker.internal:8000/v1` (vLLM runs on the default bridge network). Client `Authorization` headers are dropped; `LLM_UPSTREAM_API_KEY` is sent instead, if set.
- Wiring (pending): set `OPENAI_BASE_URL=http://llm-proxy:8000/v1` and a dummy `OPENAI_API_KEY` for the backend and `llm_gateway`. The gateway posts to `{OPENAI_BASE_URL}/chat/completions` (`backend/llm_gateway/gateway/providers.py`).

### Embedding service (`services/embed/`)
- CPU-only FastAPI server with OpenAI's `POST /v1/embeddings` interface, running `sentence-transformers` on CPU torch. Default model `BAAI/bge-m3`: multilingual (EN/DE), 8192-token context (OpenAI's is 8191), 1024 dims, and no query/passage prefixes, which matters because the OpenAI API can't say which kind of text it sends. Also `GET /health` (503 while loading) and `GET /v1/models`.
- **Why custom instead of TEI/Infinity:** upstream's direct path (LangChain `OpenAIEmbeddings`) sends **cl100k token-ID arrays** as `input`, and the openai SDK asks for **`encoding_format=base64`**. The server decodes the IDs with tiktoken `cl100k_base` (baked into the image, so no network is needed at runtime) and returns little-endian float32 base64. The gateway path (`llm_gateway` → `{OPENAI_BASE_URL}/embeddings`) sends plain strings. `dimensions` (truncate + re-normalize) is supported. The `model` field is ignored and echoed back.
- No backend setting is needed: both paths use `OPENAI_BASE_URL`, and `llm-proxy` routes `/v1/embeddings` to `http://embed:8000/v1`.
- **Vector size is 1024, not 3072.** That doesn't matter while Pinecone is off (upstream still calls `generate_embedding()` for every finished conversation, so the server is needed anyway). Changing `EMBED_MODEL` after a vector store exists means re-indexing it.
- Swap the model with `EMBED_MODEL` (any sentence-transformers model), then `docker compose up -d embed`. Inference is serialized (one worker, `EMBED_THREADS` cores).

### Firebase emulators (`services/firebase/`)
- Firestore + Auth emulators plus the Emulator UI, from `firebase-tools` (pinned to upstream's `package.json` version via the `FIREBASE_TOOLS_VERSION` build arg, currently `15.22.0`) on `node:22-trixie-slim` with JDK 21. The emulator jars are baked into the image, so startup needs no network. This mirrors the upstream harness's `_firebase_command` (`scripts/dev-harness/dev_harness/cli.py`), but runs as a container.
- Project `demo-omi-local` (`FIREBASE_PROJECT_ID`, must start with `demo-`), database `(default)`. No Google credentials are involved.
- **Fixed ports, published 1:1 on 127.0.0.1:** Firestore 8103, Auth 8104, UI 8105, Firestore UI websocket 8106 (hub 4400 and logging 4500 stay internal). They must be the same inside and outside the container because the UI makes the browser connect to the emulators on their configured ports. To change them, edit `services/firebase/firebase.json` and the compose `ports` together. The UI is reachable remotely through an SSH tunnel for 8103–8106.
- Rules and indexes are bind-mounted read-only from the submodule (`upstream/omi/firestore.rules`, `firestore.indexes.json`), not copied. The backend's Admin SDK bypasses the rules anyway, and the emulator doesn't enforce the indexes.
- **Persistence:** `state/firebase/export/` (git-ignored). It is imported on start and exported on a **clean** stop (`docker compose stop/restart/down`; `init: true` + `stop_grace_period: 60s`). A crash, `kill`, or OOM loses everything written since the last clean stop. The export goes into a subdirectory because firebase-tools replaces its target directory, which fails on a mount point. To wipe the data: stop the service, then `rm -rf state/firebase/export`.
- `FIREBASE_JAVA_OPTS` (default `-Xmx1g`) caps the Firestore JVM heap.
- Wiring (pending; values from the harness `_harness_service_extra()`): `FIRESTORE_EMULATOR_HOST=firebase:8103`, `FIREBASE_AUTH_EMULATOR_HOST=firebase:8104`, `FIREBASE_PROJECT_ID` = `FIREBASE_AUTH_PROJECT_ID` = `demo-omi-local`, `FIRESTORE_DATABASE_ID=(default)`, `FIREBASE_API_KEY=local-firebase-auth-emulator-api-key`. A non-loopback emulator host is fine: we checked that `google.cloud.firestore.Client(project=...)` (the backend's `_build_firestore_client` path) and `firebase_admin.auth` work against `firebase:8103/8104` from another container with no credentials. Note that `firebase_admin.firestore.client()` does ask for credentials, but the backend doesn't use it.
- Not included: the Storage emulator (upstream uses local filesystem storage) and seeding test users (the harness seeds through backend code; revisit once the backend container exists). Making the Auth emulator reachable from the phone is still open.

### Redis + Typesense (`redis`, `typesense`, `typesense-init`; `services/typesense/`)
- Stock images, no build: `redis:7.4.12-alpine` and `typesense/typesense:27.1` (the upstream harness pin, `TYPESENSE_PINNED_VERSION` in `scripts/dev-harness/dev_harness/config.py`). Both run as uid 1000 with `restart: unless-stopped` and a healthcheck (`redis-cli ping`; Typesense `/health` over bash `/dev/tcp`, since the image has no curl).
- **Redis:** in-network `redis:6379`, host `127.0.0.1:8107`, **no password** (like the harness). The backend always sends `AUTH default <REDIS_DB_PASSWORD>` (`backend/database/redis_connection.py`), which works because the default user has `nopass`; checked with redis-py. `--protected-mode no` is needed so other containers can connect without a password; the host port is loopback-only. The kernel warning about `vm.overcommit_memory` is harmless at this size.
- **Typesense:** in-network `http://typesense:8108`, host `127.0.0.1:8108`, admin key `TYPESENSE_API_KEY` from `.env` (required; compose refuses to start without it). CORS is on (like the harness).
- **Collections:** upstream **reads and upserts the `conversations` collection but never creates it** (`backend/utils/conversations/search.py`, `typesense_index.py`; the docs say to create it by hand from `backend/typesense/conversations.schema`), and the harness doesn't create it either. The one-shot `typesense-init` service (`curlimages/curl:8.17.0`, script `services/typesense/init.sh`) runs after Typesense is healthy on every `up`, creates each mounted `*.schema` collection that is missing, and leaves existing ones alone. The schema is bind-mounted from the submodule, not copied. The memory collection `canonical_memory_atoms` is created by the backend itself on first write (`ensure_memories_collection()` in `backend/utils/memory/atom_keyword_index.py`), so init skips it. When the backend container exists, it should `depends_on: typesense-init: condition: service_completed_successfully`.
- **Persistence:** `state/redis/` (AOF `appendonlydir/` + RDB snapshot every 60 s if anything changed) and `state/typesense/` (git-ignored). Create both directories before the first start (`mkdir -p state/redis state/typesense`), otherwise Docker creates them root-owned and the uid-1000 containers can't write. To wipe: stop the service, `rm -rf` its directory, recreate it.
- Wiring (pending; keys from the harness `_harness_service_extra()`): `REDIS_DB_HOST=redis`, `REDIS_DB_PORT=6379`, `REDIS_DB_PASSWORD=` (empty), `TYPESENSE_HOST=typesense`, `TYPESENSE_HOST_PORT=8108`, `TYPESENSE_PROTOCOL=http` (upstream defaults to `https`, so this one must be set), `TYPESENSE_API_KEY=<same as .env>`. Optional upstream knobs left at their defaults: `TYPESENSE_CONNECTION_TIMEOUT_SECONDS` (5), `TYPESENSE_CONVERSATION_INDEX_WRITES` (on whenever `TYPESENSE_HOST` + key are set), `MEMORY_TYPESENSE_COLLECTION` (`canonical_memory_atoms`). The harness's `MEMORY_MODE=read` and `MEMORY_CANONICAL_CONSOLIDATION_ENABLED=true` also touch the memory index; they belong to the backend's env.

### Upstream Omi (`upstream/omi/`, shallow submodule)
`git submodule update --init` to fetch it. Pinned at `9f2ddcc` (2026-11-06). Findings from mapping `backend/` (re-check them when bumping the submodule):
- **Upstream ships a local emulator harness** (`make dev-up`, `scripts/dev-harness/dev_harness/`, runbook `backend/docs/runbooks/local-emulator-manual-qa.md`). It runs the Firebase emulators, `redis-server`, Typesense, `llm_gateway`, `backend` and `desktop-backend` as **host processes** and injects the env in `config.py` `_harness_service_extra()` / `child_env_for()`. This is the reference for our env: we reproduce that env in Docker containers instead of running the harness. Its default ports (backend **8000** = vLLM's port, Firestore 8085, Auth 9099, Redis 6380, Typesense 8108, gateway 9080, desktop 10201) must be remapped.
- **Env stages** (`OMI_ENV_STAGE`, `backend/utils/env_loader.py`): `local` = emulators plus real providers, which is what we want. `offline` = fake LLMs and stub STT (`OMI_STT_STUB=1`), useful as a boot smoke test.
- **Firebase:** with the emulator env vars set, Firestore and Auth need no Google credentials. Composite indexes come from `firestore.indexes.json` (the emulator doesn't enforce them). The harness seeds test users (`alice@local.omi.invalid` etc.). Signing in the real mobile app against the Auth emulator is still an open question: the app has to be built pointing at the emulator (see `app/README.md`, `docs/doc/developer/AppSetup.mdx`).
- **LLM routing** (`backend/utils/llm/model_config.py`): every feature maps to a hardcoded model, `gpt-6-luna` or `gpt-5-nano`, with provider `openai`, and there is no per-feature env override. Calls go either directly through `ChatOpenAI` or through `llm_gateway` (`OMI_LLM_GATEWAY_FEATURE_MODE=gateway`, the harness default). The gateway reads `OPENAI_BASE_URL`, and its lanes only use those two OpenAI models. `web_search` → Perplexity (leave off). **Plan:** set `OPENAI_BASE_URL` to an alias layer that maps `gpt-6-luna` / `gpt-5-nano` → Gemma on vLLM and `text-embedding-3-large` → a local embedding model. Done for chat via the `llm-proxy` container, which leaves the vLLM container unchanged. Gemma has to cope with the structured-output features (`_STRUCTURED_OUTPUT_FEATURES`).
- **STT:** `STT_SERVICE_MODELS=parakeet` + `HOSTED_PARAKEET_API_URL` uses our `stt` service, the same keyless chain the offline harness pins. Deepgram was retired upstream on 2026-08-27.
- **Required secrets:** `ENCRYPTION_SECRET` (≥32 bytes, encrypts conversations, memories and chat) and `ADMIN_KEY` (local auth bypass `ADMIN_KEY<uid>`).
- **Open questions:** `HOSTED_PUSHER_API_URL` / `backend/pusher/` isn't started by the harness, but `.env.template` says conversations stay `in_progress` without it. Check whether we need to run the pusher. `desktop-backend` (Rust/desktop) is probably not needed for the pendant.

## 🚀 Commands
- **Build:** `docker compose build`
- **Run:** `docker compose up -d` (logs: `docker compose logs -f stt llm-proxy embed firebase redis typesense typesense-init`)
- **Test LLM proxy:** `scripts/test_llm_proxy.sh [BASE_URL]`: health, model list, and a plain plus a streamed chat completion for each alias.
- **Test embedding service:** `scripts/test_embed.sh [BASE_URL] [MODEL]`: health, models, and string / list / cl100k token-ID / base64 / `dimensions` requests, plus an EN/DE similarity check and a ~1k-token latency check. Pass `http://127.0.0.1:8101` to test through `llm-proxy`.
- **Test Firebase emulators:** `scripts/test_firebase.sh [--persist] [FIRESTORE_URL] [AUTH_URL]`: health, a Firestore REST write/read/delete, and an Auth sign-up/sign-in/delete. `--persist` also restarts the container and checks that a document survived.
- **Test Redis + Typesense:** `scripts/test_redis_typesense.sh [--persist] [REDIS_HOST:PORT] [TYPESENSE_URL]`: health, a Redis SET/GET/DEL with the backend's `AUTH default ""`, the Typesense collection list (fails if `conversations` is missing), and a document upsert/search/delete in `conversations`. `--persist` also restarts both containers and checks that a key and a document survived.
- **Test STT service:** `scripts/test_stt.sh [AUDIO_FILE] [STREAM_SPEED]` — health, `/v1` + `/v2` batch, and `/v3/stream` replay (via `services/stt/tools/stream_client.py`).
- **Benchmark STT on CPU:** `scripts/bench_stt.sh [AUDIO_FILE]` — runs faster-whisper and Parakeet (onnx-asr) in a Docker container across thread counts; writes `results/bench_stt/<timestamp>/` (JSONL, summary table, transcripts). Env overrides: `THREADS`, `MODELS`, `LANGUAGE`, `MIN_DURATION`, `REFERENCE` (correct transcript → word error rate), `NOISE` (`white`/`pink`/noise recording) and `SNRS` (e.g. `"clean 10 5 0 noise-only"`) for noise robustness. Without an audio file it loops a short English sample with a built-in reference. Models cache in `.cache/models/`.

## 📂 Project Structure
- `/` : Git repository root containing all necessary scripts and configurations.
- `docker-compose.yml`, `.env.example` : Service definitions and documented configuration.
- `services/stt/` : Local STT service (`app/` server code, `tools/` test clients, `Dockerfile`).
- `services/llm-proxy/` : OpenAI-compatible model-alias pass-through to vLLM (embeddings go to `embed`).
- `services/embed/` : OpenAI-compatible CPU embedding server.
- `services/firebase/` : Firebase Emulator Suite image (`firebase.json`, `entrypoint.sh`).
- `services/typesense/` : `init.sh` for the one-shot `typesense-init` collection bootstrap (Redis and Typesense themselves use stock images).
- `scripts/` : Persisted helper scripts (`bench_stt.sh` with its container in `scripts/bench_stt/`, `test_stt.sh`, `test_llm_proxy.sh`, `test_embed.sh`, `test_firebase.sh`, `test_redis_typesense.sh`).
- `.cache/`, `data/`, `results/`, `state/` : Local model cache, sample audio, benchmark output and persistent service data (`state/firebase/`, `state/redis/`, `state/typesense/`) (git-ignored).
- `upstream/omi/` : `BasedHardware/omi` as a shallow git submodule. Never copy or edit upstream code here; configure it from the outside.

## 📌 Status / Decisions
*(Record what currently runs locally vs. in the cloud, and why key choices were made.)*

- **STT requirements:** mostly English, occasionally German; **must tolerate noise** (pendant audio: background noise, silences, clothing rustle).
- **STT candidates (CPU, int8, 4 threads):** Parakeet TDT 0.6B v3 (~45× real-time, ~0.09 cores/stream) vs. faster-whisper large-v3-turbo (~3.6× real-time, ~1.1 cores/stream). Both are affordable.
- **Preliminary noise test** (JFK sample + synthetic pink noise): turbo held 0% WER down to 0 dB SNR, while Parakeet degraded (6% at 10 dB, 13% at 0 dB). Neither invented words from noise alone. *Caveat:* the JFK quote is famous and is probably in Whisper's training data, and pink noise isn't realistic, so confirm with real pendant recordings before choosing.
- **Decision: run Parakeet v3 now, keep Whisper turbo one setting away** (`STT_ENGINE=whisper`). Revisit after testing real noisy pendant audio. Measured streaming delay after a sentence ends, at 4 threads: Parakeet ~1 s, turbo ~4–5 s.
- **LLM aliasing via a proxy container, not vLLM `--served-model-name`**: this leaves the shared vLLM container untouched, and the mapping can change with a restart of `llm-proxy` alone. A plain pass-through was chosen over LiteLLM to keep vLLM-specific features (reasoning parser, tool calls, structured output) working without translation.
- **Embeddings: BGE-M3 on CPU in a small custom server** (`services/embed/`), chosen because it is multilingual, has an 8k context and needs no query/passage prefix. Measured at 4 threads: ~65 ms for a short sentence, ~2.3 s for a ~1.3k-token input, which is fine for one pendant (embeddings run once per finished conversation and once per search). EN/DE pairs score 0.92 vs 0.32 for an unrelated sentence. Checked with the real `langchain-openai==1.1.14` client through `llm-proxy`.
- **Firebase emulators in a container, not the upstream harness:** this keeps everything in Docker and keeps the harness's host processes and port defaults out of the way (port 4000, the UI default, is already taken on the host). We copy the harness's flags and env, not its code.
- **Redis + Typesense as stock containers with an init one-shot, not the harness's host processes:** same versions and env as the harness, but Redis gets AOF persistence (the harness runs it without) so locks and caches survive restarts. The `conversations` collection is created from the submodule's schema file by `typesense-init`, because upstream assumes it already exists.
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
