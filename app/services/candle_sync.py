"""Keep the candle store current so the dataset can price new announcements.

WHY THIS EXISTS
---------------
`warehouse_prices` fills px_*/vol_*/ret_/adj_/usable/mover_ from one place:
`AIdataset/stockdata/<SYMBOL>.parquet`. Those files were a one-time export, so
every announcement after the export has no candles to price against — measured
2026-07-31: 7,020 of 12,936 live rows sat at `price_status='no_candles'`, and
that fraction only grows. Filling the columns was never the missing piece; the
candles were.

So this fetches the missing days from Fyers (the same `/data/history` the rest
of the app uses — Fyers-only pricing, no public feed) and appends them to the
exact parquet files the price fill already reads. Nothing downstream changes.

Append, never rewrite: a symbol's file is read, unioned with the new rows,
deduped on `ts`, and written back. On a 2 GB box that is one symbol's history
in memory at a time, not the whole store.

    from app.services.candle_sync import run_eod
    await run_eod()                                # after the close
    python -m app.services.candle_sync --days 5    # or by hand
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from app.logging_config import get_logger
from app.services.warehouse_store import TABLE, connect

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CANDLES = ROOT / "AIdataset" / "stockdata"
IST = timezone(timedelta(hours=5, minutes=30))

# The index every market-adjusted return is measured against. It is a symbol
# like any other here, but it is never skipped — if it goes stale, mkt_*/adj_*
# stop filling for EVERY symbol, not just this one.
NIFTY_SYMBOL = "NIFTY50-INDEX"
NIFTY_FYERS = "NSE:NIFTY50-INDEX"

# Fyers caps a 1-minute history request at 100 days; stay well inside it.
MAX_DAYS = 90


def _candle_rows(raw: list[Any]) -> list[tuple]:
    """Fyers `[ts, o, h, l, c, v]` -> the parquet's own column order.

    `ts` is epoch SECONDS UTC; the store's `datetime` column is naive IST
    (verified: sessions run 09:15-15:29 in the existing files). Writing UTC
    here would silently shift every price offset by 5h30m — the announcement
    would be priced against the wrong minute rather than fail loudly.
    """
    out = []
    for c in raw or []:
        if not isinstance(c, (list, tuple)) or len(c) < 6:
            continue
        try:
            ts = int(c[0])
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(IST).replace(tzinfo=None)
            out.append((ts, dt, float(c[1]), float(c[2]), float(c[3]),
                        float(c[4]), int(c[5])))
        except (TypeError, ValueError):
            continue
    return out


def _append(symbol: str, rows: list[tuple]) -> int:
    """Merge rows into <SYMBOL>.parquet, deduped on ts. Returns rows added."""
    if not rows:
        return 0
    con = connect()
    f = CANDLES / f"{symbol}.parquet"
    CANDLES.mkdir(parents=True, exist_ok=True)

    con.execute("""CREATE OR REPLACE TEMP TABLE _new
                   (ts BIGINT, datetime TIMESTAMP, open DOUBLE, high DOUBLE,
                    low DOUBLE, close DOUBLE, volume BIGINT)""")
    con.executemany("INSERT INTO _new VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    if f.exists():
        # anti-join on ts so a re-run of the same day is a no-op
        src = (f"SELECT * FROM read_parquet('{f.as_posix()}') "
               f"UNION ALL SELECT * FROM _new WHERE ts NOT IN "
               f"(SELECT ts FROM read_parquet('{f.as_posix()}'))")
        before = con.execute(
            f"SELECT count(*) FROM read_parquet('{f.as_posix()}')").fetchone()[0]
    else:
        src, before = "SELECT * FROM _new", 0

    tmp = f.with_suffix(".parquet.tmp")
    con.execute(f"COPY ({src} ORDER BY ts) TO '{tmp.as_posix()}' (FORMAT parquet)")
    after = con.execute(
        f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
    tmp.replace(f)      # atomic swap — a crash mid-write never truncates the store
    return after - before


async def _fetch(fyers_symbol: str, days: int) -> list[Any]:
    from app.api.market import fetch_history

    return await fetch_history(fyers_symbol, resolution="1", days=days)


async def sync_symbols(symbols: list[str], days: int = 5) -> dict:
    """Pull `days` of 1-minute candles for each symbol and append them."""
    from app.config import get_settings
    from app.execution.symbols import resolve_fyers_symbol

    days = max(1, min(int(days), MAX_DAYS))
    # Fyers rate-limits history. The first run has the whole backlog to clear
    # (~4,100 symbols measured 2026-07-31), so pace it with the same knob the
    # dataset builder already uses rather than inventing a second one.
    delay = float(getattr(get_settings(), "DATASET_FETCH_DELAY_SECONDS", 0.25))
    added = ok = failed = 0
    for i, sym in enumerate(symbols, 1):
        try:
            if i > 1 and delay > 0:
                await asyncio.sleep(delay)
            broker = (NIFTY_FYERS if sym == NIFTY_SYMBOL
                      else (resolve_fyers_symbol(sym) or f"NSE:{sym}-EQ"))
            rows = _candle_rows(await _fetch(broker, days))
            if rows:
                added += _append(sym, rows)
                ok += 1
            else:
                failed += 1
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
            failed += 1
            log.warning("candle_sync.symbol_failed", symbol=sym, error=str(e)[:140])
        if i % 100 == 0:
            log.info("candle_sync.progress", done=i, of=len(symbols), added=added)
    out = {"symbols": len(symbols), "ok": ok, "failed": failed, "candles_added": added}
    log.info("candle_sync.done", **out)
    return out


def pending_symbols(limit: int = 0) -> list[str]:
    """Symbols with a row still waiting on prices, NIFTY always first.

    `no_candles` is included, not just `pending`: those rows were marked when
    the file was missing, and the whole point of this module is that the file
    can now exist. They are reset to `pending` so the price fill reconsiders
    them — otherwise the backlog is permanently frozen.
    """
    con = connect()
    con.execute(f"UPDATE {TABLE} SET price_status='pending' "
                "WHERE price_status='no_candles'")
    q = (f"SELECT DISTINCT symbol FROM {TABLE} WHERE price_status='pending' "
         "AND symbol IS NOT NULL AND symbol <> '' ORDER BY symbol")
    if limit:
        q += f" LIMIT {int(limit)}"
    syms = [r[0] for r in con.execute(q).fetchall()]
    return [NIFTY_SYMBOL] + syms


async def run_eod(days: int = 5, limit: int = 0) -> dict:
    """The whole after-close chain, in dependency order.

    candles -> AI labels -> prices -> company metadata. Each step is the
    existing implementation; this only decides that they run, and in what
    order. Prices must come after candles (nothing to price otherwise) and
    metadata last (it is the only step that does not depend on the others).
    """
    from app.services import warehouse_prices, warehouse_store

    out: dict[str, Any] = {}
    out["candles"] = await sync_symbols(pending_symbols(limit), days=days)
    # Live AI labels live in SQLite until something folds them in; without
    # this the ai_* columns only move when someone hits /rebuild by hand.
    out["ai_labels"] = warehouse_store.load_live()
    out["prices"] = await asyncio.to_thread(warehouse_prices.fill)
    out["metadata"] = warehouse_store.fill_metadata()
    log.info("candle_sync.eod_done", **{k: str(v)[:120] for k, v in out.items()})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=5, help="days of history per symbol")
    ap.add_argument("--limit", type=int, default=0, help="max symbols (0 = all)")
    ap.add_argument("--candles-only", action="store_true",
                    help="fetch candles but skip the fill chain")
    a = ap.parse_args()

    if a.candles_only:
        res = asyncio.run(sync_symbols(pending_symbols(a.limit), days=a.days))
    else:
        res = asyncio.run(run_eod(days=a.days, limit=a.limit))
    for k, v in res.items():
        print(f"  {k:<12} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
