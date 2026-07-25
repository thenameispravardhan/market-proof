"""Generate the frontend's cap-tier lookup from the AMFI market-cap list.

AMFI publishes, twice a year, the average market capitalisation of every
listed company plus its SEBI categorisation (Large / Mid / Small). The
file is an .xlsx with ~5,400 rows:

    https://www.amfiindia.com/research-information/other-data/categorization-of-stocks

Only the Large (rank 1-100) and Mid (101-250) names need to be shipped
to the browser: SEBI defines rank 251+ as Small, so "not in either set"
reproduces the remaining ~5,200 rows exactly. 250 symbols instead of
5,427 rows, with identical output.

Usage:
    python scripts/gen_market_cap.py                  # regenerate from newest xlsx
    python scripts/gen_market_cap.py --check          # verify, don't write (exit 1 on drift)
    python scripts/gen_market_cap.py path/to/file.xlsx

The source workbook lives in data/amfi/ (gitignored — it is a
half-yearly download, not source code). `--check` re-derives the data
from it and byte-compares against the committed TS, so a stale or
hand-edited lookup fails loudly.
"""
from __future__ import annotations

import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parents[1]
XLSX_DIR = ROOT / "data" / "amfi"
OUT = ROOT / "frontend" / "src" / "lib" / "marketCapData.ts"

# Column indexes in the AMFI sheet (0-based), row 1 is the header.
COL_BSE_SYM, COL_NSE_SYM, COL_CATEGORY = 3, 5, 10


def newest_xlsx() -> Path:
    files = sorted(XLSX_DIR.glob("*.xlsx"), key=lambda p: p.stat().st_mtime)
    if not files:
        sys.exit(f"No .xlsx found in {XLSX_DIR}. Download the AMFI "
                 f"categorisation file there first.")
    return files[-1]


def read_tiers(xlsx: Path) -> tuple[list[str], list[str], dict[str, int]]:
    """Return (large_symbols, mid_symbols, counts). Both the BSE and NSE
    ticker are kept — the bot's announcements arrive with either."""
    ws = openpyxl.load_workbook(xlsx, read_only=True).worksheets[0]
    by_tier: dict[str, set[str]] = {"Large Cap": set(), "Mid Cap": set(), "Small Cap": set()}
    counts = {k: 0 for k in by_tier}
    for row in ws.iter_rows(min_row=2, values_only=True):
        cat = str(row[COL_CATEGORY] or "").strip()
        if cat not in counts:
            continue
        counts[cat] += 1
        for col in (COL_BSE_SYM, COL_NSE_SYM):
            sym = str(row[col] or "").strip().upper()
            if sym and sym != "-":
                by_tier[cat].add(sym)
    if counts["Large Cap"] != 100 or counts["Mid Cap"] != 150:
        sys.exit(f"Unexpected SEBI split in {xlsx.name}: {counts}. "
                 f"Expected 100 large / 150 mid — check the file layout.")
    # The shipped lookup treats "absent from both sets" as Small. That is
    # lossless only while no small-cap ticker collides with a top-250 one,
    # so check it against all ~5,400 rows rather than assuming.
    top250 = by_tier["Large Cap"] | by_tier["Mid Cap"]
    collisions = sorted(by_tier["Small Cap"] & top250)
    if collisions:
        sys.exit(f"Ticker collision between small caps and the top 250: "
                 f"{collisions}. The absent-means-Small shortcut would "
                 f"misclassify them — ship the small set too.")
    return sorted(by_tier["Large Cap"]), sorted(by_tier["Mid Cap"]), counts


def render(xlsx: Path, large: list[str], mid: list[str], counts: dict[str, int]) -> str:
    def block(names: list[str]) -> str:
        # Wrap at ~72 chars so the diff stays readable.
        lines, cur = [], "  "
        for n in names:
            item = f'"{n}", '
            if len(cur) + len(item) > 74:
                lines.append(cur.rstrip())
                cur = "  "
            cur += item
        if cur.strip():
            lines.append(cur.rstrip())
        return "\n".join(lines)

    return f"""// GENERATED — do not edit by hand.
// Source: {xlsx.name} ({counts['Large Cap']} large, {counts['Mid Cap']} mid,
// {counts['Small Cap']} small companies, SEBI categorisation).
// Regenerate:  python scripts/gen_market_cap.py
// Verify:      python scripts/gen_market_cap.py --check
//
// Only ranks 1-250 are listed: SEBI defines 251+ as Small, so every
// symbol absent from both sets is Small by definition.

export const SOURCE = "{xlsx.stem}";

export const LARGE: ReadonlySet<string> = new Set([
{block(large)}
]);

export const MID: ReadonlySet<string> = new Set([
{block(mid)}
]);
"""


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--check"]
    check = "--check" in sys.argv[1:]
    xlsx = Path(args[0]) if args else newest_xlsx()
    text = render(xlsx, *read_tiers(xlsx))
    if check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            sys.exit(f"DRIFT: {OUT.relative_to(ROOT)} does not match {xlsx.name}. "
                     f"Run: python scripts/gen_market_cap.py")
        print(f"OK: {OUT.relative_to(ROOT)} matches {xlsx.name} "
              f"(all rows checked, no small-cap ticker collides with the top 250)")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)} from {xlsx.name}")


if __name__ == "__main__":
    main()
