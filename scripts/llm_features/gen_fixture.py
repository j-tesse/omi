"""Generate the ~30 minute meeting transcript fixture with the local model (run once).

The output, fixtures/meeting_30min.txt, is committed so test runs are
reproducible; rerun only to replace it:

  docker run --rm --network host -v $PWD/scripts/llm_features:/app \
    --entrypoint python omi-test-llm-features:latest /app/gen_fixture.py http://127.0.0.1:8101/v1

Format matches upstream TranscriptSegment.segments_as_string(include_timestamps=True):
"[mm:ss - mm:ss] Speaker: text" blocks separated by blank lines. ~150 spoken
words per minute -> ~4500 words for 30 minutes.
"""

import re
import sys
from pathlib import Path

from openai import OpenAI

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8101/v1"
OUT = Path(__file__).parent / "fixtures" / "meeting_30min.txt"
PARTS = [
    "small talk while people join (weekend, a sick kid), then the agenda: launch of the 'Harbor' mobile app v2.3",
    "QA status: crash on Android 14 when Bluetooth reconnects, who owns the fix, a deadline next Thursday",
    "marketing plan: beta newsletter, a landing page, budget of 4,000 euros, Priya to draft copy by Monday",
    "a tangent about Tom's trip to Lisbon and a restaurant recommendation, then back to pricing tiers",
    "customer feedback from the support inbox, two enterprise leads, a demo call booked for the 14th at 3pm",
    "wrap-up: decisions recap is skipped, people list their own next steps, one unresolved question about GDPR",
]
SPEAKERS = "You (the account owner, product lead), Priya (marketing), Tom (Android engineer), Lena (QA, sometimes uses German words)"

client = OpenAI(base_url=BASE, api_key="dummy")
text, minute = "", 0
for i, topic in enumerate(PARTS):
    prompt = (
        f"Write minutes {minute}-{minute + 5} of a realistic, messy, spoken team meeting transcript "
        f"(speech-to-text output: filler words, interruptions, short turns, occasional mis-hearings). "
        f"Speakers: {SPEAKERS}. This part covers: {topic}. About 750 words. "
        f"Format every turn exactly as 'Name: text' on its own line, nothing else."
        + (f"\n\nThe previous part ended with:\n{text[-1500:]}" if text else "")
    )
    r = client.chat.completions.create(
        model="gpt-6-luna",
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="none",
        max_completion_tokens=2500,
    )
    # The model copies the timestamps of the previous part; drop them, they are recomputed.
    lines = [re.sub(r"^\[[^\]]*\]\s*", "", ln.strip()) for ln in (r.choices[0].message.content or "").splitlines()]
    turns = [ln for ln in lines if re.match(r"^\w+:", ln)]
    # Spread timestamps evenly over the 5 minutes of this part.
    step = 300 / max(len(turns), 1)
    for j, turn in enumerate(turns):
        s = minute * 60 + j * step
        e = s + step
        text += f"[{int(s) // 60:02d}:{int(s) % 60:02d} - {int(e) // 60:02d}:{int(e) % 60:02d}] {turn}\n\n"
    minute += 5
    print(f"part {i + 1}: {len(turns)} turns", file=sys.stderr)

OUT.write_text(text.strip() + "\n")
print(f"wrote {OUT} ({len(text.split())} words)", file=sys.stderr)
