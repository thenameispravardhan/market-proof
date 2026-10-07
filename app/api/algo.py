"""`/api/algo` — indicator strategies: backtest, optimise, automate, data.

    GET    /api/algo/indicators            catalog for the builder (indicators, operators, defaults)
    POST   /api/algo/backtest              {spec, start, end, chart_symbol?}
    POST   /api/algo/optimize              {spec, start, end, grid:[{path, values}], metric, min_trades}
    GET    /api/algo/data?symbol=          what the candle store holds for a symbol
    POST   /api/algo/data/download         {symbols, days} — fill the store from Fyers
    GET    /api/algo/strategies            saved strategies + live P&L
    POST   /api/algo/strategies            {name, spec}            (created OFF, paper)
    PUT    /api/algo/strategies/{id}       {name?, spec?, enabled?, mode?, account_id?, confirm?}
    DELETE /api/algo/strategies/{id}
    POST   /api/algo/strategies/{id}/squareoff
    GET    /api/algo/trades?strategy_id=&limit=
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
from sqlalchemy import func, select
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
        if op["ind"] in ("PRICE", "TIME", "CANDLE", "DTE") or len(out) >= 8:
            continue
        key = (op["ind"], op.get("field"), tuple(sorted((op.get("params") or {}).items())), op.get("tf"), op.get("mult"), op.get("add"))
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
        label = op["ind"] + (f".{op['field']}" if op.get("field") else "") +             (f"({','.join(str(v) for v in (op.get('params') or {}).values())})" if op.get("params") else "") +             (f" {op['tf']}m" if op.get("tf") else "")
        out.append({"label": label, "price": on_price, "values": [None if v is None else round(v, 4) for v in vals]})
    return out


async def _assemble(spec: dict[str, Any], start: int, end: int) -> tuple[dict[str, dict], list[dict], int]:
    bars, notes, trade_from = await data.assemble(spec, start, end)
    if not bars:
        raise HTTPException(422, detail="; ".join(f"{n['symbol']}: {n['note']}" for n in notes) or "no data")
    return bars, notes, trade_from


@router.get("/indicators")
def indicators() -> dict[str, Any]:
    return {"indicators": ind.catalog(), "operators": engine.OPS, "timeframes": engine.TIMEFRAMES,
            "cond_timeframes": engine.COND_TIMEFRAMES, "sources": ind.SOURCES, "metrics": engine.METRICS,
            "sizing": engine.SIZING, "defaults": engine.DEFAULT_SPEC, "max_range_days": MAX_RANGE_DAYS}


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
        try:
            caches: dict[str, dict] = {}
            res = await asyncio.to_thread(engine.run, spec, bars, caches, trade_from, oos_from)
            res["monte_carlo"] = await asyncio.to_thread(engine.monte_carlo, res["trades"], spec["portfolio"]["capital"])
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
    async with _heavy:
        t0 = time.monotonic()
        bars, notes, trade_from = await _assemble(spec, start, end)
        oos_from = _oos_from(bars, trade_from, pct)
        try:
            res = await asyncio.to_thread(engine.optimize, spec, bars, grid,
                                          str(body.get("metric") or "net_pnl"), int(body.get("min_trades") or 5),
                                          trade_from, oos_from)
        except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError) as e:
            raise HTTPException(422, detail=f"optimize: {e}")
    return {**res, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2), "oos_from": oos_from}


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


# ---- real tick recording (app/algo/ticks.py) --------------------------------

@router.get("/ticks/status")
def ticks_status() -> dict[str, Any]:
    from app.algo import ticks

    return ticks.recorder().status()


@router.put("/ticks/config")
def ticks_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Which symbols to record. "NIFTY:FUT" = the current NIFTY future (rolls
    by itself); an index entry records its current future too."""
    from app.algo import ticks

    syms = body.get("symbols") or []
    if not isinstance(syms, list) or len(syms) > ticks.MAX_SYMBOLS:
        raise HTTPException(422, detail=f"symbols must be a list of at most {ticks.MAX_SYMBOLS}")
    ticks.recorder().save_config(bool(body.get("enabled", True)), [str(x) for x in syms])
    return ticks.recorder().status()


@router.get("/ticks/flow")
def ticks_flow(symbol: str, resolution: str = "5", from_ts: int = Query(..., alias="from"),
               to_ts: int = Query(..., alias="to"), footprint: bool = False) -> dict[str, Any]:
    """REAL order flow per chart candle: buy / sell volume classified from
    recorded ticks, delta, trades, and the resting book at the candle's end.
    Candles align with /api/market/history (09:15 buckets, D = session).
    With footprint=true also buy/sell per price per candle."""
    from app.algo import ticks

    if resolution != "D" and not resolution.isdigit():
        raise HTTPException(422, detail="resolution is minutes or D")
    span = 86400 if resolution == "D" else int(resolution) * 60
    rec = ticks.recorder()
    key = ticks.flow_key(data.fyers_symbol(symbol))

    def bucket(t: int) -> int:
        day0 = t - (t + 19800) % 86400
        if span == 86400:
            return day0
        o_s = day0 + 33300
        return o_s + ((t - o_s) // span) * span

    rows: dict[int, list[float]] = {}
    for t, (b, s_) in sorted(rec.minute_flow(key, from_ts, to_ts).items()):
        r = rows.setdefault(bucket(t), [0.0, 0.0])
        r[0] += b
        r[1] += s_
    out = {"symbol": symbol, "key": key, "recorded": bool(rows),
           "bars": [[t, round(b, 2), round(s_, 2), round(b - s_, 2)] for t, (b, s_) in sorted(rows.items())]}
    if footprint:
        fp: dict[tuple[int, float], list[float]] = {}
        for t, price, b, s_ in rec.footprint(key, from_ts, to_ts):
            f = fp.setdefault((bucket(int(t)), price), [0.0, 0.0])
            f[0] += b
            f[1] += s_
        out["footprint"] = [[t, p, round(b, 2), round(s_, 2)] for (t, p), (b, s_) in sorted(fp.items())]
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
            "open_positions": open_n, "version": s.version or 1, "versions": versions,
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
    s = AlgoStrategy(name=name, spec=_spec(body.get("spec")), enabled=False, mode="paper", version=1)
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
            _ensure_v1(db, s)
            v = (db.execute(select(func.max(AlgoStrategyVersion.version)).where(
                AlgoStrategyVersion.strategy_id == s.id)).scalar_one() or 0) + 1
            db.add(AlgoStrategyVersion(strategy_id=s.id, version=v, spec=new, note=body.get("note")))
            s.spec, s.version = new, v
            changes["version"] = v
    if "account_id" in body:
        s.account_id = int(body["account_id"]) if body["account_id"] else None
        changes["account_id"] = s.account_id
    if body.get("mode") is not None:
        mode = str(body["mode"])
        if mode not in ("paper", "live"):
            raise HTTPException(422, detail="mode is paper or live")
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


@router.delete("/strategies/{sid}")
def delete_strategy(sid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    s = _get(db, sid)
    if db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == sid, AlgoTrade.status == "open")).scalar_one():
        raise HTTPException(409, detail="square off its open positions first")
    _audit(db, "algo.delete", s, {"name": s.name})
    db.query(AlgoTrade).filter(AlgoTrade.strategy_id == sid).delete()
    db.query(AlgoStrategyVersion).filter(AlgoStrategyVersion.strategy_id == sid).delete()
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


@router.get("/trades")
def list_trades(strategy_id: Optional[int] = None, limit: int = Query(300, ge=1, le=5000),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    from app.algo.runner import _row

    q = select(AlgoTrade).order_by(AlgoTrade.id.desc()).limit(limit)
    if strategy_id:
        q = q.where(AlgoTrade.strategy_id == strategy_id)
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
            "open": open_rows}


@router.post("/sync")
async def sync_now(request: Request) -> dict[str, Any]:
    from app.algo.runner import _snapshot

    strategies, _ = await asyncio.to_thread(_snapshot)
    if not strategies:
        return {"symbols": 0, "ok": 0, "failed": []}
    return await _runner(request).sync(strategies, days=int(request.query_params.get("days", 5)))
