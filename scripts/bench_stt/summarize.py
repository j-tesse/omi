"""Print a results.jsonl from bench_stt.sh as an aligned table."""

import json
import sys

COLS = [
    ("model", "model"),
    ("threads", "thr"),
    ("snr", "snr"),
    ("wer", "wer"),
    ("audio_s", "audio_s"),
    ("load_s", "load_s"),
    ("wall_s", "wall_s"),
    ("x_realtime", "x_rt"),
    ("avg_cores_used", "cores_used"),
    ("cores_per_live_stream", "cores/stream"),
    ("peak_rss_mb", "rss_mb"),
    ("words", "words"),
]

rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip().startswith("{")]
table = [[h for _, h in COLS]] + [[str(r.get(k, "")) for k, _ in COLS] for r in rows]
widths = [max(len(row[i]) for row in table) for i in range(len(COLS))]
for i, row in enumerate(table):
    print("  ".join(c.ljust(w) for c, w in zip(row, widths)))
    if i == 0:
        print("  ".join("-" * w for w in widths))
