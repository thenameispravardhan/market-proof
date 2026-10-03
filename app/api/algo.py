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
from sqlalchemy.orm import Session

from app.algo import data, engine
from app.algo import indicators as ind
from app.db.init import init_db
from app.db.models import AlgoStrategy, AlgoTrade, AuditLog, BrokerAccount
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


def _pricing_note(spec: dict[str, Any]) -> Optional[str]:
    inst = spec["instrument"]
    if inst["type"] == "option":
        src = {"auto": "India VIX for indices, realised volatility for stocks", "hv": "20-day realised volatility",
               "vix": "India VIX", "fixed": f"a fixed {inst['iv']['value']}%"}[inst["iv"]["source"]]
        return ("Option premiums are Black-Scholes ESTIMATES priced off the underlying's real candles with IV from "
                f"{src} — Fyers keeps no history for expired contracts. Real premiums carry skew and event IV; "
                "treat option results as a guide, then paper-trade before going live.")
    if inst["type"] == "future":
        return "Futures are backtested on the underlying's price (basis ignored); live trading uses the real contract."
    return None


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


@router.post("/backtest")
async def backtest(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    spec = _spec(body.get("spec"))
    start, end = _range(body)
    t0 = time.monotonic()
    bars, notes, trade_from = await _assemble(spec, start, end)
    try:
        res = await asyncio.to_thread(engine.run, spec, bars, None, trade_from)
    except (ValueError, KeyError) as e:
        raise HTTPException(422, detail=f"backtest: {e}")
    chart_sym = body.get("chart_symbol") if body.get("chart_symbol") in bars else next(iter(bars))
    d = bars[chart_sym]
    keep = 8000                         # enough to inspect, small enough to ship
    first = next((i for i, t in enumerate(d["t"]) if t >= trade_from), 0)
    lo = max(first, len(d["t"]) - keep)
    candles = [[d["t"][i], d["o"][i], d["h"][i], d["l"][i], d["c"][i]] for i in range(lo, len(d["t"]))]
    res["trades_total"] = len(res["trades"])
    res["trades"] = res["trades"][-3000:]
    return {**res, "spec": spec, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2),
            "pricing_note": _pricing_note(spec),
            "chart": {"symbol": chart_sym, "candles": candles,
                      "trades": [x for x in res["trades"] if x["symbol"] == chart_sym]},
            "bars": {k: sum(1 for t in v["t"] if t >= trade_from) for k, v in bars.items()}}


@router.post("/optimize")
async def optimize(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    spec = _spec(body.get("spec"))
    start, end = _range(body)
    t0 = time.monotonic()
    bars, notes, trade_from = await _assemble(spec, start, end)
    try:
        res = await asyncio.to_thread(engine.optimize, spec, bars, body.get("grid") or [],
                                      str(body.get("metric") or "net_pnl"), int(body.get("min_trades") or 5),
                                      trade_from)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise HTTPException(422, detail=f"optimize: {e}")
    return {**res, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2)}


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
    return {"id": s.id, "name": s.name, "spec": s.spec, "enabled": s.enabled, "mode": s.mode,
            "account_id": s.account_id, "closed_trades": agg[0], "realized_pnl": round(agg[1], 2),
            "open_positions": open_n,
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
    s = AlgoStrategy(name=name, spec=_spec(body.get("spec")), enabled=False, mode="paper")
    db.add(s)
    db.flush()
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
        s.spec = _spec(body["spec"])
        changes["spec"] = True
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
        if engine._hhmm(spec["session"]["square_off"]) > engine._hhmm(LIVE_SQUARE_OFF_LATEST):
            raise HTTPException(422, detail=f"live square-off must be at or before {LIVE_SQUARE_OFF_LATEST} "
                                            "(Fyers auto-squares intraday positions at ~15:20)")
    _audit(db, "algo.update", s, changes)
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
    prices = await _ltp([lg["symbol"] for t in open_rows for lg in t["legs"]] + [t["symbol"] for t in open_rows])         if open_rows else {}
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
