#!/usr/bin/env bash
# Smoke-test the running STT service (docker compose up -d stt) on one audio file:
# health, batch (/v1, /v2) and live streaming (/v3/stream).
#
# Usage: scripts/test_stt.sh [AUDIO_FILE] [STREAM_SPEED]
#   AUDIO_FILE    default: data/samples/jfk.wav (fetched by scripts/bench_stt.sh)
#   STREAM_SPEED  1 = real time like a pendant (default 4 for a quicker test)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AUDIO="$(realpath "${1:-$ROOT/data/samples/jfk.wav}")"
SPEED="${2:-4}"
PORT="$(grep -E '^STT_PORT=' "$ROOT/.env" 2>/dev/null | cut -d= -f2)"
URL="http://127.0.0.1:${PORT:-8100}"

echo ">> Health"
curl -fsS "$URL/health"; echo
echo ">> /v1/transcribe"
curl -fsS -F "file=@$AUDIO" "$URL/v1/transcribe"; echo
echo ">> /v2/transcribe"
curl -fsS -F "file=@$AUDIO" -F diarize=true "$URL/v2/transcribe"; echo
echo ">> /v3/stream (speed ${SPEED}x)"
cd "$ROOT"
docker compose exec -T stt sh -c 'cat > /tmp/stream_input' <"$AUDIO"
docker compose exec -T stt python -m tools.stream_client ws://localhost:8000 /tmp/stream_input --speed "$SPEED"
