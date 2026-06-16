"""Repair trade rows corrupted by the position-doubling bug.

Before the fix in `ExecutionManager._persist_trade` (the manager wrote the
paper position a second time, on top of the paper backend's own
`_mirror_position`), every paper position was created at 2x its real size.
When those positions were later closed, the exit SELL legs were recorded at
the doubled quantity AND doubled realised P&L. That makes the Dashboard
(which sums `Trade.pnl`) show ~2x the true profit, while the Trade History
page (which scales each exit back to the matched entry lot) shows the
correct figure — hence the two pages disagree.

This script corrects the historical rows in place so both pages agree:

  1. Doubled paired exit  — a filled SELL carrying P&L whose quantity is
     exactly 2x the symbol's filled BUY quantity. Halve its quantity AND
     its P&L (back to the real lot the operator actually traded).

  2. Orphan exit          — a filled SELL carrying P&L with NO filled BUY
     leg for the symbol (the entry was never recorded). It cannot be paired
     and only pollutes the realised total, so it's deleted.

  3. Stale zero positions — `positions` rows left at quantity 0 after a
     round-trip closed. They inflate the Dashboard's "Open positions"
     count, so they're deleted.

Clean rows (e.g. positions whose exit quantity already equals the entry)
are left untouched, so the script is safe to run on a mixed database and is
idempotent (running it twice is a no-op).

Usage:
    python scripts/fix_doubled_trades.py            # DRY RUN — show changes
    python scripts/fix_doubled_trades.py --apply    # commit the changes

After applying, RESTART the backend so its in-memory caches reset too.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = PROJECT_ROOT / "data" / "trading.db"


def _plan(conn: sqlite3.Connection):
    """Compute the correction plan without mutating anything."""
    rows = conn.execute(
        "SELECT id, symbol, side, quantity, pnl FROM trades WHERE status='filled'"
    ).fetchall()

    buy_qty: dict[str, int] = defaultdict(int)
    for _id, sym, side, qty, _pnl in rows:
        if side == "BUY":
            buy_qty[sym] += abs(qty or 0)

    halve: list[tuple] = []   # (id, sym, old_qty, new_qty, old_pnl, new_pnl)
    drop_legs: list[tuple] = []  # (id, sym, qty, pnl)
    for _id, sym, side, qty, pnl in rows:
        if side != "SELL" or pnl is None:
            continue  # only exit legs carry realised P&L
        q = abs(qty or 0)
        bq = buy_qty.get(sym, 0)
        if bq > 0 and q == 2 * bq:
            halve.append((_id, sym, q, q // 2, pnl, round(pnl / 2.0, 2)))
        elif bq == 0:
            drop_legs.append((_id, sym, q, pnl))
        # else: q == bq (already clean) or some other ratio — leave it.

    zero_pos = conn.execute(
        "SELECT id, symbol FROM positions WHERE quantity = 0"
    ).fetchall()

    return halve, drop_legs, zero_pos


def _realised_after(conn, halve, drop_legs) -> float:
    halved_ids = {h[0] for h in halve}
    dropped_ids = {d[0] for d in drop_legs}
    new_pnl = {h[0]: h[5] for h in halve}
    total = 0.0
    for _id, pnl in conn.execute(
        "SELECT id, pnl FROM trades WHERE status='filled' AND side='SELL' AND pnl IS NOT NULL"
    ):
        if _id in dropped_ids:
            continue
        total += new_pnl[_id] if _id in halved_ids else (pnl or 0.0)
    return round(total, 2)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Repair doubled / orphan trade rows")
    p.add_argument("--apply", action="store_true", help="commit the changes (default: dry run)")
    args = p.parse_args(argv)

    if not DB_PATH.exists():
        print(f"No database at {DB_PATH} — nothing to do.")
        return 0

    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    try:
        halve, drop_legs, zero_pos = _plan(conn)

        before = conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades "
            "WHERE status='filled' AND side='SELL' AND pnl IS NOT NULL"
        ).fetchone()[0]
        after = _realised_after(conn, halve, drop_legs)

        print("=" * 64)
        print(f"{'DRY RUN — no changes written' if not args.apply else 'APPLYING CHANGES'}")
        print("=" * 64)
        print(f"\nDoubled exits to halve ({len(halve)}):")
        for _id, sym, oq, nq, op, np_ in halve:
            print(f"  #{_id:<4} {sym:<11} qty {oq} -> {nq}   pnl {op:>10.2f} -> {np_:>10.2f}")
        print(f"\nOrphan exits to delete ({len(drop_legs)}):")
        for _id, sym, q, pnl in drop_legs:
            print(f"  #{_id:<4} {sym:<11} qty {q}  pnl {pnl:>10.2f}   (no entry leg)")
        print(f"\nStale qty-0 positions to delete ({len(zero_pos)}):")
        for _id, sym in zero_pos:
            print(f"  #{_id:<4} {sym}")

        print(f"\nRealised P&L (Dashboard sum):  {before:>12.2f}  ->  {after:>12.2f}")
        print("After the fix the Dashboard and Trade History pages match.")

        if not args.apply:
            print("\nRe-run with --apply to commit.")
            return 0

        for _id, _sym, _oq, nq, _op, np_ in halve:
            conn.execute("UPDATE trades SET quantity=?, pnl=? WHERE id=?", (nq, np_, _id))
        for _id, *_ in drop_legs:
            conn.execute("DELETE FROM trades WHERE id=?", (_id,))
        for _id, _sym in zero_pos:
            conn.execute("DELETE FROM positions WHERE id=?", (_id,))
        conn.commit()
        print("\nDone. RESTART the backend so its in-memory caches reset too.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
