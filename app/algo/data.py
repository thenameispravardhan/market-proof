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
    """`tf_min`-minute OHLCV bars in [start_ts, end_ts), session minutes only.
    tf_min=1440 gives daily bars stamped at IST midnight."""
    src = candle_source(key)
    if not src:
        return None
    n = int(tf_min) * 60
    bucket = ("ts - ((ts + 19800) % 86400)" if tf_min == 1440
              else f"o_s + ((ts - o_s) // {n}) * {n}")
    rows = duckdb.connect().execute(f"""
        SELECT {bucket} AS b,
               arg_min(open, ts), max(high), min(low), arg_max(close, ts), sum(volume)
        FROM (SELECT DISTINCT ON (ts) ts, open, high, low, close, volume,
                     ts - ((ts + 19800) % 86400) + 33300 AS o_s
              FROM {src}
              WHERE ts >= ? AND ts < ? AND (ts + 19800) % 86400 BETWEEN 33300 AND 55799)
        GROUP BY b ORDER BY b""", [int(start_ts), int(end_ts)]).fetchall()
    return bars_from(rows, n)


def warmup_days(tf_min: int, bars: int) -> int:
    """Calendar days of history that hold `bars` bars of `tf_min` (375 session minutes a day)."""
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


async def assemble(spec: dict[str, Any], start: int, end: int) -> tuple[dict[str, dict], list[dict], int]:
    """Everything one backtest needs, per symbol: base bars from `start - warmup`,
    every higher timeframe the conditions use, India VIX when options are priced
    off it, and the F&O meta (name, lot, strike step). Returns (data, notes,
    trade_from). One failing symbol is a note, not an error."""
    from app.algo import engine, fno, orderflow

    base_tf = spec["timeframe"]
    htfs = sorted(engine.cond_timeframes(spec))
    wb = engine.warmup_bars(spec)
    minutes = engine.needs_minutes(spec)
    btype, per_day = spec["bars"]["type"], spec["bars"]["per_day"]
    base_lead = warmup_days(base_tf, wb) if btype == "time" else int(wb / per_day * 1.5) + 25   # +20 sessions to size bars
    lead = max([base_lead] + [warmup_days(tf, wb) for tf in htfs])
    lead = min(lead, 800)
    first = start - lead * DAY
    inst = spec["instrument"]
    if inst["type"] != "equity" or (spec.get("pair_leg") or {}).get("kind") == "FUT":   # lots of the pair leg's future
        await fno.ensure_master()
    vix = None
    notes: list[dict] = []
    wants_vix = inst["iv"]["source"] == "vix" or (
        inst["iv"]["source"] == "auto" and any(fno.fno_name(x) in fno.INDEX_LOTS for x in spec["symbols"]))
    vix_tfs = engine.vix_timeframes(spec)            # the VIX indicator, at these timeframes
    vix_htf: dict[int, dict] = {}
    if (inst["type"] == "option" and wants_vix) or vix_tfs:
        try:
            await ensure(VIX, first, end)
            vix = await asyncio.to_thread(load, store_key(VIX), base_tf, first, end)
            for tf in vix_tfs - {base_tf}:
                vix_htf[tf] = await asyncio.to_thread(load, store_key(VIX), tf, first, end)
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
        b = await asyncio.to_thread(load, info["key"], base_tf, first, end)
        if not b or not b["t"] or b["t"][-1] < start:
            raise ValueError(f"{ref} (read by the conditions): no candles in this range")
        b["tf_min"] = base_tf
        b["htf"] = {tf: await asyncio.to_thread(load, info["key"], tf, first, end) for tf in tfs - {base_tf}}
        b["meta"] = {"symbol": ref}
        xs[ref] = b
    out: dict[str, dict] = {}
    for fy in spec["symbols"]:
        try:
            info = await ensure(fy, first, end)
            if info["note"]:
                notes.append({"symbol": fy, "note": info["note"]})
            size = None
            if minutes:
                m1 = await asyncio.to_thread(load, info["key"], 1, first, end)
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
                del m1
            else:
                bars = await asyncio.to_thread(load, info["key"], base_tf, first, end)
            if not bars or not bars["t"] or bars["t"][-1] < start:
                notes.append({"symbol": fy, "note": "no candles in this range"})
                continue
            bars["tf_min"] = base_tf
            bars["htf"] = {tf: await asyncio.to_thread(load, info["key"], tf, first, end) for tf in htfs}
            if xs:
                bars["xs"] = xs
            if vix and vix["t"]:
                bars["vix"] = vix
            for tf, v in vix_htf.items():
                if v and v["t"] and bars["htf"].get(tf):
                    bars["htf"][tf]["vix"] = v
            name = fno.fno_name(fy)
            bars["meta"] = {"symbol": fy, "name": name, "exch": fno.exchange_of(fy), "bar_size": size}
            if inst["type"] != "equity":
                if not fno.is_fno(name):
                    notes.append({"symbol": fy, "note": f"{name} has no F&O contracts — skipped"})
                    continue
                bars["meta"]["lot"] = fno.lot_size(name)
                bars["meta"]["step"] = fno.default_step(name, bars["c"][-1])
            out[fy] = bars
        except Exception as e:  # noqa: BLE001
            notes.append({"symbol": fy, "note": str(e)[:200]})
    return out, notes, start


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
    must never act on a candle that can still change. tf_min=1440 is daily."""
    now = int(now or time.time())
    daily = tf_min == 1440
    tf_s = 86400 if daily else int(tf_min) * 60
    days = min(365, int(bars * 1.5) + 5) if daily else min(MAX_DAYS, max(5, math.ceil(bars * tf_min / 375 * 1.6) + 4))
    raw = await _backend().get_history_range(fy, resolution="D" if daily else str(tf_min),
                                             from_ts=now - days * DAY, to_ts=now)
    if raw is None:
        raise RuntimeError(f"Fyers history call failed for {fy}")
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
    if hit is None or hit[0] != stamp or (tf_min != 1440 and (not hit[1]["t"] or hit[1]["t"][-1] < stamp)):
        hit = _shared[key] = (stamp, await recent_bars(fy, tf_min, bars=bars, now=now))
    return {k: list(v) if isinstance(v, list) else v for k, v in hit[1].items()}


async def live_bundle(spec: dict[str, Any], fy: str, now: float) -> dict[str, Any]:
    """Base + higher-timeframe bars and F&O meta for one symbol, for the runner."""
    from app.algo import engine, fno, orderflow

    wb = engine.warmup_bars(spec)
    if engine.needs_minutes(spec):
        btype, per_day = spec["bars"]["type"], spec["bars"]["per_day"]
        per_bar = spec["timeframe"] if btype == "time" else max(1, 375 // per_day)
        need = min(33000, (wb + 60) * per_bar + (375 * 22 if btype != "time" else 0))
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
        d = await recent_bars(fy, spec["timeframe"], bars=wb + 60, now=now)
    d["tf_min"] = spec["timeframe"]
    d["htf"] = {tf: await recent_bars(fy, tf, bars=wb + 5, now=now) for tf in engine.cond_timeframes(spec)}
    xs = {}
    for ref, tfs in engine.ref_frames(spec).items():  # other symbols the conditions read, as the backtest loads them
        b = await shared_bars(ref, spec["timeframe"], wb + 60, now)
        b["tf_min"] = spec["timeframe"]
        b["htf"] = {tf: await shared_bars(ref, tf, wb + 5, now) for tf in tfs - {spec["timeframe"]}}
        b["meta"] = {"symbol": ref}
        xs[ref] = b
    if xs:
        d["xs"] = xs
    for tf in engine.vix_timeframes(spec):         # India VIX beside the symbol, as the backtest loads it
        try:
            v = await shared_bars(VIX, tf, wb + (60 if tf == spec["timeframe"] else 5), now)
        except Exception as e:  # noqa: BLE001 — without VIX its conditions stay false, never guessed
            log.warning("algo.vix_unavailable", tf=tf, error=str(e)[:200])
            continue
        if tf == spec["timeframe"]:
            d["vix"] = v
        elif d["htf"].get(tf):
            d["htf"][tf]["vix"] = v
    name = fno.fno_name(fy)
    d["meta"] = {"symbol": fy, "name": name, "exch": fno.exchange_of(fy)}
    if spec["instrument"]["type"] != "equity":
        d["meta"]["lot"] = fno.lot_size(name)
    return d
