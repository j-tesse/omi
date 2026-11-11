"""Replay upstream Omi's LLM call shapes against llm-proxy and report per feature.

Usage (normally via scripts/test_llm_features.sh):
  run.py [--only name,feature,...] [--skip-heavy] [--repeat N] [--out DIR]

Status per case:
  PASS  HTTP ok, parsed into upstream's schema, content sensible, within upstream's timeout budget
  SLOW  same, but slower than the budget (gateway->provider 30 s / backend->gateway 15 s,
        60 s for foreground features): upstream would time out with its default timeouts
  FAIL  HTTP error, parse/validation error, or nonsensical content
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import traceback
from pathlib import Path

import client
import features

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "openai", "langchain"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


def run_case(c: features.Case) -> dict:
    s = client.Session()
    t0 = time.monotonic()
    try:
        ok, note = c.fn(s)
        error = ""
    except Exception as exc:  # noqa: BLE001
        ok, note, error = False, "", f"{type(exc).__name__}: {exc}"[:600]
        traceback.print_exc(file=sys.stderr)
    seconds = time.monotonic() - t0
    xs = s.exchanges
    http_err = next((x.error for x in xs if x.error), "")
    budget = min((r.budget_s for r in s.routes), default=0)
    # Streamed exchanges are consumed by the SDK; fall back to the case wall time per call.
    slowest = max((x.seconds or seconds / max(len(xs), 1) for x in xs), default=0)
    status = "FAIL" if (not ok or http_err) else ("SLOW" if budget and slowest > budget else "PASS")
    req0 = xs[0].request if xs else {}
    rf = req0.get("response_format") or {}
    return {
        "case": c.name,
        "feature": c.feature,
        "shape": c.shape,
        "status": status,
        "model": req0.get("model"),
        "effort": req0.get("reasoning_effort"),
        "response_format": rf.get("type"),
        "strict": (rf.get("json_schema") or {}).get("strict"),
        "calls": len(xs),
        "seconds": round(seconds, 2),
        "slowest_call_s": round(slowest, 2),
        "ttfb_s": [round(x.ttfb, 2) for x in xs],
        "budget_s": budget,
        "prompt_tokens": sum((x.usage or {}).get("prompt_tokens", 0) for x in xs),
        "completion_tokens": sum((x.usage or {}).get("completion_tokens", 0) for x in xs),
        "reasoning_chars": sum(x.reasoning_chars for x in xs),
        "finish_reasons": [x.finish_reason for x in xs],
        "note": note,
        "error": error or http_err,
        "exchanges": [
            {"status": x.status, "seconds": round(x.seconds, 2), "finish_reason": x.finish_reason,
             "usage": x.usage, "error": x.error, "content_head": x.content[:400],
             "request_keys": sorted(x.request), "request": x.request}
            for x in xs
        ],
    }


def table(rows: list[dict]) -> str:
    head = f"{'status':6} {'feature':22} {'case':20} {'model':11} {'effort':7} {'calls':>5} {'sec':>7} {'budget':>6} {'out_tok':>7}  note"
    lines = [head, "-" * len(head)]
    for r in rows:
        note = r["error"] or r["note"]
        lines.append(
            f"{r['status']:6} {r['feature']:22} {r['case']:20} {str(r['model']):11} {str(r['effort']):7} "
            f"{r['calls']:>5} {r['slowest_call_s']:>7.1f} {r['budget_s']:>6.0f} {r['completion_tokens']:>7}  {note[:110]}"
        )
    counts = {k: sum(r["status"] == k for r in rows) for k in ("PASS", "SLOW", "FAIL")}
    lines.append(f"\n{counts['PASS']} pass, {counts['SLOW']} slow, {counts['FAIL']} fail  (sec = slowest single call)")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated case or feature names")
    ap.add_argument("--skip-heavy", action="store_true", help="skip the ~30 min transcript case")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    cases = [c for c in features.CASES if (not only or c.name in only or c.feature in only)
             and not (args.skip_heavy and c.heavy)]
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in cases:
        for i in range(args.repeat):
            r = run_case(c)
            r["run"] = i + 1
            rows.append(r)
            print(f"{r['status']:5} {c.feature}/{c.name} {r['slowest_call_s']}s  {(r['error'] or r['note'])[:150]}", flush=True)
            if out:
                with (out / "results.jsonl").open("a") as f:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    summary = table(rows)
    print("\n" + summary)
    if out:
        (out / "summary.txt").write_text(summary + "\n")
    return 1 if any(r["status"] == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
