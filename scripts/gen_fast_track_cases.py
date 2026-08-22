"""Record Python's answer for every case pinned in cpp/tests/test_fast_track.cpp.

The C++ test asserts against the PYTHON's behaviour, so the expected values
have to come from running the Python -- not from reading it. This prints the
answers; a mismatch with the C++ test means one of the two is wrong, and the
Python is the reference (§10.1).

    PYTHONPATH=. TESTING=1 python scripts/gen_fast_track_cases.py
"""
from __future__ import annotations

import sys

from app.analyzer.fast_track import (
    evaluate_fast_track,
    evaluate_fast_track_text,
    is_hybrid_order_candidate,
    parse_inr_crore,
    _order_value_near_context,
)

VALUES = [
    "Rs. 1,234.56 crore", "₹450 cr", "INR 89.5 crores", "Rs 20 lakh",
    "Rs 1.2 billion", "Rs 50 million",
    "orders of Rs 120 crore and Rs 330 crore, aggregating to Rs 450 crore",
    "USD 500 million", "Rs 450", "450 crore", "Rs , crore", "Rs 450 crorex",
    "Rs 0.5 crore",
]

HEADLINES = [
    "ACME bags order worth Rs 450 crore from NHAI",
    "ACME wins order worth Rs 600 crore",
    "ACME wins order worth Rs 100 crore",
    "ACME bags order worth Rs 24 crore",
    "ACME order worth Rs 450 crore cancelled",
    "ACME submits bid for order worth Rs 450 crore",
    "ACME order worth Rs 450 crore terminated",
    "ACME wonders about Rs 450 crore",
    "Board to consider buyback of Rs 900 crore",
    "Buyback of Rs 900 crore completed",
    "Resignation of Managing Director",
    "Resignation and appointment of Managing Director",
    "Resignation of Independent Director",
    "Resignation of Company Secretary",
    "",
    "   ",
]

HYBRID_CANDIDATES = [
    "Company informs regarding bagging of order",
    "Company bags order worth Rs 450 crore",
    "Board meeting intimation",
]

HYBRID = [
    ("Company informs the Exchange regarding Bagging of order",
     "The Company has received a Letter of Award for a work order valued at "
     "Rs 750 crore from the client."),
    ("Company informs regarding bagging of order",
     "Order of Rs 750 crore. A prior contract was terminated."),
]

WINDOWS = [
    "Bagged an order. " + "x" * 600 + " total income Rs 5,000 crore",
    "The company bagged a work order valued at Rs 450 crore.",
    "order " + "y" * 240 + " ₹ 300 crore",
]


def main() -> int:
    print("== parse_inr_crore ==")
    for t in VALUES:
        print(f"  {t!r:<72} -> {parse_inr_crore(t)!r}")

    print("\n== evaluate_fast_track ==")
    for h in HEADLINES:
        m = evaluate_fast_track(h)
        if m is None:
            print(f"  {h!r:<52} -> None")
        else:
            r = m.response
            print(f"  {h!r:<52} -> {m.pattern} {r.event_type} {r.recommendation} "
                  f"conf={r.confidence} score={r.sentiment_score} kn={r.key_numbers.model_dump()}")

    print("\n== is_hybrid_order_candidate ==")
    for h in HYBRID_CANDIDATES:
        print(f"  {h!r:<52} -> {is_hybrid_order_candidate(h)}")

    print("\n== evaluate_fast_track_text ==")
    for h, t in HYBRID:
        m = evaluate_fast_track_text(h, t)
        if m is None:
            print(f"  {h!r:<52} -> None")
        else:
            r = m.response
            print(f"  {h!r:<52} -> {m.pattern} conf={r.confidence} "
                  f"kn={r.key_numbers.model_dump()}")

    print("\n== _order_value_near_context ==")
    for t in WINDOWS:
        print(f"  len={len(t):<5} {t[:40]!r:<45} -> {_order_value_near_context(t)!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
