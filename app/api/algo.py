"""`/api/algo` — indicator strategies: backtest, optimise, automate, data.

    GET    /api/algo/indicators            catalog for the builder (indicators, operators, defaults)
    GET    /api/algo/guide                 the Algo Lab guide (markdown text): give it to an AI to write a strategy
    POST   /api/algo/backtest              {spec, start, end, chart_symbol?}
    POST   /api/algo/payoff               {spec, side?, spot?, iv_pct?} — payoff, Greeks, funds required
    POST   /api/algo/optimize              {spec, start, end, grid:[{path, values}], metric, min_trades,
                                            walk_forward?: {windows, is_mult, anchored}}
    GET    /api/algo/data?symbol=          what the candle store holds for a symbol
    POST   /api/algo/data/download         {symbols, days} — fill the store from Fyers
    GET    /api/algo/strategies            saved strategies + live P&L
    POST   /api/algo/strategies            {name, spec}            (created OFF, paper)
    PUT    /api/algo/strategies/{id}       {name?, spec?, enabled?, mode?, account_id?, confirm?}
    DELETE /api/algo/strategies/{id}
    POST   /api/algo/strategies/{id}/squareoff
    GET    /api/algo/trades?strategy_id=&strategy_ids=1,2&limit=
    GET    /api/algo/status                runner heartbeat, events, open positions
    POST   /api/algo/sync                  download today's candles for every strategy symbol now

Switching a strategy to LIVE needs `confirm: "LIVE"` typed in the UI and a real,
logged-in Fyers account — the same typed-confirm rule as the global LIVE mode.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.algo import data, engine
from app.algo import indicators as ind
from app.db.init import init_db
from app.db.models import AlgoStrategy, AlgoStrategyVersion, AlgoTrade, AuditLog, BrokerAccount
from app.db.session import get_db
from app.logging_config import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/algo", tags=["algo"])
init_db()

DAY = 86400
MAX_RANGE_DAYS = 3 * 366
LIVE_SQUARE_OFF_LATEST = "15:15"   # Fyers auto-squares MIS at ~15:20; be out before it


def _spec(raw: Any) -> dict[str, Any]:
    try:
        s = engine.normalize(raw if isinstance(raw, dict) else {})
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(422, detail=str(e))
    s["symbols"] = list(dict.fromkeys(data.fyers_symbol(x) for x in s["symbols"]))
    for o in engine._walk_operands(s):             # another symbol a condition reads: same resolution
        if o.get("symbol"):
            o["symbol"] = data.fyers_symbol(o["symbol"])
    return s


def _range(body: dict[str, Any]) -> tuple[int, int]:
    try:
        start, end = date.fromisoformat(str(body["start"])), date.fromisoformat(str(body["end"]))
    except (KeyError, ValueError) as e:
        raise HTTPException(422, detail=f"start/end must be YYYY-MM-DD ({e})")
    if end < start:
        raise HTTPException(422, detail="end is before start")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(422, detail=f"at most {MAX_RANGE_DAYS} days per backtest")
    ist0 = lambda d: int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()) - engine.IST  # noqa: E731
    return ist0(start), min(ist0(end) + DAY, int(time.time()) + 60)


# The backtester shares a 2 GB box (MemoryMax 1.4G) with the live trading
# process. ~140k bars (4 symbols x 2y x 5m) measured ~350 MB peak, so a cap of
# 600k bars keeps one job well inside the budget, and the semaphore makes it
# ONE job at a time — two big backtests in parallel would double the peak and
# could get the trading process OOM-killed mid-session.
MAX_BARS = 600_000
MAX_BAR_RUNS = 30_000_000          # optimiser: combos x bars (~4 minutes of CPU)
_heavy = asyncio.Semaphore(1)


def _estimate_bars(spec: dict[str, Any], start: int, end: int) -> int:
    """Candles held in memory at the PEAK of one run. Every symbol's base and
    higher-timeframe candles stay for the whole run; 1-minute data (order flow,
    volume / turnover candles) is loaded ONE symbol at a time, folded into base
    candles and freed (data.assemble), so it counts once, not per symbol."""
    days = (end - start) / 86400 * 5 / 7 + 30
    btype = spec["bars"]["type"]
    base = spec["bars"]["per_day"] if btype != "time" else 375 / spec["timeframe"]
    htf = sum(1 if tf == 1440 else 375 / tf for tf in engine.cond_timeframes(spec))
    held = len(spec["symbols"]) * days * (base + htf)
    for tfs in engine.ref_frames(spec).values():   # other symbols' candles: time candles, loaded once
        held += days * sum(1 if tf == 1440 else 375 / tf for tf in tfs | {spec["timeframe"]})
    transient = days * 375 if engine.needs_minutes(spec) else 0
    return int(held + transient)


def _guard(spec: dict[str, Any], start: int, end: int, combos: int = 1) -> int:
    est = _estimate_bars(spec, start, end)
    if est > MAX_BARS:
        raise HTTPException(422, detail=(
            f"too much data for one run: ~{est:,} candles ({len(spec['symbols'])} symbols x "
            f"{(end - start) // 86400} days at {spec['timeframe']}m). Keep it under {MAX_BARS:,} — "
            "shorten the range, use a larger timeframe or fewer symbols."))
    if est * combos > MAX_BAR_RUNS:
        raise HTTPException(422, detail=(
            f"optimisation too heavy: {combos} combinations x ~{est:,} candles. Narrow the grid "
            f"or the date range (limit {MAX_BAR_RUNS:,} candle-runs)."))
    return est


def _bump_trials(body: dict[str, Any], n: int) -> int:
    """Tries on this research line so far, `n` more included. A saved
    strategy's count lives on it (each backtest +1, each optimiser
    combination +1); an unsaved draft sends its own `trials`. The Deflated
    Sharpe deflates by all of them, not just the last sweep."""
    from app.db import session as dbs

    sid = body.get("strategy_id")
    if sid:
        with dbs.SessionLocal() as db:
            db.execute(update(AlgoStrategy).where(AlgoStrategy.id == int(sid))       # not an edit: updated_at stays
                       .values(trials=AlgoStrategy.trials + n, updated_at=AlgoStrategy.updated_at))
            db.commit()
            got = db.execute(select(AlgoStrategy.trials).where(AlgoStrategy.id == int(sid))).scalar_one_or_none()
        if got is not None:
            return got
    try:
        return max(0, int(body.get("trials") or 0)) + n
    except (TypeError, ValueError):
        return n


def _oos_pct(body: dict[str, Any]) -> float:
    try:
        pct = float(body.get("oos_pct") or 0)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="out-of-sample % must be a number") from None
    if pct and not 5 <= pct <= 80:
        raise HTTPException(422, detail="out-of-sample % must be between 5 and 80")
    return pct


def _oos_from(bars: dict[str, dict], trade_from: int, pct: float) -> Optional[int]:
    """Hold out the LAST pct % of the data actually loaded — not of the
    requested range, which may run past the newest candle (a split in the
    empty tail would leave the out-of-sample side with nothing)."""
    if not pct:
        return None
    last = max(v["t"][-1] for v in bars.values())
    return int(last - (last - trade_from) * pct / 100)


def _real_share(d: dict) -> float:
    """% of the order-flow volume that came from recorded ticks (rest is BVC)."""
    tot = sum(d["v"]) or 0
    return round(sum(v * p / 100 for v, p in zip(d["v"], d.get("real_pct") or [])) / tot * 100, 1) if tot else 0.0


def _overlays(spec: dict[str, Any], d: dict, cache: dict, lo: int) -> list[dict[str, Any]]:
    """The strategy's own indicator lines for the chart symbol — what it
    actually sees — so the results chart can draw them. Each is marked to sit
    on the price scale or in its own pane by whether its values live in the
    price range."""
    seen, out = set(), []
    pmin, pmax = min(d["l"][lo:] or [0]), max(d["h"][lo:] or [0])
    for op in engine._walk_operands(spec):
        if op["ind"] in ("PRICE", "TIME", "CANDLE", "DTE") and not op.get("symbol") or len(out) >= 8:
            continue
        key = (op["ind"], op.get("field"), tuple(sorted((op.get("params") or {}).items())), op.get("tf"), op.get("mult"), op.get("add"),
               op.get("symbol"))
        if key in seen:
            continue
        seen.add(key)
        try:
            vals = engine.series(d, op, cache)[lo:]
        except (ValueError, KeyError):
            continue
        nums = [v for v in vals if v is not None]
        if not nums:
            continue
        on_price = pmin * 0.8 <= min(nums) and max(nums) <= pmax * 1.2
        label = op["ind"] + (f".{op['field']}" if op.get("field") else "") +             (f"({','.join(str(v) for v in (op.get('params') or {}).values())})" if op.get("params") else "") +             (f" {op['tf']}m" if op.get("tf") else "") + (f" of {op['symbol']}" if op.get("symbol") else "")
        out.append({"label": label, "price": on_price, "values": [None if v is None else round(v, 4) for v in vals]})
    return out


async def _assemble(spec: dict[str, Any], start: int, end: int) -> tuple[dict[str, dict], list[dict], int]:
    try:
        bars, notes, trade_from = await data.assemble(spec, start, end)
    except ValueError as e:                        # e.g. another symbol a condition reads has no candles
        raise HTTPException(422, detail=str(e)) from None
    if not bars:
        raise HTTPException(422, detail="; ".join(f"{n['symbol']}: {n['note']}" for n in notes) or "no data")
    return bars, notes, trade_from


@router.get("/indicators")
def indicators() -> dict[str, Any]:
    return {"indicators": ind.catalog(), "operators": engine.OPS, "timeframes": engine.TIMEFRAMES,
            "cond_timeframes": engine.COND_TIMEFRAMES, "sources": ind.SOURCES, "metrics": engine.METRICS,
            "sizing": engine.SIZING, "defaults": engine.DEFAULT_SPEC, "max_range_days": MAX_RANGE_DAYS,
            "position_metrics": engine.POSITION_METRICS, "no_symbol": sorted(engine.NO_SYMBOL)}


@router.get("/guide")
def guide() -> PlainTextResponse:
    """Everything the builder and backtest can do, plus a generated reference
    of every indicator and allowed value — a text an AI can turn into
    importable strategy JSON."""
    from app.algo import guide as g

    return PlainTextResponse(g.text(), media_type="text/markdown; charset=utf-8")


@router.post("/validate")
def validate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Check a spec without running it: the normalized spec, or a 422 saying what is wrong."""
    return {"ok": True, "spec": _spec(body.get("spec"))}


@router.post("/backtest")
async def backtest(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    spec = _spec(body.get("spec"))
    start, end = _range(body)
    _guard(spec, start, end)
    pct = _oos_pct(body)
    async with _heavy:
        t0 = time.monotonic()
        bars, notes, trade_from = await _assemble(spec, start, end)
        oos_from = _oos_from(bars, trade_from, pct)
        trials = await asyncio.to_thread(_bump_trials, body, 1)
        try:
            caches: dict[str, dict] = {}
            res = await asyncio.to_thread(engine.run, spec, bars, caches, trade_from, oos_from)
            res["monte_carlo"] = await asyncio.to_thread(engine.monte_carlo, res["trades"], spec["portfolio"]["capital"])
            res["trials"] = trials
            res["stats"]["deflated_sharpe"] = await asyncio.to_thread(
                engine.trial_dsr, res["trades"], bars, spec["portfolio"]["capital"], trials, trade_from)
        except (ValueError, KeyError, ZeroDivisionError) as e:
            raise HTTPException(422, detail=f"backtest: {e}")
    chart_sym = body.get("chart_symbol") if body.get("chart_symbol") in bars else next(iter(bars))
    d = bars[chart_sym]
    keep = 8000                         # enough to inspect, small enough to ship
    first = next((i for i, t in enumerate(d["t"]) if t >= trade_from), 0)
    lo = max(first, len(d["t"]) - keep)
    flow = d.get("delta") if d.get("flow") else None
    candles = [[d["t"][i], d["o"][i], d["h"][i], d["l"][i], d["c"][i], d["v"][i], flow[i] if flow else None]
               for i in range(lo, len(d["t"]))]
    res["trades_total"] = len(res["trades"])
    res["trades"] = res["trades"][-3000:]
    return {**res, "spec": spec, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2),
            "chart": {"symbol": chart_sym, "candles": candles,
                      "trades": [x for x in res["trades"] if x["symbol"] == chart_sym],
                      "overlays": _overlays(spec, d, caches.get(chart_sym, {}), lo)},
            "flow_source": {k: _real_share(v) for k, v in bars.items() if v.get("flow")},
            "bars": {k: sum(1 for t in v["t"] if t >= trade_from) for k, v in bars.items()}}


@router.post("/optimize")
async def optimize(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    spec = _spec(body.get("spec"))
    start, end = _range(body)
    grid = body.get("grid") or []
    combos = 1
    for g in grid:
        combos *= max(1, len(g.get("values") or []))
    _guard(spec, start, end, combos)
    pct = _oos_pct(body)
    wf = body.get("walk_forward")         # {windows, is_mult, anchored}: rolling walk-forward instead of one split
    async with _heavy:
        t0 = time.monotonic()
        bars, notes, trade_from = await _assemble(spec, start, end)
        oos_from = None if wf else _oos_from(bars, trade_from, pct)
        trials = await asyncio.to_thread(_bump_trials, body, combos)
        try:
            metric, min_trades = str(body.get("metric") or "net_pnl"), int(body.get("min_trades") or 5)
            if wf:
                res = await asyncio.to_thread(engine.walk_forward, spec, bars, grid, metric, min_trades, trade_from,
                                              int(wf.get("windows") or 4), float(wf.get("is_mult") or 3),
                                              bool(wf.get("anchored")))
            else:
                res = await asyncio.to_thread(engine.optimize, spec, bars, grid, metric, min_trades,
                                              trade_from, oos_from, trials - combos)
        except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError, AttributeError) as e:
            raise HTTPException(422, detail=f"optimize: {e}")
    return {**res, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2), "oos_from": oos_from, "trials": trials}


@router.get("/instrument")
async def instrument_info(symbol: str) -> dict[str, Any]:
    """What the builder needs to know about an underlying: F&O or not, lot
    size, strike step, weekly expiries, and its listed futures."""
    from app.algo import fno

    fy = data.fyers_symbol(symbol)
    try:
        await fno.ensure_master()
    except Exception as e:  # noqa: BLE001
        log.warning("algo.fno_master_unavailable", error=str(e)[:200])
    name = fno.fno_name(fy)
    is_fno = fno.is_fno(name)
    return {"symbol": fy, "name": name, "fno": is_fno, "lot": fno.lot_size(name) if is_fno else None,
            "weekly": name in fno.WEEKLY,
            "futures": [{"symbol": s_, "expiry": e} for e, s_ in fno.master()["futures"].get(name, [])
                        if e >= time.time()][:3],
            "coverage": data.coverage(data.store_key(fy))}


def _last_closes(fy: str, days: int = 40) -> list[float]:
    """Daily closes the candle store holds for a symbol, oldest first (empty when none)."""
    cov = data.coverage(data.store_key(fy))
    if not cov:
        return []
    d = data.load(data.store_key(fy), 1440, int(cov["last"]) - days * DAY, int(cov["last"]) + DAY)
    return list(d["c"]) if d else []


@router.post("/payoff")
async def payoff(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Payoff curves, Greeks, breakevens and funds required for ONE set of the
    builder's legs on its first symbol: {spec, side?: BUY|SELL, spot?, iv_pct?}.
    Spot defaults to the last stored close, IV to India VIX (indices) or 20-day
    realised vol — the backtest's own pricing inputs."""
    import math

    from app.algo import fno
    from app.algo import payoff as po

    raw = dict(body.get("spec") or {})
    if not raw.get("symbols"):
        raise HTTPException(422, detail="add a symbol first")
    # Only the instrument / margin / portfolio matter here: a builder with no
    # conditions or stops yet still gets its preview.
    dummy = {"logic": "AND", "conditions": [{"left": {"value": 1}, "op": ">", "right": {"value": 0}}]}
    stub = {k: raw[k] for k in ("symbols", "instrument", "portfolio", "margin", "position_rules") if k in raw}
    try:
        spec = engine.normalize({**stub, "direction": "both", "entry_long": dummy, "entry_short": dummy,
                                 "stop_loss": None, "target": None, "sizing": {"mode": "lots", "value": 1}})
    except (ValueError, TypeError, KeyError) as e:
        raise HTTPException(422, detail=str(e))
    side = "SELL" if str(body.get("side") or "BUY").upper() == "SELL" else "BUY"
    fy = data.fyers_symbol(spec["symbols"][0])
    name = fno.fno_name(fy)
    try:
        await fno.ensure_master()
    except Exception as e:  # noqa: BLE001 — fallback lots still give a preview
        log.warning("algo.fno_master_unavailable", error=str(e)[:200])
    closes = await asyncio.to_thread(_last_closes, fy)
    try:
        spot = float(body["spot"]) if body.get("spot") else (closes[-1] if closes else None)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="spot must be a number") from None
    if not spot or spot <= 0:
        raise HTTPException(422, detail=f"no candles stored for {fy} — download its data or type a spot price")
    iv_src, iv = "input", None
    if body.get("iv_pct"):
        try:
            iv = float(body["iv_pct"]) / 100
        except (TypeError, ValueError):
            raise HTTPException(422, detail="IV % must be a number") from None
    elif spec["instrument"]["iv"]["source"] == "fixed":
        iv, iv_src = spec["instrument"]["iv"]["value"] / 100, "fixed"
    else:
        if name in fno.INDEX_LOTS and spec["instrument"]["iv"]["source"] in ("auto", "vix"):
            vix = await asyncio.to_thread(_last_closes, data.VIX, 10)
            if vix:
                iv, iv_src = vix[-1] / 100, "India VIX"
        if iv is None and len(closes) > 6:
            rets = [math.log(b / a) for a, b in zip(closes, closes[1:])][-20:]
            m = sum(rets) / len(rets)
            iv = max(0.05, math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(252))
            iv_src = "20-day realised vol"
        if iv is None:
            iv, iv_src = (0.15 if name in fno.INDEX_LOTS else 0.30), "default"
    if not 0.01 <= iv <= 5:
        raise HTTPException(422, detail="IV must be 1-500%")
    meta = {"symbol": fy, "name": name, "exch": fno.exchange_of(fy), "lot": fno.lot_size(name)}
    try:
        res = await asyncio.to_thread(po.preview, spec, side, spot, time.time(), iv, meta)
    except (ValueError, KeyError, ZeroDivisionError) as e:
        raise HTTPException(422, detail=f"payoff: {e}")
    return {**res, "symbol": fy, "iv_source": iv_src, "spot_source": "input" if body.get("spot") else "last close"}


# ---- real order flow (app/algo/ticks.py, recorded by app/algo/flowd.py) -------

def _flow_status(q: str = "", top: int = 50) -> dict[str, Any]:
    from app.algo import flow_universe as fu
    from app.algo import ticks

    cfg = ticks.load_config()
    col = ticks.read_json(ticks.ROOT / "status.json") or {}
    alive = bool(col) and col.get("state") == "running" and time.time() - float(col.get("heartbeat") or 0) < 30
    sup = None
    try:
        from app.main import app as _app

        s_ = getattr(_app.state, "flow_supervisor", None)
        sup = s_.info() if s_ is not None else None
    except Exception:  # noqa: BLE001
        sup = None
    stats = ticks.read_json(ticks.ROOT / "stats.json") or {}
    rows = [[k, *v] for k, v in stats.items() if not q or q.upper() in k]
    rows.sort(key=lambda r: -(r[3] + r[4] + r[5]))           # traded volume today, busiest first
    days = ticks.store().collected_days()
    return {
        "enabled": bool(cfg.get("enabled")), "config": cfg, "config_symbols": cfg.get("symbols", []),
        "strategy_symbols": ticks.strategy_symbols(), "collector": {**col, "alive": alive, "supervisor": sup},
        "days": {"count": len(days), "first": days[0] if days else None, "last": days[-1] if days else None,
                 "retention": int(cfg.get("retention_days") or ticks.RETENTION_DAYS)},
        "disk_mb": ticks.disk_mb(), "lists": {k: v["label"] for k, v in fu.LISTS.items()},
        "top": rows[:max(1, min(500, top))], "keys_today": len(stats),
    }


@router.get("/ticks/status")
def ticks_status(q: str = "", top: int = 50) -> dict[str, Any]:
    """The order-flow collector: config, its process's health and load, the
    symbol plan by class, the stored days (a sliding window) and today's
    busiest keys (`q` filters them by name)."""
    return _flow_status(q, top)


@router.put("/ticks/config")
def ticks_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Change what is recorded. Any of: enabled, symbols (hand-picked; "NIFTY:FUT"
    = the current NIFTY future, rolling by itself), retention_days (1-365,
    the sliding window), raw_days (0-30, every frame of the hand-picked
    symbols), budget (50-5000 symbols on the socket), min_free_gb (0.5-1000:
    the disk guard drops old days early, then pauses) and universe {lists,
    stocks, index_futures, stock_futures, next_futures, index_options,
    index_strikes, index_next_strikes, stock_options, stock_strikes,
    stock_next_expiry, roll_days, enabled}."""
    from app.algo import flow_universe as fu
    from app.algo import ticks

    patch: dict[str, Any] = {}
    if "enabled" in body:
        patch["enabled"] = bool(body["enabled"])
    if "symbols" in body:
        syms = body.get("symbols") or []
        if not isinstance(syms, list) or len(syms) > ticks.MAX_FOCUS:
            raise HTTPException(422, detail=f"symbols must be a list of at most {ticks.MAX_FOCUS}")
        patch["symbols"] = [str(x) for x in syms]
    for k, lo, hi in (("retention_days", 1, 365), ("raw_days", 0, 30), ("budget", 50, 5000)):
        if body.get(k) is not None:
            v = body[k]
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise HTTPException(422, detail=f"{k} must be {lo}-{hi}")
            patch[k] = v
    if body.get("min_free_gb") is not None:
        v = body["min_free_gb"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0.5 <= v <= 1000:
            raise HTTPException(422, detail="min_free_gb must be 0.5-1000")
        patch["min_free_gb"] = float(v)
    uni = body.get("universe")
    if uni is not None:
        if not isinstance(uni, dict):
            raise HTTPException(422, detail="universe must be an object")
        clean: dict[str, Any] = {}
        for k, v in uni.items():
            if k not in ticks.DEFAULT_UNIVERSE:
                raise HTTPException(422, detail=f"unknown universe setting {k!r}")
            if k == "lists":
                if not isinstance(v, list) or any(x not in fu.LISTS for x in v):
                    raise HTTPException(422, detail=f"lists are some of {sorted(fu.LISTS)}")
            elif k in ("next_futures",):
                if v not in ("always", "roll", "off"):
                    raise HTTPException(422, detail="next_futures is always, roll or off")
            elif k in ("stock_next_expiry",):
                if v not in ("roll", "off"):
                    raise HTTPException(422, detail="stock_next_expiry is roll or off")
            elif k in ("index_strikes", "index_next_strikes", "stock_strikes", "roll_days"):
                if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 50:
                    raise HTTPException(422, detail=f"{k} must be 0-50")
            elif not isinstance(v, bool):
                raise HTTPException(422, detail=f"{k} must be true or false")
            clean[k] = v
        patch["universe"] = clean
    ticks.save_config(patch)
    return _flow_status()


@router.get("/ticks/plan")
def ticks_plan(cls: str = "", q: str = "", limit: int = Query(300, ge=1, le=5000)) -> dict[str, Any]:
    """The symbols the collector records now ([symbol, key, class]), by class or name."""
    from app.algo import ticks

    plan = ticks.read_json(ticks.ROOT / "plan.json") or {}
    rows = [r for r in plan.get("symbols") or [] if (not cls or r[2] == cls) and (not q or q.upper() in r[0])]
    return {"at": plan.get("at"), "classes": plan.get("classes"), "total": len(rows), "symbols": rows[:limit]}


@router.get("/ticks/flow")
async def ticks_flow(symbol: str, resolution: str = "5", from_ts: int = Query(..., alias="from"),
                     to_ts: int = Query(..., alias="to"), footprint: bool = False, prints: bool = False) -> dict[str, Any]:
    """REAL order flow per chart candle: buy / sell volume classified from
    recorded ticks. Candles align with /api/market/history (09:15 buckets,
    D = session). Each bar: [t, buy, sell, delta, trades, unk, oi, close,
    delta_low, delta_high] — unk = volume with no aggressor (a feed gap, the
    auction), oi = open interest at the bar's end (F&O), close = its last
    trade (an index's bars are its future's: the chart shifts by the basis),
    delta_low / high = the lowest / highest running delta inside the bar.
    footprint=true adds [t, price, buy, sell]; prints=true the big trades
    [ts, price, qty, side]. Crypto: Binance's taker-buy volume per candle."""
    from app.services import crypto

    if resolution != "D" and not resolution.isdigit():
        raise HTTPException(422, detail="resolution is minutes or D")
    if crypto.is_crypto(symbol):
        try:
            rows = await crypto.klines(symbol.upper(), resolution, from_ts, to_ts)
        except ValueError as e:
            raise HTTPException(422, detail=str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, detail=f"Binance unavailable: {e!s}"[:200]) from None
        out = {"symbol": symbol, "key": symbol.upper(), "recorded": bool(rows),
               "bars": [[int(t), round(tb, 6), round(v - tb, 6), round(2 * tb - v, 6)] for t, *_, v, tb in rows]}
        return {**out, "footprint": [], "prints": []} if footprint or prints else out
    return await asyncio.to_thread(_recorded_flow, symbol, resolution, from_ts, to_ts, footprint, prints)


FOOTPRINT_MAX_ROWS = 250_000


def _recorded_flow(symbol: str, resolution: str, from_ts: int, to_ts: int, footprint: bool,
                   prints: bool = False) -> dict[str, Any]:
    from app.algo import ticks

    span = 86400 if resolution == "D" else int(resolution) * 60
    st = ticks.store()
    fy = data.fyers_symbol(symbol)

    def bucket(t: int) -> int:
        day0 = t - (t + 19800) % 86400
        if span == 86400:
            return day0
        o_s = day0 + 33300
        return o_s + ((t - o_s) // span) * span

    # The key that held this symbol each day (a future was <NAME>-FUT2 before it became <NAME>-FUT).
    spans: list[tuple[str, int, int]] = []
    for day, key in sorted(st.keys_for(fy, from_ts, to_ts).items()):
        a, b = max(from_ts, day * 86400 - 19800), min(to_ts, (day + 1) * 86400 - 19800)
        if spans and spans[-1][0] == key and spans[-1][2] == a:
            spans[-1] = (key, spans[-1][1], b)
        else:
            spans.append((key, a, b))
    bars: dict[int, list] = {}
    for key, a, b in spans:
        for r in st.flow_rows(key, a, b):
            t, buy, sell, unk, trades, _, _, _, _, c, _, _, oi, _ = r
            buy, sell = buy or 0.0, sell or 0.0
            x = bars.setdefault(bucket(int(t)), [0.0, 0.0, 0.0, 0, None, None, 0.0, 0.0, 0.0])
            x[0] += buy
            x[1] += sell
            x[2] += unk or 0.0
            x[3] += int(trades or 0)
            x[4] = oi if oi is not None else x[4]
            x[5] = c if c is not None else x[5]
            x[6] += buy - sell                                     # running delta inside the bar
            x[7], x[8] = min(x[7], x[6]), max(x[8], x[6])
    key_last = spans[-1][0] if spans else ticks.flow_key(fy)
    out: dict[str, Any] = {
        "symbol": symbol, "key": key_last, "recorded": bool(bars),
        "bars": [[t, round(v[0], 2), round(v[1], 2), round(v[0] - v[1], 2), v[3], round(v[2], 2), v[4], v[5],
                  round(v[7], 2), round(v[8], 2)] for t, v in sorted(bars.items())],
    }
    if footprint:
        fp: dict[tuple[int, float], list[float]] = {}
        for key, a, b in spans:
            for t, price, b_, s_ in st.footprint(key, a, b):
                f = fp.setdefault((bucket(int(t)), price), [0.0, 0.0])
                f[0] += b_
                f[1] += s_
        rows = [[t, p, round(b_, 2), round(s_, 2)] for (t, p), (b_, s_) in sorted(fp.items())]
        out["footprint"] = rows[-FOOTPRINT_MAX_ROWS:]
        out["footprint_truncated"] = len(rows) > FOOTPRINT_MAX_ROWS
    if prints:
        out["prints"] = [[int(t), p, q, int(sd)] for key, a, b in spans for t, p, q, sd in st.prints(key, a, b)]
    return out


@router.get("/data")
def data_coverage(symbol: str) -> dict[str, Any]:
    fy = data.fyers_symbol(symbol)
    return {"symbol": fy, "key": data.store_key(fy), "coverage": data.coverage(data.store_key(fy))}


@router.post("/data/download")
async def data_download(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    syms = [data.fyers_symbol(s) for s in (body.get("symbols") or []) if str(s).strip()]
    days = int(body.get("days") or 365)
    if not syms or len(syms) > 50:
        raise HTTPException(422, detail="1-50 symbols")
    if not 1 <= days <= MAX_RANGE_DAYS:
        raise HTTPException(422, detail=f"days must be 1-{MAX_RANGE_DAYS}")
    now = int(time.time())
    out = []
    for fy in syms:
        try:
            out.append(await data.ensure(fy, now - days * DAY, now))
        except Exception as e:  # noqa: BLE001
            out.append({"symbol": fy, "note": str(e)[:200], "coverage": None})
    return {"results": out}


# ---- saved strategies ------------------------------------------------------

def _ser(s: AlgoStrategy, db: Session) -> dict[str, Any]:
    agg = db.execute(select(func.count(), func.coalesce(func.sum(AlgoTrade.net_pnl), 0.0)).where(
        AlgoTrade.strategy_id == s.id, AlgoTrade.status == "closed")).one()
    open_n = db.execute(select(func.count()).select_from(AlgoTrade).where(
        AlgoTrade.strategy_id == s.id, AlgoTrade.status == "open")).scalar_one()
    versions = db.execute(select(func.max(AlgoStrategyVersion.version)).where(
        AlgoStrategyVersion.strategy_id == s.id)).scalar_one() or 1
    return {"id": s.id, "name": s.name, "spec": s.spec, "enabled": s.enabled, "mode": s.mode,
            "account_id": s.account_id, "closed_trades": agg[0], "realized_pnl": round(agg[1], 2),
            "open_positions": open_n, "version": s.version or 1, "versions": versions, "trials": s.trials or 0,
            "shadow_of": s.shadow_of,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "updated_at": s.updated_at.isoformat() if s.updated_at else None}


def _audit(db: Session, action: str, s: AlgoStrategy, after: dict) -> None:
    db.add(AuditLog(actor="ui", action=action, target=f"algo_strategy:{s.id}", before=None, after=after))


def _get(db: Session, sid: int) -> AlgoStrategy:
    s = db.get(AlgoStrategy, sid)
    if s is None:
        raise HTTPException(404, detail=f"strategy {sid} not found")
    return s


@router.get("/strategies")
def list_strategies(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(select(AlgoStrategy).order_by(AlgoStrategy.id)).scalars().all()
    return {"strategies": [_ser(s, db) for s in rows]}


@router.post("/strategies", status_code=201)
def create_strategy(body: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(422, detail="name is required (max 128 chars)")
    if db.execute(select(AlgoStrategy).where(AlgoStrategy.name == name)).scalar_one_or_none():
        raise HTTPException(409, detail=f"a strategy named {name!r} already exists")
    s = AlgoStrategy(name=name, spec=_spec(body.get("spec")), enabled=False, mode="paper", version=1,
                     trials=_bump_trials({"trials": body.get("trials")}, 0))     # a draft's tries come with it
    db.add(s)
    db.flush()
    db.add(AlgoStrategyVersion(strategy_id=s.id, version=1, spec=s.spec, note=body.get("note")))
    _audit(db, "algo.create", s, {"name": name})
    db.commit()
    return _ser(s, db)


@router.put("/strategies/{sid}")
def update_strategy(sid: int, body: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _get(db, sid)
    changes: dict[str, Any] = {}
    if body.get("name") is not None:
        name = str(body["name"]).strip()
        clash = db.execute(select(AlgoStrategy).where(AlgoStrategy.name == name, AlgoStrategy.id != sid)).scalar_one_or_none()
        if not name or clash:
            raise HTTPException(409 if clash else 422, detail="name taken" if clash else "name is required")
        s.name = changes["name"] = name
    if body.get("spec") is not None:
        new = _spec(body["spec"])
        if new != s.spec:
            changes["version"] = _new_version(db, s, new, body.get("note"))
    if "account_id" in body:
        s.account_id = int(body["account_id"]) if body["account_id"] else None
        changes["account_id"] = s.account_id
    if body.get("mode") is not None:
        mode = str(body["mode"])
        if mode not in ("paper", "live"):
            raise HTTPException(422, detail="mode is paper or live")
        if mode == "live" and s.shadow_of:
            raise HTTPException(422, detail="a shadow only paper-trades — promote it to put its rules on the live strategy")
        if mode == "live" and s.mode != "live" and body.get("confirm") != "LIVE":
            raise HTTPException(422, detail='type LIVE to confirm real-money automation')
        s.mode = changes["mode"] = mode
    if body.get("enabled") is not None:
        s.enabled = changes["enabled"] = bool(body["enabled"])
    if s.mode == "live":
        acc = db.get(BrokerAccount, s.account_id) if s.account_id else None
        if acc is None or acc.paper_mode or acc.broker != "fyers":
            raise HTTPException(422, detail="live mode needs a real Fyers account selected")
        spec = engine.normalize(s.spec)
        # Off = carry forward (NRML / CNC orders); a set time must beat Fyers' MIS square-off.
        if engine._hhmm(LIVE_SQUARE_OFF_LATEST) < engine.session_window(spec)[2] < engine.CARRY:
            raise HTTPException(422, detail=f"live square-off must be at or before {LIVE_SQUARE_OFF_LATEST} "
                                            "(Fyers auto-squares intraday positions at ~15:20)")
    _audit(db, "algo.update", s, changes)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, detail="another save of this strategy landed at the same moment — reload and retry")
    return _ser(s, db)


def _new_version(db: Session, s: AlgoStrategy, spec: dict[str, Any], note: Optional[str]) -> int:
    """`spec` becomes the strategy's next version and the active one."""
    _ensure_v1(db, s)
    v = (db.execute(select(func.max(AlgoStrategyVersion.version)).where(
        AlgoStrategyVersion.strategy_id == s.id)).scalar_one() or 0) + 1
    db.add(AlgoStrategyVersion(strategy_id=s.id, version=v, spec=spec, note=note))
    s.spec, s.version = spec, v
    return v


def _ensure_v1(db: Session, s: AlgoStrategy) -> None:
    """Strategies saved before versioning get their current spec as v1."""
    if not db.execute(select(func.count()).select_from(AlgoStrategyVersion).where(
            AlgoStrategyVersion.strategy_id == s.id)).scalar_one():
        db.add(AlgoStrategyVersion(strategy_id=s.id, version=s.version or 1, spec=s.spec, note="initial"))
        db.flush()


@router.get("/strategies/{sid}/versions")
def list_versions(sid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every version, newest first, with what each one actually traded."""
    s = _get(db, sid)
    _ensure_v1(db, s)
    db.commit()
    perf = {v: (n, pnl) for v, n, pnl in db.execute(
        select(AlgoTrade.version, func.count(), func.coalesce(func.sum(AlgoTrade.net_pnl), 0.0))
        .where(AlgoTrade.strategy_id == sid, AlgoTrade.status == "closed").group_by(AlgoTrade.version))}
    rows = db.execute(select(AlgoStrategyVersion).where(AlgoStrategyVersion.strategy_id == sid)
                      .order_by(AlgoStrategyVersion.version.desc())).scalars().all()
    return {"active": s.version or 1, "versions": [
        {"version": v.version, "spec": v.spec, "note": v.note, "active": v.version == (s.version or 1),
         "created_at": v.created_at.isoformat() if v.created_at else None,
         "closed_trades": perf.get(v.version, (0, 0))[0], "realized_pnl": round(perf.get(v.version, (0, 0.0))[1], 2)}
        for v in rows]}


@router.post("/strategies/{sid}/versions/{version}/activate")
def activate_version(sid: int, version: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Make an earlier (or later) version the one that runs. Open positions keep
    the stops/targets they were opened with."""
    s = _get(db, sid)
    _ensure_v1(db, s)
    v = db.execute(select(AlgoStrategyVersion).where(
        AlgoStrategyVersion.strategy_id == sid, AlgoStrategyVersion.version == version)).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, detail=f"strategy {sid} has no v{version}")
    s.spec, s.version = v.spec, v.version
    if s.mode == "live":
        spec = engine.normalize(s.spec)
        if engine._hhmm(LIVE_SQUARE_OFF_LATEST) < engine.session_window(spec)[2] < engine.CARRY:
            raise HTTPException(422, detail=f"v{version} squares off after {LIVE_SQUARE_OFF_LATEST} — "
                                            "switch the strategy to paper first")
    _audit(db, "algo.activate_version", s, {"version": version})
    db.commit()
    return _ser(s, db)


@router.post("/strategies/{sid}/versions/{version}/shadow", status_code=201)
def shadow_version(sid: int, version: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Shadow test: run one of this strategy's versions in PAPER beside it, as
    its own automation, to see how it trades on the live market before it
    replaces the active version. Promote it when it holds up."""
    s = _get(db, sid)
    if s.shadow_of:
        raise HTTPException(422, detail="a shadow can't have a shadow")
    _ensure_v1(db, s)
    v = db.execute(select(AlgoStrategyVersion).where(
        AlgoStrategyVersion.strategy_id == sid, AlgoStrategyVersion.version == version)).scalar_one_or_none()
    if v is None:
        raise HTTPException(404, detail=f"strategy {sid} has no v{version}")
    name = f"{s.name} · shadow v{version}"[:128]
    if db.execute(select(AlgoStrategy).where(AlgoStrategy.name == name)).scalar_one_or_none():
        raise HTTPException(409, detail=f"v{version} is already being shadow-tested")
    sh = AlgoStrategy(name=name, spec=v.spec, enabled=True, mode="paper", version=1, shadow_of=sid)
    db.add(sh)
    db.flush()
    db.add(AlgoStrategyVersion(strategy_id=sh.id, version=1, spec=v.spec, note=f"shadow of {s.name} v{version}"))
    _audit(db, "algo.shadow", s, {"version": version, "shadow": sh.id})
    db.commit()
    return _ser(sh, db)


@router.post("/strategies/{sid}/promote")
def promote_shadow(sid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """The shadow's rules become its parent's active version (an existing
    version when one matches, else a new one) and the shadow stops. The
    parent's open positions keep the levels they were opened with."""
    sh = _get(db, sid)
    if not sh.shadow_of:
        raise HTTPException(422, detail="only a shadow can be promoted")
    s = _get(db, sh.shadow_of)
    _ensure_v1(db, s)
    same = next((v for v in db.execute(select(AlgoStrategyVersion).where(
        AlgoStrategyVersion.strategy_id == s.id)).scalars() if v.spec == sh.spec), None)
    if s.mode == "live":
        spec = engine.normalize(sh.spec)
        if engine._hhmm(LIVE_SQUARE_OFF_LATEST) < engine.session_window(spec)[2] < engine.CARRY:
            raise HTTPException(422, detail=f"its square-off is after {LIVE_SQUARE_OFF_LATEST} — "
                                            "switch the live strategy to paper first")
    if same is not None:
        s.spec, s.version = same.spec, same.version
    else:
        _new_version(db, s, sh.spec, f"promoted from shadow {sh.name}"[:200])
    sh.enabled = False
    _audit(db, "algo.promote", s, {"from_shadow": sh.id, "version": s.version})
    db.commit()
    return _ser(s, db)


@router.get("/strategies/{sid}/shadow")
def shadow_compare(sid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    """A shadow beside its parent over the same stretch (since the shadow
    started): closed trades, winners and net P&L of each."""
    sh = _get(db, sid)
    if not sh.shadow_of:
        raise HTTPException(422, detail="not a shadow")

    def side(strategy_id: int) -> dict[str, Any]:
        nets = [n or 0.0 for (n,) in db.execute(select(AlgoTrade.net_pnl).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.status == "closed", AlgoTrade.entry_at >= sh.created_at))]
        return {"trades": len(nets), "wins": sum(1 for n in nets if n > 0), "net": round(sum(nets), 2)}
    parent = db.get(AlgoStrategy, sh.shadow_of)
    return {"since": sh.created_at.isoformat() if sh.created_at else None, "shadow": side(sh.id),
            "parent": side(sh.shadow_of), "parent_name": parent.name if parent else None,
            "parent_mode": parent.mode if parent else None}


@router.delete("/strategies/{sid}")
def delete_strategy(sid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _get(db, sid)
    if db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == sid, AlgoTrade.status == "open")).scalar_one():
        raise HTTPException(409, detail="square off its open positions first")
    _audit(db, "algo.delete", s, {"name": s.name})
    db.query(AlgoTrade).filter(AlgoTrade.strategy_id == sid).delete()
    db.query(AlgoStrategyVersion).filter(AlgoStrategyVersion.strategy_id == sid).delete()
    db.query(AlgoStrategy).filter(AlgoStrategy.shadow_of == sid).update({"shadow_of": None})   # orphans: plain paper
    db.delete(s)
    db.commit()
    return {"ok": True}


def _runner(request: Request):
    from app.algo.runner import AlgoRunner

    r = getattr(request.app.state, "algo_runner", None)
    if r is None:
        r = request.app.state.algo_runner = AlgoRunner()
    return r


@router.post("/strategies/{sid}/squareoff")
async def square_off(sid: int, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.algo.runner import _ltp, _row

    _get(db, sid)
    trades = [_row(t) for t in db.execute(select(AlgoTrade).where(
        AlgoTrade.strategy_id == sid, AlgoTrade.status == "open")).scalars()]
    prices = await _ltp([lg["symbol"] for t in trades for lg in t["legs"]]) if trades else {}
    runner, closed, failed = _runner(request), 0, []
    for t in trades:
        runner._retry_at.pop(t["id"], None)  # noqa: SLF001 — an operator click retries now
        if await runner._exit(t, prices, "MANUAL", time.time()):  # noqa: SLF001
            closed += 1
        else:
            failed.append(t.get("instrument") or t["symbol"])
    return {"closed": closed, "failed": failed}


def _epoch(dt: Optional[datetime]) -> Optional[int]:
    return int(dt.replace(tzinfo=timezone.utc).timestamp()) if dt else None     # stored as naive UTC


def _drift_inputs(sid: int, since: int) -> tuple[dict[int, list[dict[str, Any]]], dict[int, dict]]:
    """The strategy's live / paper trades since `since`, by the version that
    took them, and those versions' specs."""
    from app.db import session as dbs

    with dbs.SessionLocal() as db:
        s = _get(db, sid)
        trades = db.execute(select(AlgoTrade).where(
            AlgoTrade.strategy_id == sid,
            AlgoTrade.entry_at >= datetime.fromtimestamp(since, tz=timezone.utc).replace(tzinfo=None))).scalars().all()
        specs = {v.version: v.spec for v in db.execute(select(AlgoStrategyVersion).where(
            AlgoStrategyVersion.strategy_id == sid)).scalars()}
        specs.setdefault(s.version or 1, s.spec)
        rows: dict[int, list[dict[str, Any]]] = {}
        for t in trades:
            rows.setdefault(t.version or s.version or 1, []).append({
                "symbol": t.symbol, "side": t.side, "entry_t": _epoch(t.entry_at), "entry": t.entry_price,
                "exit_t": _epoch(t.exit_at), "exit": t.exit_price, "reason": t.exit_reason, "net": t.net_pnl,
                "status": t.status})
        return rows, {v: specs.get(v, s.spec) for v in rows}


@router.get("/strategies/{sid}/drift")
async def drift(sid: int, days: int = Query(10, ge=1, le=60)) -> dict[str, Any]:
    """Live vs backtest: each version that traded in the last `days` days is
    backtested over the days it traded (same engine, real candles) and every
    live / paper trade is put beside the backtest's — matched by symbol, side
    and an entry within one candle. A gap is data, fills or timing; a trade
    on one side only is a miss (runner off, no live price, a rejected order)
    or an extra (a manual action)."""
    rows, specs = await asyncio.to_thread(_drift_inputs, sid, int(time.time()) - days * DAY)
    if not rows:
        return {"strategy_id": sid, "days": days, "versions": [], "note": f"no trades in the last {days} days to compare"}
    out = []
    async with _heavy:
        for v, live in sorted(rows.items()):
            spec = _spec(specs[v])
            first = min(x["entry_t"] for x in live)
            last = max(x["exit_t"] or x["entry_t"] for x in live)
            day0 = first - (first + engine.IST) % DAY
            end = min(last - (last + engine.IST) % DAY + DAY, int(time.time()) + 60)
            item: dict[str, Any] = {"version": v, "from": day0, "to": end, "matched": [], "live_only": live, "bt_only": [],
                                    "live_net": round(sum(x["net"] or 0 for x in live), 2), "bt_net": 0.0, "notes": []}
            out.append(item)
            try:
                _guard(spec, day0, end)
                bars, notes, trade_from = await _assemble(spec, day0, end)
                res = await asyncio.to_thread(engine.run, spec, bars, None, trade_from)
            except HTTPException as e:
                item["notes"] = [{"symbol": f"v{v}", "note": f"not backtested: {e.detail}"}]
                continue
            except (ValueError, KeyError, ZeroDivisionError) as e:
                item["notes"] = [{"symbol": f"v{v}", "note": f"not backtested: {e}"}]
                continue
            bt = [{"symbol": x["symbol"], "side": x["side"], "entry_t": x["entry_t"], "entry": x["entry"],
                   "exit_t": x["exit_t"], "exit": x["exit"], "reason": x["reason"], "net": x["net"]}
                  for x in res["trades"] if day0 <= x["entry_t"] < end]
            pairs, only_live, only_bt = engine.match_trades(live, bt, engine.bar_minutes(spec) * 60 + 120)
            priced = spec["instrument"]["type"] in ("equity", "future")      # option rows mix premium / spot
            item.update({
                "matched": [{"live": a, "bt": b, "same_reason": a["reason"] == b["reason"],
                             "net_gap": round(a["net"] - b["net"], 2) if a["net"] is not None else None,
                             # + = live filled worse than the backtest
                             "entry_gap_pct": round((a["entry"] - b["entry"]) / b["entry"] * 100 * (1 if a["side"] == "BUY" else -1), 3)
                             if priced and a["entry"] and b["entry"] else None} for a, b in pairs],
                "live_only": only_live, "bt_only": only_bt,
                "bt_net": round(sum(x["net"] for x in bt), 2), "notes": notes})
    return {"strategy_id": sid, "days": days, "versions": out}


@router.get("/trades")
def list_trades(strategy_id: Optional[int] = None, strategy_ids: Optional[str] = None,
                limit: int = Query(300, ge=1, le=5000), db: Session = Depends(get_db)) -> dict[str, Any]:
    """`strategy_ids`: comma-separated, for the trades of several strategies at once."""
    from app.algo.runner import _row

    q = select(AlgoTrade).order_by(AlgoTrade.id.desc()).limit(limit)
    ids = {int(x) for x in (strategy_ids or "").split(",") if x.strip().isdigit()} | ({strategy_id} if strategy_id else set())
    if ids:
        q = q.where(AlgoTrade.strategy_id.in_(ids))
    rows = []
    for t in db.execute(q).scalars():
        r = _row(t)
        for k in ("entry_at", "exit_at"):
            r[k] = r[k].isoformat() if r[k] else None
        rows.append(r)
    return {"trades": rows}


@router.get("/status")
async def status(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.algo.runner import _ltp, _mtm, _row

    r = _runner(request)
    open_rows = [_row(t) for t in db.execute(select(AlgoTrade).where(AlgoTrade.status == "open")).scalars()]
    prices = await _ltp([lg["symbol"] for t in open_rows for lg in t["legs"]] + [t["symbol"] for t in open_rows]) if open_rows else {}
    for t in open_rows:
        t["ltp"] = prices.get((t["symbol"] if t["ref"] == "u" else t["legs"][0]["symbol"]).upper())
        m = _mtm(t["legs"], prices)
        t["unrealized"] = round(m, 2) if m is not None else None
        for lg in t["legs"]:
            lg["ltp"] = prices.get(lg["symbol"].upper())
        t["entry_at"] = t["entry_at"].isoformat()
    return {"running": getattr(request.app.state, "algo_task", None) is not None,
            "last_tick": r.last_tick, "last_sync": r.last_sync, "events": list(r.events)[:150],
            "ticks": {"seen": r.ticks_seen, "last_exit_check": r.last_tick_exit, "watching": len(r._watch)},
            "open": open_rows}


@router.get("/book")
async def book_view(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Every automation's open positions as ONE book: per underlying the
    positions, net delta (lots; shares for cash), margin and MTM, plus the
    book's day P&L against ALGO_BOOK_MAX_DAY_LOSS and its margin against
    ALGO_BOOK_MAX_MARGIN. A delta that can't be priced is null, not guessed."""
    from app.algo import fno
    from app.algo.runner import _book_all, _ltp, _mtm, _row
    from app.config import get_settings

    shadows = select(AlgoStrategy.id).where(AlgoStrategy.shadow_of.is_not(None))
    rows = [_row(t) for t in db.execute(select(AlgoTrade).where(
        AlgoTrade.status == "open", AlgoTrade.strategy_id.not_in(shadows))).scalars()]
    prices = await _ltp(list({lg["symbol"] for t in rows for lg in t["legs"]} | {t["symbol"] for t in rows})) if rows else {}
    now = time.time()
    by: dict[str, dict[str, Any]] = {}
    for t in rows:
        u = by.setdefault(t["symbol"], {"symbol": t["symbol"], "positions": 0, "margin": 0.0, "mtm": 0.0, "delta": 0.0})
        u["positions"] += 1
        u["margin"] += t.get("margin") or 0.0
        m = _mtm(t["legs"], prices)
        u["mtm"] = None if m is None or u["mtm"] is None else u["mtm"] + m
        spot = prices.get(t["symbol"].upper())
        lot = (fno.known_lot(fno.fno_name(t["symbol"])) or 1) if any(lg["kind"] != "EQ" for lg in t["legs"]) else 1
        d = engine.position_metrics(t["legs"], spot, now, lot, lambda lg: prices.get(lg["symbol"].upper()))["delta"] \
            if spot else None
        first = t["legs"][0]
        sets = first["qty"] / (first.get("per_set") or first["qty"]) if first.get("qty") else 1
        u["delta"] = None if d is None or u["delta"] is None else u["delta"] + d * sets
    out = [{**u, "margin": round(u["margin"], 2), "mtm": None if u["mtm"] is None else round(u["mtm"], 2),
            "delta": None if u["delta"] is None else round(u["delta"], 3)} for u in sorted(by.values(), key=lambda x: x["symbol"])]
    day_start = now - (now + engine.IST) % DAY
    agg = await asyncio.to_thread(_book_all, day_start)
    open_mtm = None if any(u["mtm"] is None for u in out) else sum(u["mtm"] for u in out)
    st = get_settings()
    return {"underlyings": out, "positions": len(rows), "margin": round(agg["margin"], 2),
            "realized_today": round(agg["today"], 2), "open_mtm": None if open_mtm is None else round(open_mtm, 2),
            "day_pnl": None if open_mtm is None else round(agg["today"] + open_mtm, 2),
            "limits": {"max_day_loss": st.ALGO_BOOK_MAX_DAY_LOSS or None, "max_margin": st.ALGO_BOOK_MAX_MARGIN or None},
            "halted": _runner(request)._book_halt == int((now + engine.IST) // DAY)}


@router.get("/allocation")
async def allocation_view(request: Request) -> dict[str, Any]:
    """Today's capital split across automations (ALGO_ALLOC_CAPITAL; empty
    when off): each one's weight, the volatility it was measured at, its net
    over the decay window and the capital it sizes on."""
    from app.algo.runner import _snapshot
    from app.config import get_settings

    alloc = await _runner(request).allocated(time.time())
    names = {s["id"]: s["name"] for s in (await asyncio.to_thread(_snapshot))[0]}
    return {"capital": get_settings().ALGO_ALLOC_CAPITAL or None,
            "rows": [{"strategy_id": sid, "name": names.get(sid, str(sid)), **{k: (round(v, 4) if isinstance(v, float) else v)
                                                                               for k, v in x.items()}}
                     for sid, x in sorted(alloc.items(), key=lambda kv: -kv[1]["weight"])]}


@router.post("/book/kill")
async def book_kill(request: Request) -> dict[str, Any]:
    """Operator kill switch for the Algo Lab only: flatten every automation's
    open position and stop all automation entries until tomorrow (the news
    bot and the Trade page are untouched)."""
    from app.algo.runner import _ltp, _row
    from app.db import session as dbs

    def rows() -> list[dict[str, Any]]:
        with dbs.SessionLocal() as s:
            return [_row(t) for t in s.execute(select(AlgoTrade).where(AlgoTrade.status == "open")).scalars()]
    open_rows = await asyncio.to_thread(rows)
    prices = await _ltp([lg["symbol"] for t in open_rows for lg in t["legs"]]) if open_rows else {}
    now = time.time()
    r = _runner(request)
    await r.halt_book(int((now + engine.IST) // DAY), "operator kill switch", open_rows, prices, now, "BOOK_KILL")
    left = await asyncio.to_thread(rows)
    log.warning("algo.book_kill", flattened=len(open_rows) - len(left), still_open=len(left))
    return {"flattened": len(open_rows) - len(left), "still_open": len(left)}


@router.post("/sync")
async def sync_now(request: Request) -> dict[str, Any]:
    from app.algo.runner import _snapshot

    strategies, _ = await asyncio.to_thread(_snapshot)
    if not strategies:
        return {"symbols": 0, "ok": 0, "failed": []}
    return await _runner(request).sync(strategies, days=int(request.query_params.get("days", 5)))


@router.get("/tca")
def tca(days: int = Query(30, ge=1, le=365), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Transaction-cost analysis: LIVE fills vs the quotes the runner decided
    at, per strategy and instrument kind (paper fills ARE the quote). Signed
    so + = it cost you. Beside each, the slippage % that strategy's backtests
    assume — measured above assumed means the backtests flatter it.
    ponytail: a scale-out part repeats its parent's entry fill."""
    import statistics
    from datetime import timedelta

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = db.execute(select(AlgoTrade).where(AlgoTrade.mode == "live", AlgoTrade.status != "rejected",
                                              AlgoTrade.entry_at >= since)).scalars().all()
    strategies = {s.id: s for s in db.execute(select(AlgoStrategy)).scalars()}
    groups: dict[tuple[int, str], list[tuple[float, float]]] = {}
    for t in rows:
        for lg in t.legs or []:
            groups.setdefault((t.strategy_id, lg.get("kind", "EQ")), []).extend(engine.fill_slippage(lg))
    out = []
    for (sid, kind), xs in sorted(groups.items()):
        if not xs:
            continue
        s = strategies.get(sid)
        pct = [x[0] for x in xs]
        out.append({"strategy_id": sid, "name": s.name if s else str(sid), "kind": kind, "fills": len(xs),
                    "median_pct": round(statistics.median(pct), 4), "mean_pct": round(sum(pct) / len(pct), 4),
                    "rupees": round(sum(x[1] for x in xs), 2),
                    "assumed_pct": ((s.spec or {}).get("costs") or {}).get("slippage_pct") if s else None})
    return {"days": days, "rows": out}
