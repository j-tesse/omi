#!/usr/bin/env bash
# Generate a ~1 min synthetic English planning conversation (two espeak-ng voices,
# 16 kHz mono PCM16 WAV) with concrete tasks and dates. The repeated JFK quote is
# too thin: the finalizer's LLM discard check rightly drops it, so it never gets
# a summary. Runs espeak-ng + sox in a throwaway Debian container.
# Usage: scripts/test_pusher/make_sample.sh [OUT_WAV]   (default data/samples/meeting_en.wav)
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT="${1:-data/samples/meeting_en.wav}"
mkdir -p "$(dirname "$OUT")"
OUT_DIR="$(realpath "$(dirname "$OUT")")"
docker run --rm -i -u 0 -v "$OUT_DIR:/out" -e OUT_NAME="$(basename "$OUT")" -e HOST_UID="$(id -u):$(id -g)" \
  debian:trixie-slim bash -s <<'EOF'
set -euo pipefail
apt-get update -qq && apt-get install -y -qq --no-install-recommends espeak-ng sox >/dev/null
lines=(
  "en-us+m3|Hi Sarah, thanks for joining. Let's plan the move of the office to the new building on Main Street."
  "en-us+f3|Sure Tom. The movers are booked for Friday the twentieth. I still need the final floor plan from you."
  "en-us+m3|I will send you the floor plan by Wednesday. Can you also call the internet provider to set up the fiber line before Friday?"
  "en-us+f3|Yes, I will call them tomorrow morning. We also need to order twelve new desks and chairs. The budget is about eight thousand dollars."
  "en-us+m3|That works. Please get two quotes for the furniture and email them to me by Thursday."
  "en-us+f3|Okay. And who is telling the team? Everyone should pack their desks on Thursday afternoon."
  "en-us+m3|I will write an announcement to the whole team today. Let's meet again on Monday to check progress."
)
i=0
for entry in "${lines[@]}"; do
  voice="${entry%%|*}"; text="${entry#*|}"
  espeak-ng -v "$voice" -s 150 -w "/tmp/l$i.wav" "$text"
  sox "/tmp/l$i.wav" -r 16000 -c 1 -b 16 "/tmp/r$i.wav" pad 0 0.8
  i=$((i + 1))
done
sox /tmp/r*.wav -r 16000 -c 1 -b 16 "/out/$OUT_NAME"
chown "$HOST_UID" "/out/$OUT_NAME"
EOF
echo "wrote $OUT ($(python3 -c "import wave,sys; w=wave.open(sys.argv[1]); print(round(w.getnframes()/w.getframerate(),1))" "$OUT") s)"
