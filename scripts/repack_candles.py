"""Rewrite every candle file with candle_sync.PARQUET_OPTS (smaller, faster to read).

Same rows, columns and types — only the encoding changes. Each file is written
to a temp file, checked (row count + a checksum over every column), and only
then swapped in atomically, so an interrupted run leaves every file whole.
Already-repacked files are rewritten too (idempotent, just slower).

    python scripts/repack_candles.py            # whole store: base exports + _inc
    python scripts/repack_candles.py --dry      # count files, change nothing

Run it outside market hours on the server: candle_sync may be appending.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.candle_sync import CANDLES, PARQUET_OPTS  # noqa: E402

# Exact and order-independent: a float sum could differ by summation order alone.
CHECK = ("SELECT count(*), sum(hash(ts, datetime, open, high, low, close, volume)::HUGEINT) "
         "FROM read_parquet('{}')")


def repack(con: duckdb.DuckDBPyConnection, f: Path) -> int:
    tmp = f.with_suffix(".parquet.repack")
    src = f.as_posix()
    con.execute(f"COPY (SELECT * FROM read_parquet('{src}') ORDER BY ts) TO '{tmp.as_posix()}' ({PARQUET_OPTS})")
    before = con.execute(CHECK.format(src)).fetchone()
    after = con.execute(CHECK.format(tmp.as_posix())).fetchone()
    if before != after:
        tmp.unlink()
        raise RuntimeError(f"{f.name}: rows differ after repack {before} != {after}")
    saved = f.stat().st_size - tmp.stat().st_size
    tmp.replace(f)
    return saved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--store", type=Path, default=CANDLES, help="candle folder (default: the app's)")
    a = ap.parse_args()
    files = sorted(a.store.glob("*.parquet")) + sorted(a.store.glob("_inc/*/*.parquet"))
    print(f"{len(files):,} files under {a.store}")
    if a.dry:
        return 0
    con = duckdb.connect(config={"memory_limit": "512MB"})
    saved = failed = 0
    t0 = time.time()
    for i, f in enumerate(files, 1):
        try:
            saved += repack(con, f)
        except Exception as e:  # noqa: BLE001 — a bad file is reported, the rest still repack
            failed += 1
            print("FAIL", f, str(e)[:160], flush=True)
        if i % 500 == 0:
            print(f"  {i:,}/{len(files):,}  saved {saved / 2**30:.2f} GB  {time.time() - t0:.0f}s", flush=True)
    print(f"done: {len(files) - failed:,} repacked, {failed} failed, saved {saved / 2**30:.2f} GB")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
