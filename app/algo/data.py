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
from array import array
from contextvars import ContextVar
from typing import Any, Optional, Sequence

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


def backtest_db() -> duckdb.DuckDBPyConnection:
    """A DuckDB of its own for one backtest's reads, closed when it ends (its
    memory goes back then). Memory-capped so a big universe can't take the
    box's RAM; no file cache (the store is local, and rewritten in place)."""
    return duckdb.connect(config={"memory_limit": "512MB", "enable_external_file_cache": False,
                                  "allocator_flush_threshold": "16MB"})


# The running backtest's DuckDB (assemble sets it for its own task): coverage()
# and load() use it instead of starting a database per call — ~25 ms each, more
# than the read itself, three or four times per symbol, some on the event loop.
_active: ContextVar[Optional[duckdb.DuckDBPyConnection]] = ContextVar("algo_data_db", default=None)


def coverage(key: str) -> Optional[dict[str, Any]]:
    src = candle_source(key)
    if not src:
        return None
    sql = f"SELECT min(ts), max(ts), count(*) FROM {src}"
    con = _active.get()
    if con is not None:
        first, last, rows = con.execute(sql).fetchone()
    else:
        with duckdb.connect() as own:
            first, last, rows = own.execute(sql).fetchone()
    return {"first": first, "last": last, "rows": rows} if rows else None


# The session minutes of [start, end), one row per minute (base export and
# increments can overlap).
_M1 = """SELECT DISTINCT ON (ts) ts, open, high, low, close, volume FROM {src}
         WHERE ts >= ? AND ts < ? AND (ts + 19800) % 86400 BETWEEN 33300 AND 55799"""
_FETCH = 50_000     # rows per fetch: a result never sits in memory whole as Python tuples


WEEK = 10080         # minutes in a week: weekly candles


def _resample(con: duckdb.DuckDBPyConnection, rel: str, tf_min: int, params: Sequence[Any] = ()) -> dict[str, Any]:
    if tf_min == WEEK:
        return _resample_weekly(con, rel, params)
    n = int(tf_min) * 60
    o_s = "(ts - ((ts + 19800) % 86400) + 33300)"            # the day's 09:15 open
    bucket = "ts - ((ts + 19800) % 86400)" if tf_min == 1440 else f"{o_s} + ((ts - {o_s}) // {n}) * {n}"
    cur = con.execute(f"""SELECT {bucket} AS b, arg_min(open, ts), max(high), min(low), arg_max(close, ts), sum(volume)
                          FROM {rel} GROUP BY b ORDER BY b""", list(params))
    return columns(cur, n)


def _resample_weekly(con: duckdb.DuckDBPyConnection, rel: str, params: Sequence[Any] = ()) -> dict[str, Any]:
    """Weekly candles (IST Monday to Sunday), stamped at the 09:15 open of the
    week's first session with `tc` = the 15:30 close of its last one."""
    day = "(ts - ((ts + 19800) % 86400))"
    week = f"({day} - ((((ts + 19800) // 86400) + 3) % 7) * 86400)"
    cur = con.execute(f"""SELECT {week} AS w, min(ts), arg_min(open, ts), max(high), min(low), arg_max(close, ts),
                                 sum(volume), max(ts)
                          FROM {rel} GROUP BY w ORDER BY w""", list(params))
    t, tc, v = array("q"), array("q"), array("q")
    o, h, l, c = array("d"), array("d"), array("d"), array("d")
    while rows := cur.fetchmany(_FETCH):
        t.extend(r[1] - (r[1] + 19800) % DAY + 33300 for r in rows)
        tc.extend(r[7] - (r[7] + 19800) % DAY + 55800 for r in rows)
        o.extend(r[2] for r in rows)
        h.extend(r[3] for r in rows)
        l.extend(r[4] for r in rows)
        c.extend(r[5] for r in rows)
        v.extend(int(r[6] or 0) for r in rows)
    return {"t": t, "tc": tc, "o": o, "h": h, "l": l, "c": c, "v": v, "tf_s": WEEK * 60}


def as_base(b: Optional[dict[str, Any]], tf_min: int) -> Optional[dict[str, Any]]:
    """Daily candles as a strategy's OWN timeframe: stamped at the session's
    09:15 open (when its entries fill) with `tc` at the 15:30 close (when it is
    evaluated) — loaded / fetched daily candles are stamped at IST midnight,
    which is right for a condition reading them but no time to trade at.
    Weekly candles already carry both. In place; returns `b`."""
    if b and tf_min == 1440 and b.get("t") is not None and len(b["t"]):
        day0 = [x - (x + 19800) % DAY for x in b["t"]]
        mk = (lambda xs: array("q", xs)) if isinstance(b["t"], array) else list
        b["t"], b["tc"] = mk(x + 33300 for x in day0), mk(x + 55800 for x in day0)
    return b


def load(key: str, tf_min: int, start_ts: int, end_ts: int) -> Optional[dict[str, Any]]:
    """`tf_min`-minute OHLCV bars in [start_ts, end_ts), session minutes only.
    tf_min=1440 gives daily bars stamped at IST midnight, 10080 weekly ones."""
    src = candle_source(key)
    if not src:
        return None
    con = _active.get()
    if con is not None:
        return _resample(con, f"({_M1.format(src=src)})", tf_min, (int(start_ts), int(end_ts)))
    with duckdb.connect() as own:
        return _resample(own, f"({_M1.format(src=src)})", tf_min, (int(start_ts), int(end_ts)))


def load_frames(key: str, tfs: Sequence[int], start_ts: int, end_ts: int,
                con: Optional[duckdb.DuckDBPyConnection] = None) -> Optional[list[dict[str, Any]]]:
    """load() at each of `tfs` (in that order) off ONE read of the symbol's
    parquet: its minutes go to a temp table once and every timeframe is
    resampled from there. A backtest read the files once per timeframe."""
    src = candle_source(key)
    if not src:
        return None
    own = con is None
    con = con or backtest_db()
    try:
        if len(tfs) == 1:           # one timeframe: nothing to share, so no copy of every minute into a temp table
            return [_resample(con, f"({_M1.format(src=src)})", tfs[0], (int(start_ts), int(end_ts)))]
        con.execute(f"CREATE OR REPLACE TEMP TABLE _m1 AS {_M1.format(src=src)}", [int(start_ts), int(end_ts)])
        try:
            return [_resample(con, "_m1", tf) for tf in tfs]
        finally:
            con.execute("DROP TABLE IF EXISTS _m1")
    finally:
        if own:
            con.close()


def warmup_days(tf_min: int, bars: int) -> int:
    """Calendar days of history that hold `bars` bars of `tf_min` (375 session minutes a day)."""
    if tf_min == WEEK:
        return int(bars * 7 * 1.1) + 10
    per_day = 1 if tf_min == 1440 else max(1, 375 // tf_min)
    return int(bars / per_day * 1.5) + 4


VIX = "NSE:INDIAVIX-INDEX"


def real_flow(fy: str, start: int, end: int) -> dict[int, tuple[float, float]]:
    """Recorded buy/sell volume per minute for a symbol (an index reads its
    future's), or {} where nothing was recorded."""
    from app.algo import ticks

    try:
        return ticks.store().minute_flow(ticks.flow_key(fy), start, end)
    except Exception as e:  # noqa: BLE001 — real flow is an upgrade, never a blocker
        log.warning("algo.real_flow_failed", symbol=fy, error=str(e)[:200])
        return {}


def lead_days(spec: dict[str, Any]) -> int:
    """Calendar days of history loaded before a backtest's start: the warm-up of
    the base candles, of every higher timeframe the conditions read, and RVOL's
    whole sessions (at most 800)."""
    from app.algo import engine

    wb = engine.warmup_bars(spec)
    btype, per_day = spec["bars"]["type"], spec["bars"]["per_day"]
    base = warmup_days(spec["timeframe"], wb) if btype == "time" else int(wb / per_day * 1.5) + 25   # +20 sessions to size bars
    return min(800, max([base, warmup_days(1440, engine.warmup_sessions(spec))]
                        + [warmup_days(tf, wb) for tf in engine.cond_timeframes(spec)]))


async def assemble(spec: dict[str, Any], start: int, end: int) -> tuple[dict[str, dict], list[dict], int]:
    """Everything one backtest needs, per symbol: base bars from `start - warmup`,
    every higher timeframe the conditions use, India VIX when options are priced
    off it, and the F&O meta (name, lot, strike step). Returns (data, notes,
    trade_from). One failing symbol is a note, not an error."""
    from app.algo import fno

    if spec["instrument"]["type"] != "equity" or (spec.get("pair_leg") or {}).get("kind") == "FUT":   # lots of the pair leg's future
        await fno.ensure_master()
    con = backtest_db()
    token = _active.set(con)
    try:
        return await _assemble(spec, start, end, con)
    finally:
        _active.reset(token)
        con.close()


async def _assemble(spec: dict[str, Any], start: int, end: int, con: duckdb.DuckDBPyConnection
                    ) -> tuple[dict[str, dict], list[dict], int]:
    from app.algo import engine, fno, orderflow

    base_tf = spec["timeframe"]
    htfs = sorted(engine.cond_timeframes(spec))
    minutes = engine.needs_minutes(spec)
    btype, per_day = spec["bars"]["type"], spec["bars"]["per_day"]
    first = start - lead_days(spec) * DAY
    inst = spec["instrument"]
    vix = None
    notes: list[dict] = []
    wants_vix = inst["iv"]["source"] == "vix" or (       # surface: VIX prices the candles no snapshot covers
        inst["iv"]["source"] in ("auto", "surface") and any(fno.fno_name(x) in fno.INDEX_LOTS for x in spec["symbols"]))
    vix_tfs = engine.vix_timeframes(spec)            # the VIX indicator, at these timeframes
    vix_htf: dict[int, dict] = {}
    if (inst["type"] == "option" and wants_vix) or vix_tfs:
        try:
            await ensure(VIX, first, end)
            others = sorted(vix_tfs - {base_tf})
            fr = await asyncio.to_thread(load_frames, store_key(VIX), [base_tf, *others], first, end, con)
            if fr:
                vix, vix_htf = as_base(fr[0], base_tf), dict(zip(others, fr[1:]))
        except Exception as e:  # noqa: BLE001
            notes.append({"symbol": VIX, "note": f"VIX unavailable ({'conditions on VIX stay false' if vix_tfs else 'using realised vol'}): {e}"[:200]})
    # Other symbols the conditions read: loaded once, shared by every symbol's
    # bars. Missing one is an error — those conditions can't be evaluated.
    xs: dict[str, dict] = {}
    for ref, tfs in engine.ref_frames(spec).items():
        try:
            info = await ensure(ref, first, end)
        except RuntimeError as e:
            raise ValueError(f"{ref} (read by the conditions): {e}") from None
        others = sorted(tfs - {base_tf})
        fr = await asyncio.to_thread(load_frames, info["key"], [base_tf, *others], first, end, con)
        b = as_base(fr[0], base_tf) if fr else None
        if not b or not b["t"] or b["t"][-1] < start:
            raise ValueError(f"{ref} (read by the conditions): no candles in this range")
        b["tf_min"] = base_tf
        b["htf"] = dict(zip(others, fr[1:]))
        b["meta"] = {"symbol": ref}
        xs[ref] = b
    out: dict[str, dict] = {}
    symbols, member = list(spec["symbols"]), None
    if spec.get("universe"):                         # the index as it was each day, dropped stocks included
        member, symbols, note = universe_members(spec["universe"]["index"], start, end)
        notes.append({"symbol": spec["universe"]["index"], "note": note})
    for fy in symbols:
        try:
            info = await ensure(fy, first, end)
            if info["note"]:
                notes.append({"symbol": fy, "note": info["note"]})
            size = None
            # the base (or 1-minute) candles and every higher timeframe off one read of the files
            fr = await asyncio.to_thread(load_frames, info["key"], [1 if minutes else base_tf, *htfs], first, end, con)
            htf = dict(zip(htfs, fr[1:])) if fr else {tf: None for tf in htfs}
            if minutes:
                m1 = fr[0] if fr else None
                if not m1 or not m1["t"] or m1["t"][-1] < start:
                    notes.append({"symbol": fy, "note": "no candles in this range"})
                    continue
                if btype != "time":
                    try:
                        size = orderflow.bar_size(m1, btype, per_day, until=start)
                    except ValueError as e:
                        notes.append({"symbol": fy, "note": str(e)})
                        continue
                elif not any(m1["v"]):
                    notes.append({"symbol": fy, "note": "no traded volume (an index) — order-flow values are flat"})
                real = await asyncio.to_thread(real_flow, fy, first, end)
                bars = await asyncio.to_thread(orderflow.build_bars, m1, btype, base_tf, size, real)
                del m1, fr
            else:
                bars = fr[0] if fr else None
                del fr
            if not bars or not bars["t"] or bars["t"][-1] < start:
                notes.append({"symbol": fy, "note": "no candles in this range"})
                continue
            as_base(bars, base_tf)
            bars["tf_min"] = base_tf
            bars["htf"] = htf
            if xs:
                bars["xs"] = xs
            if engine.uses_filings(spec):
                _attach_filings(bars, await asyncio.to_thread(filing_rows, fy, first, end))
            if member is not None:
                bars["member_days"] = member[fy]
            if vix and vix["t"]:
                bars["vix"] = vix
            for tf, v in vix_htf.items():
                if v and v["t"] and bars["htf"].get(tf):
                    bars["htf"][tf]["vix"] = v
            name = fno.fno_name(fy)
            bars["meta"] = {"symbol": fy, "name": name, "exch": fno.exchange_of(fy), "bar_size": size}
            if inst["type"] == "option" and inst["iv"]["source"] == "surface":
                bars["surfaces"] = await asyncio.to_thread(chain_surfaces, name, bars["t"])
                got = sum(1 for x in bars["surfaces"] if x)
                notes.append({"symbol": fy, "note": f"recorded IV surface on {got / len(bars['t']) * 100:.0f}% of candles"
                              + (" — the rest priced with flat IV (VIX / realised)" if got < len(bars["t"]) else "")})
            if inst["type"] != "equity":
                if not fno.is_fno(name):
                    notes.append({"symbol": fy, "note": f"{name} has no F&O contracts — skipped"})
                    continue
                bars["meta"]["lot"] = fno.lot_size(name)
                bars["meta"]["step"] = fno.default_step(name, bars["c"][-1])
            out[fy] = bars
        except Exception as e:  # noqa: BLE001
            notes.append({"symbol": fy, "note": str(e)[:200]})
    if member is not None:
        gone = [fy for fy in symbols if fy not in out]
        if gone:
            notes.append({"symbol": spec["universe"]["index"],
                          "note": f"{len(gone)} of {len(symbols)} past members have no candles (delisted / renamed?) — "
                                  f"survivorship bias remains for them: {', '.join(store_key(x) for x in gone[:20])}"
                                  + ("…" if len(gone) > 20 else "")})
    return out, notes, start


def universe_members(index: str, start: int, end: int) -> tuple[dict[str, set[int]], list[str], str]:
    """({NSE:X-EQ: member IST days}, those symbols, a note) for an index over
    [start, end) — every stock that was in it at any point of the range."""
    from app.research import universe

    m = universe.members_between(index, universe.ist_day(start), universe.ist_day(end - 1))
    member = {f"NSE:{t}-EQ": days for t, days in sorted(m.items())}
    cov = universe.coverage(index)
    note = f"{cov['label']} as it was each day: {len(member)} stocks were members during the range"
    if cov["trusted_from"] and cov["trusted_from"] > universe.ymd(universe.ist_day(start)):
        note += (f"; membership before {cov['trusted_from']} assumes no index changes (import the change log, "
                 "Research → universes)")
    return member, list(member), note


def filing_rows(fy: str, start: int, end: int) -> list[tuple[int, str, Optional[float]]]:
    """The symbol's exchange filings in [start, end) from the warehouse, each at
    the moment it became public (disseminated, else announced; stored IST):
    [(epoch, event type, AI sentiment -100..100 or None)], oldest first. Only
    NSE shares have them; anything else gets []."""
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from app.services import warehouse_store as ws

    if not fy.startswith("NSE:") or not fy.endswith("-EQ"):
        return []
    ist = lambda e: _dt.fromtimestamp(e + 19800, _tz.utc).replace(tzinfo=None)   # noqa: E731
    try:
        rows = ws.connect().execute(
            f"SELECT epoch(coalesce(disseminated_at, announced_at)) - 19800, upper(coalesce(ai_event_type, 'OTHER')), "
            f"ai_sentiment_score FROM {ws.TABLE} WHERE symbol = ? AND coalesce(disseminated_at, announced_at) >= ? "
            "AND coalesce(disseminated_at, announced_at) < ? ORDER BY 1",
            [store_key(fy), ist(start), ist(end)]).fetchall()
    except Exception as e:  # noqa: BLE001 — no warehouse: no filings, conditions on them stay false / 0
        log.warning("algo.filings_unavailable", symbol=fy, error=str(e)[:200])
        return []
    return [(int(r[0]), r[1], None if r[2] is None else float(r[2])) for r in rows]


def _attach_filings(bars: dict, rows: list) -> None:
    bars["filings"] = rows
    for h in (bars.get("htf") or {}).values():
        if h:
            h["filings"] = rows


def chain_snapshots(name: str, start: int, end: int):
    """`name`'s recorded chain snapshots in [start, end), one IST day read at a
    time (a day of a chain is ~35k rows; months of it at once would not fit
    the box): yields (ts, [{key, kind, strike, expiry, qts, ltp, bid, ask}])."""
    from app.algo import ticks

    st = ticks.store()
    for day in range((start + 19800) // DAY, (end - 1 + 19800) // DAY + 1):
        meta = st.chain_keys(name, day)
        if not meta:
            continue
        d0 = day * DAY - 19800
        snaps: dict[int, list[dict]] = {}
        for r in st.chain_rows(name, max(start, d0), min(end, d0 + DAY)):
            m = meta.get(r[1], {})
            snaps.setdefault(r[0], []).append({"key": r[1], "kind": m.get("kind"), "strike": m.get("strike"),
                                               "expiry": m.get("expiry"), "qts": r[2], "ltp": r[3], "bid": r[4], "ask": r[5]})
        for t in sorted(snaps):
            yield t, snaps[t]


def chain_surfaces(name: str, times: list[int]) -> list[Any]:
    """Per candle: the IV surface of `name`'s latest recorded chain snapshot
    at or before the candle's open, the same IST day; None where none was
    recorded. Each snapshot used is fitted once."""
    from app.research import surface as sf

    out: list[Any] = [None] * len(times)
    by_day: dict[int, list[int]] = {}
    for i, t in enumerate(times):
        by_day.setdefault((t + 19800) // DAY, []).append(i)
    for day, idx in by_day.items():
        d0 = day * DAY - 19800
        snaps = list(chain_snapshots(name, d0, d0 + DAY))      # one day at a time
        fitted: dict[int, Any] = {}
        j = -1
        for i in idx:
            while j + 1 < len(snaps) and snaps[j + 1][0] <= times[i]:
                j += 1
            if j < 0:
                continue
            t, contracts = snaps[j]
            if t not in fitted:
                fitted[t] = sf.fit(contracts, t)
            out[i] = fitted[t]
    return out


def columns(cur: duckdb.DuckDBPyConnection, tf_s: int) -> dict[str, Any]:
    """Backtest bars as typed arrays (8 bytes a value; a list of Python floats
    takes ~32), filled a chunk of rows at a time. Index / slice / iterate them
    like the lists bars_from gives.

    With numpy installed DuckDB fills each column in C++ and it is copied in one
    go — measured 4-7x faster than walking rows as Python tuples (20 symbols x
    19 months: 1.63 -> 0.41 s at 5-minute, 5.30 -> 0.78 s at 1-minute), same arrays."""
    try:
        import numpy as np
    except ImportError:          # the row walk below gives the same arrays, slower
        np = None
    if np is not None:
        names = [x[0] for x in cur.description][:6]
        r = cur.fetchnumpy()
        out: dict[str, Any] = {"tf_s": tf_s}
        for key, code, name in zip(("t", "o", "h", "l", "c", "v"), "qddddq", names):
            x = r[name]
            if np.ma.is_masked(x):
                if key != "v":   # the row walk can't store a NULL price either
                    raise TypeError(f"NULL {key} in the candles")
                x = np.ma.filled(x, 0)
            a = array(code)
            a.frombytes(np.ascontiguousarray(x, dtype=np.int64 if code == "q" else np.float64).tobytes())
            out[key] = a
        return out
    t, v = array("q"), array("q")
    o, h, l, c = array("d"), array("d"), array("d"), array("d")
    while rows := cur.fetchmany(_FETCH):
        t.extend(r[0] for r in rows)
        o.extend(r[1] for r in rows)
        h.extend(r[2] for r in rows)
        l.extend(r[3] for r in rows)
        c.extend(r[4] for r in rows)
        v.extend(int(r[5] or 0) for r in rows)
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "tf_s": tf_s}


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
    from app.execution.data_budget import bulk

    key = store_key(fy)
    with bulk():                # backtest downloads: the data budget's bulk lane
        return await _ensure(fy, key, coverage(key), start_ts, end_ts)


async def _ensure(fy: str, key: str, cov: Optional[dict[str, Any]], start_ts: int, end_ts: int) -> dict[str, Any]:
    note = None
    fetched = False             # coverage re-read only when something may have been added
    try:
        if cov is None:
            fetched = True
            await download(fy, start_ts, end_ts)
        else:
            if start_ts < cov["first"] - 3 * DAY and _floor.get(key, start_ts + 1) > start_ts:
                fetched = True
                await download(fy, start_ts, cov["first"])
            if end_ts > cov["last"] + 3600:     # one cheap call even when nothing is new
                fetched = True
                await download(fy, cov["last"], min(end_ts, int(time.time())))
    except RuntimeError as e:
        note = str(e)
        if (cov := coverage(key)) is None:
            raise
        fetched = False
    return {"symbol": fy, "key": key, "note": note, "coverage": coverage(key) if fetched else cov}


# Resolutions Fyers' /data/history serves; anything else is resampled from one it does.
FYERS_RES = (1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 120, 180, 240, 1440)


def fetch_res(tf_min: int) -> int:
    """The Fyers resolution a `tf_min` candle is built from: itself when Fyers
    serves it, else the largest one that divides it (75 <- 15, 125 <- 5), and
    daily candles for a week."""
    if tf_min == WEEK:
        return 1440
    return tf_min if tf_min in FYERS_RES else max(r for r in FYERS_RES if r < 1440 and tf_min % r == 0)


def resample(b: dict[str, Any], tf_min: int) -> dict[str, Any]:
    """`b` (finer candles, Fyers-stamped) as `tf_min` candles: intraday from the
    09:15 open; weekly by IST week, stamped at its first session's 09:15 with
    `tc` = its last session's 15:30 close. Every input candle is kept, so a
    caller drops the one still forming."""
    week = tf_min == WEEK
    n = tf_min * 60
    out: dict[str, Any] = {k: [] for k in ("t", "o", "h", "l", "c", "v")}
    if week:
        out["tc"] = []
    key = None
    for i, t in enumerate(b["t"]):
        day0 = t - (t + 19800) % DAY
        k = day0 - ((t + 19800) // DAY + 3) % 7 * DAY if week else day0 + 33300 + (t - day0 - 33300) // n * n
        if k != key:
            key = k
            out["t"].append(day0 + 33300 if week else k)
            out["o"].append(b["o"][i])
            out["h"].append(b["h"][i])
            out["l"].append(b["l"][i])
            out["c"].append(b["c"][i])
            out["v"].append(b["v"][i])
            if week:
                out["tc"].append(day0 + 55800)
            continue
        out["h"][-1] = max(out["h"][-1], b["h"][i])
        out["l"][-1] = min(out["l"][-1], b["l"][i])
        out["c"][-1] = b["c"][i]
        out["v"][-1] += b["v"][i]
        if week:
            out["tc"][-1] = day0 + 55800
    out["tf_s"] = n
    return out


async def recent_bars(fy: str, tf_min: int, bars: int = 400, now: Optional[float] = None) -> dict[str, Any]:
    """The last `bars` COMPLETED bars straight from Fyers at the strategy's
    resolution (live runner). The still-forming bar is dropped — a strategy
    must never act on a candle that can still change. tf_min=1440 is daily,
    10080 weekly; 75 / 125 minutes are built from 15 / 5-minute candles."""
    now = int(now or time.time())
    res = fetch_res(tf_min)
    if res != tf_min:
        per = tf_min // res if tf_min != WEEK else 5
        src = await recent_bars(fy, res, bars=bars * per + per, now=now)
        out = resample(src, tf_min)
        if tf_min == WEEK:     # the newest finished session: is today a week's first?
            out["last_session"] = src["t"][-1] if src["t"] else None
        if tf_min == WEEK:     # a week is complete once its Friday session has closed
            done = lambda i: out["t"][i] - (out["t"][i] + 19800) % DAY \
                - ((out["t"][i] + 19800) // DAY + 3) % 7 * DAY + 4 * DAY + 55800 <= now  # noqa: E731
        else:                  # ... an intraday candle once its last minute has (15:30 at the latest)
            done = lambda i: min(out["t"][i] + tf_min * 60,  # noqa: E731
                                 out["t"][i] - (out["t"][i] + 19800) % DAY + 55800) <= now
        keep = [i for i in range(len(out["t"])) if done(i)][-bars:]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in out.items()}
    daily = tf_min == 1440
    tf_s = 86400 if daily else int(tf_min) * 60
    days = int(bars * 1.5) + 5 if daily else min(MAX_DAYS, max(5, math.ceil(bars * tf_min / 375 * 1.6) + 4))
    raw: list = []
    hi = now
    while True:            # daily history is served a year per call: a weekly warm-up needs more
        lo = max(now - days * DAY, hi - 365 * DAY)
        part = await _backend().get_history_range(fy, resolution="D" if daily else str(tf_min), from_ts=lo, to_ts=hi)
        if part is None:
            raise RuntimeError(f"Fyers history call failed for {fy}")
        raw = list(part) + raw
        if lo <= now - days * DAY or not part:
            break
        hi = lo
    if daily:      # Fyers stamps daily candles at the day's start; finished once the session closed
        done = lambda t: t - (t + 19800) % 86400 + 55800 <= now  # noqa: E731
    else:
        done = lambda t: t + tf_s <= now  # noqa: E731
    rows = [r for r in raw if isinstance(r, (list, tuple)) and len(r) >= 6 and done(int(r[0]))]
    return bars_from(rows[-bars:], tf_s)


# Live 1-minute history per symbol, topped up with only the new minutes.
# ponytail: process memory, one entry per watched symbol (~13k rows for a
# volume-candle strategy); a restart refetches once.
_m1_cache: dict[str, dict[str, Any]] = {}
_size_cache: dict[tuple, float] = {}


async def minute_history(fy: str, need: int, now: float) -> dict[str, Any]:
    """The last `need` COMPLETED 1-minute candles, incrementally cached."""
    have = _m1_cache.get(fy)
    # Full fetch only the first time, or when a strategy now wants a longer
    # window. NOT when the cache is short: a recently listed symbol has less
    # history than `need` forever, and refetching it every minute would hammer
    # Fyers for nothing.
    if have is None or need > have.get("need", 0):
        have = await recent_bars(fy, 1, bars=need, now=now)
        have["need"] = need
    else:
        raw = await _backend().get_history_range(fy, resolution="1", from_ts=have["t"][-1] + 60, to_ts=int(now))
        if raw is None:
            raise RuntimeError(f"Fyers history call failed for {fy}")
        rows = [r for r in raw if isinstance(r, (list, tuple)) and len(r) >= 6
                and int(r[0]) > have["t"][-1] and int(r[0]) + 60 <= now]
        new = bars_from(rows, 60)
        for k in ("t", "o", "h", "l", "c", "v"):
            have[k] = (have[k] + new[k])[-need:]
    _m1_cache[fy] = have
    return {k: list(v) if isinstance(v, list) else v for k, v in have.items()}


def last_closed(tf_min: int, now: float) -> int:
    """Start of the newest `tf_min` candle that has closed by `now` (candles
    from the 09:15 open; 1440 = the day's start once its session ended)."""
    day0 = int(now - (now + 19800) % DAY)
    if tf_min == WEEK:      # the Monday 00:00 of the newest week whose Friday session has closed
        mon = day0 - ((now + 19800) // DAY + 3) % 7 * DAY
        return mon if now >= mon + 4 * DAY + 55800 else mon - 7 * DAY
    if tf_min == 1440:
        return day0 if now >= day0 + 55800 else day0 - DAY
    k = int(now - day0 - 33300) // (tf_min * 60)
    return day0 + 33300 + (k - 1) * tf_min * 60


# Candles many strategy symbols read in the same pass (India VIX, an index a
# condition compares against): fetched once per closed candle, not once per
# symbol. ponytail: process memory, one entry per (symbol, timeframe, length).
_shared: dict[tuple, tuple[int, dict[str, Any]]] = {}


async def shared_bars(fy: str, tf_min: int, bars: int, now: float) -> dict[str, Any]:
    """recent_bars, reused until a newer candle closes. An intraday fetch that
    came back without the newest closed candle (Fyers hadn't published it
    yet) is not reused: the next call asks again."""
    key, stamp = (fy, tf_min, bars), last_closed(tf_min, now)
    hit = _shared.get(key)
    if hit is None or hit[0] != stamp or (tf_min < 1440 and (not hit[1]["t"] or hit[1]["t"][-1] < stamp)):
        hit = _shared[key] = (stamp, await recent_bars(fy, tf_min, bars=bars, now=now))
    return {k: list(v) if isinstance(v, list) else v for k, v in hit[1].items()}


async def live_bundle(spec: dict[str, Any], fy: str, now: float) -> dict[str, Any]:
    """Base + higher-timeframe bars and F&O meta for one symbol, for the runner."""
    from app.algo import engine, fno, orderflow

    wb = engine.warmup_bars(spec)
    keep = lambda tf, extra: engine.history_bars(spec, tf) + extra   # noqa: E731 — RVOL's whole sessions too
    if engine.needs_minutes(spec):
        btype, per_day = spec["bars"]["type"], spec["bars"]["per_day"]
        per_bar = spec["timeframe"] if btype == "time" else max(1, 375 // per_day)
        need = min(33000, max((wb + 60) * per_bar, engine.warmup_sessions(spec) * 375 + 60)
                   + (375 * 22 if btype != "time" else 0))
        m1 = await minute_history(fy, need, now)
        size = None
        if btype != "time":
            today = int(now - (now + 19800) % 86400)
            key = (fy, btype, per_day, today)
            if key not in _size_cache:          # fixed for the whole session
                _size_cache[key] = orderflow.bar_size(m1, btype, per_day, until=today)
            size = _size_cache[key]
        real = await asyncio.to_thread(real_flow, fy, m1["t"][0] if m1["t"] else int(now), int(now) + 60)
        d = orderflow.complete_only(orderflow.build_bars(m1, btype, spec["timeframe"], size, real), now)
    else:
        d = await recent_bars(fy, spec["timeframe"], bars=keep(spec["timeframe"], 60), now=now)
    as_base(d, spec["timeframe"])
    d["tf_min"] = spec["timeframe"]
    d["htf"] = {tf: await recent_bars(fy, tf, bars=keep(tf, 5), now=now) for tf in engine.cond_timeframes(spec)}
    xs = {}
    for ref, tfs in engine.ref_frames(spec).items():  # other symbols the conditions read, as the backtest loads them
        b = as_base(await shared_bars(ref, spec["timeframe"], keep(spec["timeframe"], 60), now), spec["timeframe"])
        b["tf_min"] = spec["timeframe"]
        b["htf"] = {tf: await shared_bars(ref, tf, keep(tf, 5), now) for tf in tfs - {spec["timeframe"]}}
        b["meta"] = {"symbol": ref}
        xs[ref] = b
    if xs:
        d["xs"] = xs
    if engine.uses_filings(spec) and d.get("t"):
        _attach_filings(d, await asyncio.to_thread(filing_rows, fy, d["t"][0] - 400 * 86400, int(now) + 60))
    for tf in engine.vix_timeframes(spec):         # India VIX beside the symbol, as the backtest loads it
        try:
            v = await shared_bars(VIX, tf, wb + (60 if tf == spec["timeframe"] else 5), now)
        except Exception as e:  # noqa: BLE001 — without VIX its conditions stay false, never guessed
            log.warning("algo.vix_unavailable", tf=tf, error=str(e)[:200])
            continue
        if tf == spec["timeframe"]:
            d["vix"] = as_base(v, tf)
        elif d["htf"].get(tf):
            d["htf"][tf]["vix"] = v
    name = fno.fno_name(fy)
    d["meta"] = {"symbol": fy, "name": name, "exch": fno.exchange_of(fy)}
    if spec["instrument"]["type"] != "equity":
        d["meta"]["lot"] = fno.lot_size(name)
    return d
