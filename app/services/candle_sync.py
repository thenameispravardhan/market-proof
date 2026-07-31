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
from app.services.warehouse_store import TABLE, _lock, connect

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
    """Merge rows into the symbol's MONTHLY increment files. Rows added.

    Not into the base export. Rewriting <SYMBOL>.parquet to add one day cost
    the symbol's whole history every time — measured on the server, that made a
    full pass under 5 symbols/minute, ~15 hours for the backlog. A month of
    1-minute candles is ~7.5k rows, so an append now rewrites that instead of
    1.5 years, and warehouse_prices.candle_source() reads base + increments as
    one table.

    Per-symbol DIRECTORY, not a SYMBOL*.parquet glob: the glob would also match
    RELIANCEPOWER when reading RELIANCE.
    """
    if not rows:
        return 0
    con = connect()
    inc_dir = CANDLES / "_inc" / symbol
    inc_dir.mkdir(parents=True, exist_ok=True)

    con.execute("""CREATE OR REPLACE TEMP TABLE _new
                   (ts BIGINT, datetime TIMESTAMP, open DOUBLE, high DOUBLE,
                    low DOUBLE, close DOUBLE, volume BIGINT)""")
    con.executemany("INSERT INTO _new VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    # Already in the base export? Then it is not new. Checked once here rather
    # than per month, so a re-fetch overlapping the export adds nothing.
    base = CANDLES / f"{symbol}.parquet"
    if base.exists():
        con.execute(f"DELETE FROM _new WHERE ts IN "
                    f"(SELECT ts FROM read_parquet('{base.as_posix()}'))")

    added = 0
    months = [r[0] for r in con.execute(
        "SELECT DISTINCT strftime(datetime, '%Y%m') FROM _new ORDER BY 1").fetchall()]
    for ym in months:
        f = inc_dir / f"{ym}.parquet"
        month = f"SELECT * FROM _new WHERE strftime(datetime, '%Y%m') = '{ym}'"
        if f.exists():
            src = (f"SELECT * FROM read_parquet('{f.as_posix()}') UNION ALL "
                   f"{month} AND ts NOT IN "
                   f"(SELECT ts FROM read_parquet('{f.as_posix()}'))")
            before = con.execute(
                f"SELECT count(*) FROM read_parquet('{f.as_posix()}')").fetchone()[0]
        else:
            src, before = month, 0
        tmp = f.with_suffix(".parquet.tmp")
        con.execute(f"COPY ({src} ORDER BY ts) TO '{tmp.as_posix()}' (FORMAT parquet)")
        after = con.execute(
            f"SELECT count(*) FROM read_parquet('{tmp.as_posix()}')").fetchone()[0]
        tmp.replace(f)  # atomic swap — a crash mid-write never truncates the store
        added += after - before
    return added


def _convert_and_append(symbol: str, raw: list[Any]) -> Optional[int]:
    """Parse + persist one symbol's candles. None when there was nothing.

    The blocking half of a sync, kept together so it is one hop to a worker
    thread instead of two. `connect()` hands out a thread-local cursor, so a
    threadpool worker gets its own — which is exactly what that design is for.
    """
    rows = _candle_rows(raw)
    if not rows:
        return None
    return _append(symbol, rows)


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
            raw = await _fetch(broker, days)
            # Everything after the fetch is blocking: _candle_rows walks up to
            # ~35k candles and _append rewrites a multi-MB parquet. Run on the
            # event loop it starves the bot — measured 2026-07-31, monitor polls
            # fell 147/min -> 13/min and the tick heartbeat stopped. The fetch
            # itself stays awaited here, on the loop the Fyers client belongs to.
            added_now = await asyncio.to_thread(_convert_and_append, sym, raw)
            if added_now is None:
                failed += 1
            else:
                added += added_now
                ok += 1
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
            failed += 1
            log.warning("candle_sync.symbol_failed", symbol=sym, error=str(e)[:140])
        if i % 50 == 0:
            log.info("candle_sync.progress", done=i, of=len(symbols),
                     ok=ok, failed=failed, added=added)
    out = {"symbols": len(symbols), "ok": ok, "failed": failed, "candles_added": added}
    log.info("candle_sync.done", **out)
    return out


def pending_symbols(days: int, limit: int = 0) -> list[str]:
    """Symbols with a row still waiting on prices, NIFTY always first.

    `no_candles` rows are reconsidered, not just `pending` ones: they were
    marked when the file was missing, and the point of this module is that the
    file can now exist. Otherwise the backlog is frozen forever.

    But only within the window this run can actually reach. Fyers serves a
    bounded range of 1-minute history, so a filing older than that will never
    price no matter how often it is retried — and retrying it costs a history
    call per symbol, every single day. Bounding the reset by `days` is what
    keeps the daily job proportional to the day's news instead of re-walking
    the whole archive.
    """
    con = connect()
    with _lock:
        con.execute(f"UPDATE {TABLE} SET price_status='pending' "
                    "WHERE price_status='no_candles' "
                    f"AND announced_at >= current_date - INTERVAL {int(days)} DAY")
    q = (f"SELECT DISTINCT symbol FROM {TABLE} WHERE price_status='pending' "
         "AND symbol IS NOT NULL AND symbol <> '' ORDER BY symbol")
    if limit:
        q += f" LIMIT {int(limit)}"
    syms = [r[0] for r in con.execute(q).fetchall()]
    return [NIFTY_SYMBOL] + syms


def ripe_symbols(max_age_days: int = 3, min_age_minutes: int = 61,
                 limit: int = 0) -> list[str]:
    """Symbols whose fresh announcements are old enough to be fully priced.

    px_60m cannot exist until 60 minutes of trading have passed, so a filing is
    only worth fetching once it is `min_age_minutes` old — asking sooner just
    marks it no_candles and burns a call. `max_age_days` keeps each pass to
    recent news; the daily sweep handles anything older.

    NIFTY rides along on every pass: mkt_*/adj_* are measured against it, so a
    stale index silently zeroes the market adjustment for every other symbol.
    """
    con = connect()
    with _lock:
        con.execute(f"""
            UPDATE {TABLE} SET price_status='pending'
            WHERE price_status='no_candles'
              AND announced_at >= current_date - INTERVAL {int(max_age_days)} DAY
        """)
    q = (f"SELECT DISTINCT symbol FROM {TABLE} "
         "WHERE price_status='pending' AND symbol IS NOT NULL AND symbol <> '' "
         f"AND announced_at >= current_date - INTERVAL {int(max_age_days)} DAY "
         f"AND announced_at <= now() - INTERVAL {int(min_age_minutes)} MINUTE "
         "ORDER BY symbol")
    if limit:
        q += f" LIMIT {int(limit)}"
    return [NIFTY_SYMBOL] + [r[0] for r in con.execute(q).fetchall()]


async def run_incremental(max_age_days: int = 3, days: int = 2,
                          limit: int = 0) -> dict:
    """The real-time pass: price the day's news as soon as it is priceable.

    Small by construction — only symbols with fresh unfilled rows, only a
    couple of days of candles each, and the price fill is restricted to those
    same symbols instead of sweeping the whole pending set.
    """
    from app.services import warehouse_prices, warehouse_store

    syms = ripe_symbols(max_age_days=max_age_days, limit=limit)
    out: dict[str, Any] = {"candles": await sync_symbols(syms, days=days)}
    out["prices"] = await asyncio.to_thread(warehouse_prices.fill, 0, syms)
    out["metadata"] = warehouse_store.fill_metadata()
    log.info("candle_sync.incremental_done", **{k: str(v)[:120] for k, v in out.items()})
    return out


async def run_eod(days: int = 5, limit: int = 0) -> dict:
    """The whole after-close chain, in dependency order.

    candles -> AI labels -> prices -> company metadata. Each step is the
    existing implementation; this only decides that they run, and in what
    order. Prices must come after candles (nothing to price otherwise) and
    metadata last (it is the only step that does not depend on the others).
    """
    from app.services import warehouse_prices, warehouse_store

    out: dict[str, Any] = {}
    out["candles"] = await sync_symbols(pending_symbols(days, limit), days=days)
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
        res = asyncio.run(sync_symbols(pending_symbols(a.days, a.limit), days=a.days))
    else:
        res = asyncio.run(run_eod(days=a.days, limit=a.limit))
    for k, v in res.items():
        print(f"  {k:<12} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
