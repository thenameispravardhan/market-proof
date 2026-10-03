"""Candles for the Algo page: read from the local store, top up from Fyers.

The store is the one `candle_sync` already maintains — 1-minute parquet per
symbol under AIdataset/stockdata (base export + monthly `_inc/` files). A
backtest asks for a range; whatever the store lacks is fetched from Fyers
`/data/history` (Fyers-only pricing) in 90-day 1-minute chunks and appended
with candle_sync's own writer, so the news dataset and the backtester share
one copy of every candle. Bars for any timeframe are resampled from the
1-minute store in DuckDB, aligned to the 09:15 open like Fyers' own candles.
"""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any, Optional

import duckdb

from app.logging_config import get_logger
from app.services.candle_sync import MAX_DAYS, _convert_and_append
from app.services.warehouse_prices import candle_source

log = get_logger(__name__)

DAY = 86400
# Symbols whose Fyers history ran out before a requested start: key -> the
# earliest ts asked for. Saves re-asking Fyers for a listing date's pre-history
# on every backtest.
_floor: dict[str, int] = {}


def fyers_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if ":" in s:
        return s
    from app.execution.symbols import resolve_fyers_symbol

    return resolve_fyers_symbol(s) or f"NSE:{s}-EQ"


def store_key(fy: str) -> str:
    """NSE:SBIN-EQ -> SBIN and NSE:NIFTY50-INDEX -> NIFTY50-INDEX (candle_sync's
    own names, so both writers share a file); anything else -> EXCH_NAME."""
    ex, _, name = fy.partition(":")
    if ex == "NSE":
        return name[:-3] if name.endswith("-EQ") else name
    return f"{ex}_{name}".replace("/", "_")


def coverage(key: str) -> Optional[dict[str, Any]]:
    src = candle_source(key)
    if not src:
        return None
    first, last, rows = duckdb.connect().execute(f"SELECT min(ts), max(ts), count(*) FROM {src}").fetchone()
    return {"first": first, "last": last, "rows": rows} if rows else None


def load(key: str, tf_min: int, start_ts: int, end_ts: int) -> Optional[dict[str, Any]]:
    """`tf_min`-minute OHLCV bars in [start_ts, end_ts), session minutes only."""
    src = candle_source(key)
    if not src:
        return None
    n = int(tf_min) * 60
    rows = duckdb.connect().execute(f"""
        SELECT o_s + ((ts - o_s) // {n}) * {n} AS b,
               arg_min(open, ts), max(high), min(low), arg_max(close, ts), sum(volume)
        FROM (SELECT DISTINCT ON (ts) ts, open, high, low, close, volume,
                     ts - ((ts + 19800) % 86400) + 33300 AS o_s
              FROM {src}
              WHERE ts >= ? AND ts < ? AND (ts + 19800) % 86400 BETWEEN 33300 AND 55799)
        GROUP BY b ORDER BY b""", [int(start_ts), int(end_ts)]).fetchall()
    return bars_from(rows, n)


def bars_from(rows: list, tf_s: int) -> dict[str, Any]:
    return {"t": [int(r[0]) for r in rows], "o": [float(r[1]) for r in rows],
            "h": [float(r[2]) for r in rows], "l": [float(r[3]) for r in rows],
            "c": [float(r[4]) for r in rows], "v": [int(r[5] or 0) for r in rows], "tf_s": tf_s}


def _backend():
    from app.api.market import _fyers_backend

    b = _fyers_backend()
    if b is None or not hasattr(b, "get_history_range"):
        raise RuntimeError("Fyers is not connected — log in on the Accounts page to download candles")
    return b


async def download(fy: str, start_ts: int, end_ts: int) -> dict[str, Any]:
    """Fetch 1-minute candles for [start_ts, end_ts] newest-first, 90 days a call.

    Stops at the first empty 90-day window: a quarter with no session at all
    means the symbol was not listed yet (Fyers has nothing older).
    """
    backend, key = _backend(), store_key(fy)
    calls = added = 0
    hi, exhausted = int(end_ts), False
    while hi > start_ts:
        lo = max(int(start_ts), hi - MAX_DAYS * DAY)
        raw = await backend.get_history_range(fy, resolution="1", from_ts=lo, to_ts=hi)
        calls += 1
        if raw is None:
            raise RuntimeError(f"Fyers history call failed for {fy}")
        if raw:
            added += await asyncio.to_thread(_convert_and_append, key, raw) or 0
        elif hi - lo >= 30 * DAY:
            exhausted = True
            break
        hi = lo
        await asyncio.sleep(0.35)       # Fyers rate-limits /data/history
    if exhausted:
        _floor[key] = min(_floor.get(key, hi), int(start_ts))
    log.info("algo.download", symbol=fy, calls=calls, added=added, exhausted=exhausted)
    return {"symbol": fy, "calls": calls, "added": added}


async def ensure(fy: str, start_ts: int, end_ts: int) -> dict[str, Any]:
    """Make the store cover [start_ts, end_ts], downloading only the edges it
    lacks. Without a Fyers login it falls back to whatever is already stored."""
    key = store_key(fy)
    cov = coverage(key)
    note = None
    try:
        if cov is None:
            await download(fy, start_ts, end_ts)
        else:
            if start_ts < cov["first"] - 3 * DAY and _floor.get(key, start_ts + 1) > start_ts:
                await download(fy, start_ts, cov["first"])
            if end_ts > cov["last"] + 3600:     # one cheap call even when nothing is new
                await download(fy, cov["last"], min(end_ts, int(time.time())))
    except RuntimeError as e:
        note = str(e)
        if coverage(key) is None:
            raise
    return {"symbol": fy, "key": key, "note": note, "coverage": coverage(key)}


async def recent_bars(fy: str, tf_min: int, bars: int = 400, now: Optional[float] = None) -> dict[str, Any]:
    """The last `bars` COMPLETED bars straight from Fyers at the strategy's
    resolution (live runner). The still-forming bar is dropped — a strategy
    must never act on a candle that can still change."""
    now = int(now or time.time())
    tf_s = int(tf_min) * 60
    days = min(MAX_DAYS, max(5, math.ceil(bars * tf_min / 375 * 1.6) + 4))
    raw = await _backend().get_history_range(fy, resolution=str(tf_min), from_ts=now - days * DAY, to_ts=now)
    if raw is None:
        raise RuntimeError(f"Fyers history call failed for {fy}")
    rows = [r for r in raw if isinstance(r, (list, tuple)) and len(r) >= 6 and int(r[0]) + tf_s <= now]
    return bars_from(rows[-bars:], tf_s)
