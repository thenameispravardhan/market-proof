#!/usr/bin/env python
"""AMFI market-cap workbook -> data/mcap.csv, the file the bot reads.

Run this offline (it needs pandas + openpyxl, which the bot's own venv
deliberately does not carry) whenever AMFI publishes a new half-yearly
sheet — end of June and end of December.

    py -3.14 scripts/build_mcap.py
    py -3.14 scripts/build_mcap.py --sheet data/amfi/Average...30Jun2026.xlsx

Output is two columns, `symbol,mcap_cr`, ~5,400 rows / ~110 KB. Only the
rupee figure is stored: the Large/Mid/Small boundary is an operator
setting (CAP_LARGE_MIN_CR / CAP_MID_MIN_CR), not AMFI's own column, so
the tiers can be retuned in the UI without refetching anything.

Both exchange symbol columns are emitted — a name trades under the same
ticker on NSE and BSE far more often than not, but not always, and the
bot only ever looks up the symbol the filing arrived with.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHEET = ROOT / "data" / "amfi" / "AverageMarketCapitalization30Jun2026.xlsx"
OUT = ROOT / "data" / "mcap.csv"

# Column positions in AMFI's "FINAL" sheet (header on the second row).
# Named columns would be nicer, but AMFI re-words the headers between
# editions while the layout stays put.
COL_BSE_SYM, COL_NSE_SYM, COL_AVG_MCAP = 3, 5, 9


def build(sheet: Path, out: Path) -> int:
    import pandas as pd  # offline-only dependency

    df = pd.read_excel(sheet, sheet_name="FINAL", header=1)
    sym_bse, sym_nse, mcap = (df.columns[i] for i in
                              (COL_BSE_SYM, COL_NSE_SYM, COL_AVG_MCAP))

    rows: dict[str, float] = {}
    for _, r in df.iterrows():
        try:
            cap = round(float(r[mcap]), 2)
        except (TypeError, ValueError):
            continue
        if cap <= 0:
            continue
        for col in (sym_nse, sym_bse):
            s = str(r[col]).strip().upper()
            # AMFI writes "-" where a company is not listed on that exchange.
            if s and s not in {"-", "NAN", "NONE"}:
                rows.setdefault(s, cap)

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "mcap_cr"])
        for sym in sorted(rows):
            w.writerow([sym, rows[sym]])
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sheet", type=Path, default=DEFAULT_SHEET)
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    if not a.sheet.exists():
        print(f"no such sheet: {a.sheet}", file=sys.stderr)
        return 1
    n = build(a.sheet, a.out)
    print(f"{n:,} symbols -> {a.out} ({a.out.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
