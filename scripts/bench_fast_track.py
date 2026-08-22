"""Benchmark the fast track: Python vs C++, on the SAME text, on the target box.

c++.text §9 PHASE 2 exists so that every performance claim in this migration is
measured rather than asserted, and §12 says anything that does not measurably
help gets reverted. This is the measurement for Phase 5.

    # on the server
    PYTHONPATH=. TESTING=1 .venv/bin/python scripts/bench_fast_track.py \
        --replay cpp/build/replay_cpp

Generates a deterministic ~11.6k-char synthetic filing (the size §1.2 measured
the 987 us on), times the Python, then hands the identical text to the C++ via
TB_BENCH and prints both distributions.

Synthetic because `announcements.body` is NULL for every row in the live DB --
there is no real extracted text to benchmark against yet. That gap is Phase 0's
outstanding debt; see cpp/MIGRATION.md.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

from app.analyzer.fast_track import _order_value_near_context, evaluate_fast_track_text

FILLER = "the company hereby informs the exchange pursuant to regulation 30 "
ORDER = ["order", "orders", "contract", "bagged", "secured", "won", "wins",
         "awarded", "letter of award", "purchase order"]
CCY = ["Rs", "Rs.", "INR", "₹"]
UNITS = ["crore", "crores", "cr", "lakh", "million", "billion"]
AMOUNTS = ["450", "1,234.56", "25", "5,000", "99", "750"]

HEADLINE = "Company informs the Exchange regarding Bagging of order"


def make_doc(target: int, seed: int) -> str:
    rnd = random.Random(seed)
    parts: list[str] = []
    n = 0
    while n < target:
        r = rnd.random()
        if r < 0.06:
            p = rnd.choice(ORDER)
        elif r < 0.12:
            p = f"{rnd.choice(CCY)} {rnd.choice(AMOUNTS)} {rnd.choice(UNITS)}"
        else:
            p = FILLER
        parts.append(p)
        n += len(p) + 1
    return " ".join(parts)


def pct(xs: list[float], p: float) -> float:
    return sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", default="cpp/build/replay_cpp")
    ap.add_argument("--chars", type=int, default=11600)
    ap.add_argument("--iters", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    doc = make_doc(args.chars, args.seed)
    print(f"document      {len(doc)} chars, seed {args.seed}")
    print(f"python value  {_order_value_near_context(doc)!r}")

    # -- Python: the whole hybrid decision, same entry point the C++ runs -----
    samples: list[float] = []
    for _ in range(max(50, args.iters // 20)):
        t0 = time.perf_counter_ns()
        evaluate_fast_track_text(HEADLINE, doc)
        samples.append(time.perf_counter_ns() - t0)
    print(f"\npython  n={len(samples)}  "
          f"p50={pct(samples, .5)/1000:9.1f} us  "
          f"p99={pct(samples, .99)/1000:9.1f} us  "
          f"mean={sum(samples)/len(samples)/1000:9.1f} us")

    # -- C++: identical text, identical entry point ---------------------------
    case = {"headline": HEADLINE, "extracted_text": doc}
    env = dict(os.environ, TB_BENCH=str(args.iters))
    proc = subprocess.run([args.replay], input=json.dumps(case), capture_output=True,
                          text=True, env=env)
    if proc.returncode != 0:
        print(f"replay_cpp failed: {proc.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    c = json.loads(proc.stdout)
    print(f"cpp     n={c['iters']}  "
          f"p50={c['ns_p50']/1000:9.1f} us  "
          f"p99={c['ns_p99']/1000:9.1f} us  "
          f"mean={c['ns_mean']/1000:9.1f} us")

    speedup = (sum(samples) / len(samples)) / c["ns_mean"]
    print(f"\nspeedup (mean)  {speedup:.1f}x")
    print("\nContext: §1.6 -- the end-to-end signal path is 93% exchange lag and 6%")
    print("DeepSeek, so this number does NOT make signals faster. It is the CPU")
    print("headroom argument, not the latency argument.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
