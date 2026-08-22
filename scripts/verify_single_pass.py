"""Prove the C++ rewrite of `_order_value_near_context` is EXACT.

cpp/src/fast_track.cpp replaces the Python's per-window rescan with two
linear passes plus a positional join. That is the one algorithmic change in
the Phase 5 port, it sits on the money path, and the migration plan's own
Phase 5 exit criterion is "zero diffs" -- so it gets proven against the real
corpus rather than argued.

This runs BOTH algorithms in Python (so the comparison isolates the algorithm,
not the language) over every announcement body and headline in the live DB,
and reports any disagreement.

    python scripts/verify_single_pass.py [--db data/trading.db]

Exit code 0 = zero diffs. Non-zero = the rewrite is not equivalent; fix the
C++ before trusting any Phase 5 parity run.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
import time

from app.analyzer.fast_track import (
    _INR_VALUE_RE,
    _ORDER_CONTEXT_RE,
    _UNIT_TO_CRORE,
    _order_value_near_context,
)


def _value_hits(text: str) -> list[tuple[int, int, float]]:
    """Every INR value in `text`: (start, end, crore). One pass.

    Mirrors scan_inr_values() in cpp/src/fast_track.cpp.
    """
    out: list[tuple[int, int, float]] = []
    for m in _INR_VALUE_RE.finditer(text):
        try:
            amount = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        value = amount * _UNIT_TO_CRORE.get(m.group(2).lower().rstrip("."), 0.0)
        if value <= 0:
            continue
        out.append((m.start(), m.end(), value))
    return out


def single_pass(text: str) -> float | None:
    """The algorithm cpp/src/fast_track.cpp implements."""
    normalized = " ".join(text.split())
    if not normalized:
        return None
    values = _value_hits(normalized)
    if not values:
        return None
    best: float | None = None
    for m in _ORDER_CONTEXT_RE.finditer(normalized.lower()):
        lo, hi = max(0, m.start() - 150), m.end() + 250
        for vs, ve, crore in values:
            if vs >= lo and ve <= hi and (best is None or crore > best):
                best = crore
    return best



# -- fuzz ---------------------------------------------------------------------
# The live corpus stores no extracted PDF text (announcements.body is NULL for
# all 28,381 rows -- that is the Phase 0 gap this migration has to close), so
# the real data only ever exercises short headlines with one order mention.
# The window arithmetic is where an inexact rewrite would actually break, so it
# gets hammered directly: values placed at every offset around the -150/+250
# boundary, multi-byte characters shifting byte offsets away from code-point
# offsets, and multiple overlapping order mentions.

_FILLER = "the company hereby informs the exchange pursuant to regulation 30 "
_UNITS = ["crore", "crores", "cr", "lakh", "lakhs", "lac", "million", "mn",
          "billion", "bn", "cro", "crorex"]
_CCY = ["Rs", "Rs.", "INR", "₹", "rs", "inr"]
_ORDER = ["order", "orders", "contract", "bagged", "secured", "won", "wins",
          "awarded", "letter of award", "loa", "purchase order"]
_NOISE = [chr(0x20B9), chr(0x928) + chr(0x92E), chr(0xA0), "  ",
          chr(10), chr(9), "total income", "revenue", "5,000", "1,23,456.78"]


def _fuzz_text(rnd) -> str:
    parts = []
    for _ in range(rnd.randint(1, 14)):
        r = rnd.random()
        if r < 0.30:
            parts.append(rnd.choice(_ORDER))
        elif r < 0.55:
            amt = rnd.choice(["450", "1,234.56", "25", "24.99", "0", "99",
                              "1,00,000", ",", "12,", "007"])
            parts.append(f"{rnd.choice(_CCY)} {amt} {rnd.choice(_UNITS)}")
        elif r < 0.72:
            parts.append(rnd.choice(_NOISE))
        else:
            parts.append(_FILLER[: rnd.randint(1, len(_FILLER))])
    return " ".join(parts)


def fuzz(rounds: int, seed: int) -> int:
    import random

    rnd = random.Random(seed)
    diffs = 0
    for i in range(rounds):
        text = _fuzz_text(rnd)
        want = _order_value_near_context(text)
        got = single_pass(text)
        if want != got:
            diffs += 1
            if diffs <= 5:
                print(f"FUZZ DIFF seed={seed} i={i}: python={want!r} single={got!r}")
                print(f"  text={text!r}")
    print(f"fuzz         {rounds} texts, {diffs} diffs")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/trading.db")
    ap.add_argument("--limit", type=int, default=0, help="0 = every row")
    ap.add_argument("--fuzz", type=int, default=20000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    diffs = fuzz(args.fuzz, args.seed)

    if not Path(args.db).exists():
        # CI has no live DB; the fuzz half is the part that proves the
        # algorithm, and it just ran.
        print(f"(skipping corpus sweep: {args.db} not present)")
        return 1 if diffs else 0

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    sql = "SELECT id, headline, body FROM announcements"
    if args.limit:
        sql += f" LIMIT {args.limit}"

    checked = nonempty = 0
    t_old = t_new = 0.0
    for ann_id, headline, body in con.execute(sql):
        for label, text in (("headline", headline), ("body", body)):
            if not text:
                continue
            checked += 1
            t0 = time.perf_counter()
            want = _order_value_near_context(text)
            t1 = time.perf_counter()
            got = single_pass(text)
            t2 = time.perf_counter()
            t_old += t1 - t0
            t_new += t2 - t1
            if want is not None:
                nonempty += 1
            if want != got:
                diffs += 1
                if diffs <= 10:
                    print(f"DIFF id={ann_id} {label}: python={want!r} single_pass={got!r}")

    print(f"\nchecked      {checked} texts ({nonempty} with a value)")
    print(f"diffs        {diffs}")
    if checked:
        print(f"per-call     python {t_old / checked * 1e6:.1f} us  "
              f"single-pass {t_new / checked * 1e6:.1f} us  "
              f"({t_old / t_new:.1f}x)" if t_new else "")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
