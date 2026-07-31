"""Fill price/volume columns for announcements that do not have them yet.

A filing enters the dataset the moment it is collected, with px_*/vol_*/day_*
NULL and price_status='pending'. This fills them from the 1-minute candle store:

    px_pre / vol_pre     the minute before the announcement
    px_t0 / vol_t0       the minute containing it (its CLOSE — never lookahead)
    px_1m .. px_60m      1..15, 20, 30, 45, 60 minutes after
    day_high/low/volume  the session those prices come from
    ret_/mkt_/adj_       returns, market-adjusted against NIFTY 50

PURE SQL, NO PANDAS. The service deliberately has no pandas/numpy — on a 2 GB
box shared with a live trader, importing them for one admin job costs more
memory than the job. Everything below runs inside DuckDB: one pass per symbol
that joins the candle window and pivots it by minute-offset, so peak memory is
one symbol's candles regardless of how many announcements are pending.

    POST /api/warehouse/rebuild {"prices": true}     # in-process (owns the lock)
    python -m app.services.warehouse_prices          # only when the service is down
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from app.logging_config import get_logger
from app.services.warehouse_store import TABLE, _lock, connect

log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
CANDLES = ROOT / "AIdataset" / "stockdata"
NIFTY_SYMBOL = "NIFTY50-INDEX"
NIFTY = CANDLES / f"{NIFTY_SYMBOL}.parquet"

OFFSETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 30, 45, 60)
HORIZONS = (5, 15, 30, 60)


def _pivot_columns() -> str:
    """max(CASE WHEN off = N ...) for every price point, in one pass."""
    parts = ["max(CASE WHEN off = -1 THEN close END) AS px_pre",
             "max(CASE WHEN off = -1 THEN volume END) AS vol_pre",
             "max(CASE WHEN off = 0 THEN close END) AS px_t0",
             "max(CASE WHEN off = 0 THEN volume END) AS vol_t0"]
    for n in OFFSETS:
        parts.append(f"max(CASE WHEN off = {n} THEN close END) AS px_{n}m")
        parts.append(f"max(CASE WHEN off = {n} THEN volume END) AS vol_{n}m")
    return ",\n           ".join(parts)


def candle_source(symbol: str) -> Optional[str]:
    """A read_parquet() over one symbol's base export plus its increments.

    The base file is the original one-time export. Everything collected since
    lands in `_inc/<SYMBOL>/<YYYYMM>.parquet`, so appending a day costs one
    month of candles to rewrite instead of the symbol's whole history.

    They live in a per-symbol DIRECTORY rather than a `SYMBOL*.parquet` glob on
    purpose: `RELIANCE*.parquet` would also match RELIANCEPOWER and silently
    price one company off another's candles.
    """
    parts = []
    base = CANDLES / f"{symbol}.parquet"
    if base.exists():
        parts.append(base)
    inc_dir = CANDLES / "_inc" / symbol
    if inc_dir.is_dir():
        parts += sorted(inc_dir.glob("*.parquet"))
    if not parts:
        return None
    files = ", ".join(f"'{p.as_posix()}'" for p in parts)
    return f"read_parquet([{files}], union_by_name=true)"


def fill_symbol(con, symbol: str) -> int:
    """Fill every pending row for one symbol. One query, one UPDATE."""
    src = candle_source(symbol)
    if src is None:
        with _lock:
            con.execute(
                f"UPDATE {TABLE} SET price_status='no_candles' "
                "WHERE price_status='pending' AND symbol = ?", [symbol])
        return 0

    nif_src = candle_source(NIFTY_SYMBOL) or f"read_parquet('{NIFTY.as_posix()}')"
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE _fill AS
    WITH cand AS (
        SELECT CAST(datetime AS TIMESTAMP) AS ts, close, volume, high, low
        FROM {src}
    ),
    raw_pend AS (
        SELECT uid, anchor_time, date_trunc('minute', announced_at) AS filed_min
        FROM {TABLE}
        WHERE price_status = 'pending' AND symbol = ?
    ),
    -- The anchor is the first candle at or after the filing, NOT the filing
    -- minute. Most filings land after hours, where no candle exists until the
    -- next session opens; anchoring on announced_at alone finds nothing and
    -- discards the row. Letting the candle series define the anchor handles
    -- both cases without needing a trading calendar: intraday resolves to the
    -- containing minute, after-hours to the next session's open.
    pend AS (
        SELECT p.uid,
               coalesce(p.anchor_time,
                        (SELECT min(c.ts) FROM cand c WHERE c.ts >= p.filed_min)) AS t0
        FROM raw_pend p
    ),
    -- One join over the -1..+60 minute window, then pivot by offset. This is
    -- the whole trick: 21 price points from a single pass, no per-offset join.
    win AS (
        SELECT p.uid, p.t0,
               CAST(date_diff('minute', p.t0, c.ts) AS INTEGER) AS off,
               c.close, c.volume
        FROM pend p
        JOIN cand c
          ON c.ts BETWEEN p.t0 - INTERVAL 1 MINUTE AND p.t0 + INTERVAL 60 MINUTE
    ),
    px AS (
        SELECT uid, any_value(t0) AS t0,
           {_pivot_columns()}
        FROM win GROUP BY uid
    ),
    day AS (
        SELECT p.uid, max(c.high) AS day_high, min(c.low) AS day_low,
               sum(c.volume) AS day_volume
        FROM pend p JOIN cand c ON CAST(c.ts AS DATE) = CAST(p.t0 AS DATE)
        GROUP BY p.uid
    ),
    nif AS (
        SELECT CAST(datetime AS TIMESTAMP) AS ts, close
        FROM {nif_src}
    )
    SELECT px.*, day.day_high, day.day_low, day.day_volume,
           n0.close AS nif0,
           {", ".join(f"n{h}.close AS nif{h}" for h in HORIZONS)}
    FROM px
    LEFT JOIN day USING (uid)
    LEFT JOIN nif n0 ON n0.ts = px.t0
    {" ".join(f"LEFT JOIN nif n{h} ON n{h}.ts = px.t0 + INTERVAL {h} MINUTE"
              for h in HORIZONS)}
    WHERE px.px_t0 IS NOT NULL
    """, [symbol])

    n = con.execute("SELECT count(*) FROM _fill").fetchone()[0]
    if not n:
        with _lock:
            con.execute(
                f"UPDATE {TABLE} SET price_status='no_candles' "
                "WHERE price_status='pending' AND symbol = ?", [symbol])
        return 0

    px_cols = ["px_pre", "vol_pre", "px_t0", "vol_t0"]
    for o in OFFSETS:
        px_cols += [f"px_{o}m", f"vol_{o}m"]
    px_cols += ["day_high", "day_low", "day_volume"]
    sets = [f"{c} = f.{c}" for c in px_cols]
    sets.append("anchor_time = f.t0")
    for h in HORIZONS:
        ret = f"(f.px_{h}m - f.px_t0) / f.px_t0 * 100"
        mkt = f"CASE WHEN f.nif0 IS NULL OR f.nif{h} IS NULL THEN 0 " \
              f"ELSE (f.nif{h} - f.nif0) / f.nif0 * 100 END"
        sets += [f"ret_{h}m = {ret}", f"mkt_{h}m = {mkt}",
                 f"adj_{h}m = ({ret}) - ({mkt})"]
    adj30 = "((f.px_30m - f.px_t0)/f.px_t0*100) - (CASE WHEN f.nif0 IS NULL " \
            "OR f.nif30 IS NULL THEN 0 ELSE (f.nif30 - f.nif0)/f.nif0*100 END)"
    sets += [f"usable = (f.px_30m IS NOT NULL)",
             f"mover_1_5 = (abs({adj30}) > 1.5)",
             f"mover_3 = (abs({adj30}) > 3.0)",
             "price_status = 'filled'", "price_source = 'nse_candles'"]

    # The three columns the historical dataset carries that this fill used to
    # leave NULL. They are derived from what _fill already computed — no extra
    # scan — and use the SAME vocabulary as history, or the model sees two
    # encodings of one feature.
    #   session_offset  where the filing sits relative to its pricing session
    #   px_t0_age_min   minutes from the filing to the anchor candle
    #   data_quality    did the anchor candle actually trade
    # `_fill` carries t0 but not the filing minute, so the age is measured
    # against the target row's own announced_at rather than widening the CTE.
    filed = "date_trunc('minute', t.announced_at)"
    sets += [
        f"px_t0_age_min = date_diff('minute', {filed}, f.t0)",
        f"""session_offset = CASE
               WHEN date_diff('minute', {filed}, f.t0) <= 0 THEN 'same_session'
               WHEN CAST(f.t0 AS DATE) = CAST({filed} AS DATE)
                    THEN CASE WHEN CAST({filed} AS TIME) < TIME '09:15'
                              THEN 'same_day_preopen' ELSE 'same_session' END
               ELSE 'next_session' END""",
        """data_quality = CASE
               WHEN f.px_t0 IS NULL THEN 'no_price'
               WHEN coalesce(f.vol_t0, 0) > 0 THEN 'traded'
               ELSE 'stale' END""",
    ]

    # Held per SYMBOL, not for the whole run: this shares the table with live
    # ingestion, and the fill walks thousands of symbols. One lock around the
    # loop would stall the monitors for the length of the backfill.
    with _lock:
        con.execute(f"UPDATE {TABLE} t SET {', '.join(sets)} "
                    f"FROM _fill f WHERE t.uid = f.uid")
        # Anything still pending for this symbol had no candle at its anchor.
        con.execute(f"UPDATE {TABLE} SET price_status='no_candles' "
                    "WHERE price_status='pending' AND symbol = ?", [symbol])
    return n


def fill(limit: int = 0, symbols: Optional[list[str]] = None) -> dict:
    """Fill pending rows. `symbols` restricts the sweep to those symbols.

    The real-time pass only ever touches the handful it just fetched candles
    for; walking all ~4,600 pending symbols every few minutes would burn far
    more time than the fill it performs.
    """
    con = connect()
    if symbols is not None:
        symbols = sorted({s for s in symbols if s})
        if not symbols:
            return {"symbols": 0, "filled": 0}
    else:
        q = (f"SELECT DISTINCT symbol FROM {TABLE} "
             "WHERE price_status = 'pending' AND symbol IS NOT NULL ORDER BY symbol")
        if limit:
            q += f" LIMIT {limit}"
        symbols = [r[0] for r in con.execute(q).fetchall()]
    if not symbols:
        return {"symbols": 0, "filled": 0}

    total = 0
    for i, sym in enumerate(symbols, 1):
        try:
            total += fill_symbol(con, sym)
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the run
            log.warning("warehouse_prices.symbol_failed", symbol=sym, error=str(e)[:140])
        if i % 500 == 0:
            log.info("warehouse_prices.progress", done=i, of=len(symbols), filled=total)
    out = {"symbols": len(symbols), "filled": total}
    log.info("warehouse_prices.done", **out)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max symbols")
    a = ap.parse_args()
    for k, v in fill(a.limit).items():
        print(f"  {k:<10} {v:,}")
    con = connect()
    print("price_status:", dict(con.execute(
        f"select price_status, count(*) from {TABLE} group by 1").fetchall()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
