"""`/api/algo` — indicator strategies: backtest, optimise, automate, data.

    GET    /api/algo/indicators            catalog for the builder (indicators, operators, defaults)
    GET    /api/algo/guide                 the Algo Lab guide (markdown text): give it to an AI to write a strategy
    POST   /api/algo/backtest              {spec, start, end, chart_symbol?}
    POST   /api/algo/payoff               {spec, side?, spot?, iv_pct?} — payoff, Greeks, funds required
    POST   /api/algo/optimize              {spec, start, end, grid:[{path, values}], metric, min_trades,
                                            walk_forward?: {windows, is_mult, anchored}}
    POST   /api/algo/research/check        {spec, start, end, grid?, oos_pct?, walk_forward?, hypothesis?}:
                                           split + walk-forward + DSR + PBO -> pass / kill, saved as a card
    GET    /api/algo/research/cards        ?strategy_id=&verdict=   (GET /{id}, /{id}/markdown, PATCH /{id})
    POST   /api/algo/research/pairs        {symbols, anchor?, timeframe?, days?}: pair finder (cointegration,
                                           held-out check, half-life, z now)
    GET    /api/algo/research/surface      ?name=NIFTY&at=   the recorded chain's IV surface
    POST   /api/algo/research/surface/evaluate  {name, start, end, lag?}: surface vs flat IV on recorded quotes
    POST   /api/algo/research/tick-replay  {spec, start, end}: the backtest's stop / target exits vs recorded prints
    GET    /api/algo/research/universe     point-in-time index membership per index (POST /{index}/changes,
                                           POST /{index}/snapshot, GET /{index}/members?on=)
    GET    /api/algo/research/nightly      nightly re-validation: switch, last run, open flags (PUT to set,
                                           POST /run to run now); GET /research/flags, POST /flags/{id}/clear
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
from app.db.models import AlgoResearchCard, AlgoStrategy, AlgoStrategyVersion, AlgoTrade, AuditLog, BrokerAccount
from app.db.session import get_db
from app.logging_config import get_logger
from app.research import loop

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
    if s.get("pair_leg"):
        s["pair_leg"]["symbol"] = data.fyers_symbol(s["pair_leg"]["symbol"])
        if s["pair_leg"]["symbol"] in s["symbols"]:
            raise HTTPException(422, detail="the pair leg's symbol must be another symbol than the ones the strategy trades")
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
# process, and the semaphore makes it ONE job at a time — two big backtests in
# parallel would double the peak and could get the trading process OOM-killed
# mid-session. Measured on the same machine: 600k bars peaked +370 MB with
# candles as Python lists; as typed arrays (data.columns), indicator caches
# freed per symbol and a lazy event merge, 1.6M bars peak +280 MB. So 1.5M
# stays inside the old budget with room to spare.
MAX_BARS = 1_500_000
MAX_BAR_RUNS = 30_000_000          # optimiser: combos x bars (~4 minutes of CPU)
_heavy = asyncio.Semaphore(1)


def _estimate_bars(spec: dict[str, Any], start: int, end: int) -> int:
    """Candles held in memory at the PEAK of one run. Every symbol's base and
    higher-timeframe candles stay for the whole run; 1-minute data (order flow,
    volume / turnover candles) is loaded ONE symbol at a time, folded into base
    candles and freed (data.assemble), so it counts once, not per symbol."""
    days = (end - start) / 86400 * 5 / 7 + max(30, data.lead_days(spec) * 5 / 7)    # the warm-up history too
    btype = spec["bars"]["type"]
    base = spec["bars"]["per_day"] if btype != "time" else engine.per_session(spec["timeframe"])
    htf = sum(engine.per_session(tf) for tf in engine.cond_timeframes(spec))
    n_syms = len(spec["symbols"])
    if spec.get("universe"):                       # every past member is loaded, not just today's list
        from app.research import universe

        try:
            n_syms = len(universe.members_between(spec["universe"]["index"], universe.ist_day(start),
                                                  universe.ist_day(end - 1)))
        except ValueError as e:
            raise HTTPException(422, detail=str(e)) from None
    held = n_syms * days * (base + htf)
    for tfs in engine.ref_frames(spec).values():   # other symbols' candles: time candles, loaded once
        held += days * sum(engine.per_session(tf) for tf in tfs | {spec["timeframe"]})
    transient = days * 375 if engine.needs_minutes(spec) else 0
    return int(held + transient)


def _guard(spec: dict[str, Any], start: int, end: int, combos: int = 1) -> int:
    est = _estimate_bars(spec, start, end)
    if est > MAX_BARS:
        raise HTTPException(422, detail=(
            f"too much data for one run: ~{est:,} candles ({'the universe' if spec.get('universe') else str(len(spec['symbols'])) + ' symbols'} x "
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
            "position_metrics": engine.POSITION_METRICS, "no_symbol": sorted(engine.NO_SYMBOL), "events": ind.EVENTS}


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


@router.post("/capacity")
def capacity(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """How {spec} would run live on the Fyers plan: every symbol's candles at each
    close (exact), screened on batch quotes, or more than the budget checks
    (over) — with its data calls a day against the automations' share."""
    from app.algo import screen

    return screen.capacity(_spec(body.get("spec")))


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
        chart_sym = body.get("chart_symbol") if body.get("chart_symbol") in bars else next(iter(bars))
        try:
            caches: dict[str, dict] = {}       # only the chart's: its indicator lines are drawn
            res = await asyncio.to_thread(engine.run, spec, bars, caches, trade_from, oos_from, {chart_sym})
            res["monte_carlo"] = await asyncio.to_thread(engine.monte_carlo, res["trades"], spec["portfolio"]["capital"])
            res["trials"] = trials
            res["stats"]["deflated_sharpe"] = await asyncio.to_thread(
                engine.trial_dsr, res["trades"], bars, spec["portfolio"]["capital"], trials, trade_from)
        except (ValueError, KeyError, ZeroDivisionError) as e:
            raise HTTPException(422, detail=f"backtest: {e}")
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


# ---- research loop: one idea through every out-of-sample test -> a card ------

def _card(c: AlgoResearchCard, full: bool = True) -> dict[str, Any]:
    r = c.result or {}
    over = r.get("overfitting") or {}
    out = {"id": c.id, "strategy_id": c.strategy_id, "version": c.version, "name": c.name, "trials": c.trials,
           "verdict": c.verdict, "checks": c.checks, "hypothesis": c.hypothesis, "note": c.note, "setup": c.setup,
           "kind": c.kind or "check",
           "created_at": c.created_at.isoformat() + "Z" if c.created_at else None,
           "headline": {"dsr": (over.get("deflated_sharpe") or {}).get("dsr"), "pbo": (over.get("pbo") or {}).get("pbo"),
                        "wf_net": (r.get("walk_forward") or {}).get("oos", {}).get("net_pnl"),
                        "held_out_net": ((r.get("split") or {}).get("oos") or {}).get("net_pnl")}}
    if full:
        out.update({"spec": c.spec, "result": r, "rules": loop.rules_text(c.spec)})
    return out


def _save_card(body: dict[str, Any], spec: dict[str, Any], res: dict[str, Any], setup: dict[str, Any]) -> dict[str, Any]:
    from app.db import session as dbs

    sid = body.get("strategy_id")
    with dbs.SessionLocal() as db:
        s = db.get(AlgoStrategy, int(sid)) if sid else None
        name = s.name if s else (str(body.get("name") or "").strip()[:128] or "Unsaved draft")
        hyp = str(body.get("hypothesis") or "").strip() or None
        c = AlgoResearchCard(strategy_id=s.id if s else None, version=s.version if s else None, name=name,
                             spec=spec, setup=setup, trials=res["trials"], result=res, checks=res.pop("checks"),
                             verdict=res.pop("verdict"), hypothesis=hyp)
        db.add(c)
        db.commit()
        return _card(c)


@router.post("/research/check")
async def research_check(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{spec, start, end, grid?, metric?, min_trades?, oos_pct? (default 30),
    walk_forward?: {windows, is_mult, anchored}, strategy_id? | trials?, name?, hypothesis?}
    Runs the held-out split, a walk-forward, the Deflated Sharpe and PBO on
    the same backtests, judges them against the bar and saves the card."""
    spec = _spec(body.get("spec"))
    start, end = _range(body)
    grid = body.get("grid") or []
    combos = 1
    for g in grid:
        combos *= max(1, len(g.get("values") or []))
    _guard(spec, start, end, combos)
    pct = _oos_pct(body) or 30.0
    wf = body.get("walk_forward") or {}
    try:
        metric, min_trades = str(body.get("metric") or "net_pnl"), max(1, int(body.get("min_trades") or 10))
        windows, is_mult = engine.wf_check(wf.get("windows") or 4, wf.get("is_mult") or 3)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, detail=f"research check: {e}") from None
    async with _heavy:
        t0 = time.monotonic()
        bars, notes, trade_from = await _assemble(spec, start, end)
        oos_from = _oos_from(bars, trade_from, pct)
        trials = await asyncio.to_thread(_bump_trials, body, combos)
        try:
            res = await asyncio.to_thread(loop.check, spec, bars, grid, oos_from=oos_from, metric=metric,
                                          min_trades=min_trades, trade_from=trade_from, windows=windows,
                                          is_mult=is_mult, anchored=bool(wf.get("anchored")),
                                          prior_trials=trials - combos)
        except (ValueError, KeyError, IndexError, TypeError, ZeroDivisionError, AttributeError) as e:
            raise HTTPException(422, detail=f"research check: {e}")
    setup = {"start": str(body["start"]), "end": str(body["end"]), "grid": grid, "metric": metric,
             "min_trades": min_trades, "oos_pct": pct,
             "walk_forward": {"windows": windows, "is_mult": is_mult, "anchored": bool(wf.get("anchored"))}}
    card = await asyncio.to_thread(_save_card, body, spec, res, setup)
    return {**card, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2)}


@router.get("/research/cards")
def research_cards(strategy_id: Optional[int] = None, verdict: Optional[str] = None, kind: Optional[str] = None,
                   limit: int = Query(200, ge=1, le=1000), db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(AlgoResearchCard).order_by(AlgoResearchCard.id.desc()).limit(limit)
    if kind:
        q = q.where(AlgoResearchCard.kind == kind)
    if strategy_id:
        q = q.where(AlgoResearchCard.strategy_id == strategy_id)
    if verdict:
        q = q.where(AlgoResearchCard.verdict == verdict)
    return {"cards": [_card(c, full=False) for c in db.execute(q).scalars()], "bar": loop.BAR}


def _get_card(db: Session, cid: int) -> AlgoResearchCard:
    c = db.get(AlgoResearchCard, cid)
    if not c:
        raise HTTPException(404, detail="no such card")
    return c


@router.get("/research/cards/{cid}")
def research_card(cid: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return _card(_get_card(db, cid))


@router.get("/research/cards/{cid}/markdown")
def research_card_markdown(cid: int, db: Session = Depends(get_db)) -> PlainTextResponse:
    return PlainTextResponse(loop.markdown(_card(_get_card(db, cid))), media_type="text/markdown; charset=utf-8")


@router.patch("/research/cards/{cid}")
def research_card_edit(cid: int, body: dict[str, Any] = Body(...), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Only the idea and note text change; the results and verdict are the record."""
    c = _get_card(db, cid)
    for k in ("hypothesis", "note"):
        if k in body:
            setattr(c, k, (str(body[k] or "").strip() or None))
    db.commit()
    return _card(c)


@router.post("/research/pairs")
async def research_pairs(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{symbols: [...], anchor?: "NIFTY50-INDEX", timeframe?: 60, days?: 365}
    Every symbol against the anchor (or every pair of peers without one):
    hedge ratio, Engle-Granger on the first two thirds, the same fit
    checked on the last third, half-life, today's z — in bars of the
    timeframe the strategy will trade. `tested` is how many
    pairs were tried — load a pair and they count as its tries."""
    from app.research import pairs as pf

    try:
        syms = list(dict.fromkeys(data.fyers_symbol(str(x)) for x in body.get("symbols") or [] if str(x).strip()))
        anchor = data.fyers_symbol(str(body["anchor"])) if body.get("anchor") else None
        tf, days = int(body.get("timeframe") or 60), int(body.get("days") or 365)
    except (TypeError, ValueError) as e:
        raise HTTPException(422, detail=f"pairs: {e}") from None
    if anchor in syms:
        syms.remove(anchor)
    if len(syms) < (1 if anchor else 2) or len(syms) > pf.MAX_SYMBOLS:
        raise HTTPException(422, detail=f"give 2-{pf.MAX_SYMBOLS} symbols (1+ with an anchor)")
    if tf not in engine.TIMEFRAMES or engine.multi_day(tf):       # its pair spec trades intraday
        raise HTTPException(422, detail=f"timeframe must be an intraday one of {[t for t in engine.TIMEFRAMES if t < engine.DAILY]}")
    if not 30 <= days <= MAX_RANGE_DAYS:
        raise HTTPException(422, detail=f"days must be 30-{MAX_RANGE_DAYS}")
    per_day = 375 / tf
    est = int((len(syms) + bool(anchor)) * days * 5 / 7 * per_day)
    if est > MAX_BARS:
        raise HTTPException(422, detail=f"too much data: ~{est:,} candles (limit {MAX_BARS:,}) — fewer symbols, "
                                        "fewer days or a larger timeframe")
    end = int(time.time()) + 60
    start = end - days * DAY
    async with _heavy:
        t0 = time.monotonic()
        closes, notes = {}, []
        for fy in ([anchor] if anchor else []) + syms:
            try:
                info = await data.ensure(fy, start, end)
                b = await asyncio.to_thread(data.load, info["key"], tf, start, end)
            except Exception as e:  # noqa: BLE001 — one symbol is a note, not an error
                notes.append({"symbol": fy, "note": str(e)[:200]})
                continue
            if not b or len(b["t"]) < 60:
                notes.append({"symbol": fy, "note": "fewer than 60 candles in this range"})
                continue
            closes[fy] = {"t": b["t"], "c": b["c"]}
        if anchor and anchor not in closes:
            raise HTTPException(422, detail=f"{anchor}: no candles — " + "; ".join(n["note"] for n in notes if n["symbol"] == anchor))
        res = await asyncio.to_thread(pf.scan, closes, anchor)
    for r in res["pairs"]:
        r["spec"] = pf.spec_for(r["a"], r["b"], r["window"], tf)
    return {**res, "anchor": anchor, "timeframe": tf, "days": days, "notes": notes,
            "elapsed_s": round(time.monotonic() - t0, 2)}


@router.get("/research/surface")
def research_surface(name: str, at: Optional[int] = None) -> dict[str, Any]:
    """The IV surface of `name`'s recorded chain at `at` (default now): the
    smile per expiry and the quotes it was fitted on."""
    from app.algo import ticks
    from app.research import surface as sf

    at = int(at or time.time())
    t, rows = ticks.store().chain_at(name.upper(), at)
    if t is None:
        raise HTTPException(404, detail=f"no recorded chain for {name.upper()} that day — switch chains on in Data → Research recording")
    surf = sf.fit(rows, t)
    if surf is None:
        raise HTTPException(422, detail="the snapshot has no usable two-sided option quotes")
    pts = sf.points(rows, t, surf.spot)
    return {**surf.to_dict(), "name": name.upper(),
            "points": [{"kind": p["kind"], "strike": p["strike"], "exp": p["exp"], "k": round(p["k"], 4),
                        "iv": round(p["iv"], 4), "fit": round(surf.iv(p["strike"], surf.spot, t, p["exp"]), 4),
                        "mid": round(p["mid"], 2)} for p in pts]}


@router.post("/research/surface/evaluate")
async def research_surface_evaluate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{name: "NIFTY", start, end, lag?: 1} — does the recorded surface price
    the next snapshots closer than flat IV? (B4's bar before live Greeks.)"""
    from app.algo import fno
    from app.research import surface as sf

    name = str(body.get("name") or "").strip().upper()
    if not name:
        raise HTTPException(422, detail="name is the chain's underlying, e.g. NIFTY")
    start, end = _range(body)
    try:
        lag = int(body["lag"]) if body.get("lag") is not None else 1
    except (TypeError, ValueError):
        raise HTTPException(422, detail="lag is a whole number of snapshots") from None
    if not 1 <= lag <= 60:
        raise HTTPException(422, detail="lag is 1-60 snapshots")
    flat = None
    if name in fno.INDEX_LOTS:                          # what index backtests price with today: India VIX
        vix = await asyncio.to_thread(data.load, data.store_key(data.VIX), 1, start, end)
        if vix and vix["t"]:
            import bisect

            vt, vc = vix["t"], vix["c"]

            def flat(t: int) -> Optional[float]:
                j = bisect.bisect_right(vt, t - 60) - 1      # the last minute CLOSED by t
                return vc[j] / 100 if j >= 0 and t - vt[j] < 86400 else None
    async with _heavy:
        t0 = time.monotonic()
        res = await asyncio.to_thread(sf.evaluate, data.chain_snapshots(name, start, end), lag=lag, flat=flat)
    return {**res, "name": name, "flat_source": "India VIX" if flat else None, "elapsed_s": round(time.monotonic() - t0, 2)}


@router.post("/research/tick-replay")
async def research_tick_replay(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{spec, start, end} — backtest, then replay each fixed stop / target exit
    on the recorded trade prints: which level did price touch first?"""
    from app.algo import ticks
    from app.research import tick_replay as tr

    spec = _spec(body.get("spec"))
    start, end = _range(body)
    _guard(spec, start, end)
    st = ticks.store()

    def ticks_for(symbol: str, a: int, b: int) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        for day, key in st.keys_for(symbol, a, b).items():
            d0 = day * DAY - engine.IST
            out += [(int(r[0]), float(r[1])) for r in st.prints(key, max(a, d0), min(b, d0 + DAY))]
        return out

    async with _heavy:
        t0 = time.monotonic()
        bars, notes, trade_from = await _assemble(spec, start, end)
        try:
            res = await asyncio.to_thread(engine.run, spec, bars, {}, trade_from)
        except (ValueError, KeyError, ZeroDivisionError) as e:
            raise HTTPException(422, detail=f"backtest: {e}")
        out = await asyncio.to_thread(tr.replay, res["trades"], spec, ticks_for)
    return {**out, "notes": notes, "elapsed_s": round(time.monotonic() - t0, 2)}


@router.get("/research/universe")
def research_universes() -> dict[str, Any]:
    """Each index's point-in-time membership: recorded lists, change log, from when it holds."""
    from app.research import universe

    return {"indexes": [universe.coverage(i) for i in universe.INDEXES]}


def _index(index: str) -> str:
    from app.research import universe

    if index not in universe.INDEXES:
        raise HTTPException(404, detail=f"unknown index {index!r}")
    return index


@router.post("/research/universe/{index}/changes")
def research_universe_changes(index: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """{csv: "date,symbol,action\n2024-03-28,SHRIRAMFIN,add\n..."} — inclusions /
    exclusions from NSE's index-change announcements, by effective date."""
    from app.research import universe

    try:
        res = universe.import_changes(_index(index), str(body.get("csv") or ""))
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from None
    return {**res, **universe.coverage(index)}


@router.post("/research/universe/{index}/snapshot")
async def research_universe_snapshot(index: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Record the index's list: {symbols, date} as given (a list you have for a
    past date), or — empty — today's from niftyindices.com."""
    from app.research import universe

    _index(index)
    if body.get("symbols"):
        try:
            day = universe.day_of(date.fromisoformat(str(body.get("date") or date.today().isoformat())))
            syms = [str(x) for x in (body["symbols"] if isinstance(body["symbols"], list)
                                     else str(body["symbols"]).replace(",", " ").split())]
            n = universe.record_snapshot(index, [x.upper().removeprefix("NSE:").removesuffix("-EQ") for x in syms], day)
        except ValueError as e:
            raise HTTPException(422, detail=str(e)) from None
        return {"recorded": True, "day": universe.ymd(day), "members": n, **universe.coverage(index)}
    try:
        res = await asyncio.to_thread(universe.fetch_snapshot, index, force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, detail=f"could not fetch the list: {e}"[:300]) from None
    return {**res, **universe.coverage(index)}


@router.get("/research/universe/{index}/members")
def research_universe_members(index: str, on: Optional[str] = None) -> dict[str, Any]:
    from app.research import universe

    _index(index)
    try:
        day = universe.day_of(date.fromisoformat(on)) if on else universe.ist_day(time.time())
    except ValueError:
        raise HTTPException(422, detail="on is YYYY-MM-DD") from None
    m = universe.members_at(index, day)
    if m is None:
        raise HTTPException(404, detail="no recorded list for this index yet")
    return {"index": index, "on": universe.ymd(day), "members": sorted(m)}


def _flag(f: Any, names: dict[int, str]) -> dict[str, Any]:
    return {"id": f.id, "strategy_id": f.strategy_id, "name": names.get(f.strategy_id), "card_id": f.card_id,
            "reason": f.reason, "detail": f.detail, "created_at": f.created_at.isoformat() + "Z" if f.created_at else None,
            "cleared_at": f.cleared_at.isoformat() + "Z" if f.cleared_at else None, "cleared_note": f.cleared_note}


@router.get("/research/nightly")
def research_nightly(db: Session = Depends(get_db)) -> dict[str, Any]:
    """The nightly re-validation's switch and last run, and every open flag."""
    from app.research import revalidate

    names = dict(db.execute(select(AlgoStrategy.id, AlgoStrategy.name)).all())
    return {**revalidate.load_config(), "flags": [_flag(f, names) for f in revalidate.active_flags(db)]}


@router.put("/research/nightly")
def research_nightly_config(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    from app.research import revalidate

    try:
        return revalidate.save_config({k: body[k] for k in ("enabled", "time") if k in body})
    except ValueError as e:
        raise HTTPException(422, detail=str(e)) from None


@router.post("/research/nightly/run")
async def research_nightly_run() -> dict[str, Any]:
    """Run it now (outside market hours): every switched-on strategy."""
    from app.research import revalidate

    try:
        return await revalidate.run_nightly(heavy=_heavy)
    except RuntimeError as e:
        raise HTTPException(409, detail=str(e)) from None


@router.get("/research/flags")
def research_flags(strategy_id: Optional[int] = None, active: bool = True, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Strategies whose edge no longer holds. The live desk's auto-pause reads
    the open ones (in process: app.research.revalidate.active_flags)."""
    from app.db.models import AlgoResearchFlag
    from app.research import revalidate

    names = dict(db.execute(select(AlgoStrategy.id, AlgoStrategy.name)).all())
    if active:
        rows = revalidate.active_flags(db, strategy_id)
    else:
        q = select(AlgoResearchFlag).order_by(AlgoResearchFlag.id.desc()).limit(500)
        rows = list(db.execute(q.where(AlgoResearchFlag.strategy_id == strategy_id) if strategy_id else q).scalars())
    return {"flags": [_flag(f, names) for f in rows]}


@router.post("/research/flags/{fid}/clear")
def research_flag_clear(fid: int, body: dict[str, Any] = Body(default={}), db: Session = Depends(get_db)) -> dict[str, Any]:
    """A person looked at it: {note} says what was decided."""
    from app.db.models import AlgoResearchFlag

    f = db.get(AlgoResearchFlag, fid)
    if not f:
        raise HTTPException(404, detail="no such flag")
    if f.cleared_at is None:
        f.cleared_at = datetime.now(timezone.utc).replace(tzinfo=None)
        f.cleared_note = (str(body.get("note") or "").strip() or None)
        db.commit()
    names = dict(db.execute(select(AlgoStrategy.id, AlgoStrategy.name)).all())
    return _flag(f, names)


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
    long_days = ticks.store().long_days()
    full_days = [d for d in days if d not in set(long_days)]
    try:
        cap = ticks.capacity(cfg)
    except Exception as e:  # noqa: BLE001 — a status page never fails on the projection
        cap = {"error": str(e)[:200]}
    return {
        "enabled": bool(cfg.get("enabled")), "config": cfg, "config_symbols": cfg.get("symbols", []),
        "strategy_symbols": ticks.strategy_symbols(), "collector": {**col, "alive": alive, "supervisor": sup},
        "days": {"count": len(full_days), "first": full_days[0] if full_days else None,
                 "last": full_days[-1] if full_days else None,
                 "retention": int(cfg.get("retention_days") or ticks.RETENTION_DAYS),
                 "long": {"count": len(long_days), "first": long_days[0] if long_days else None}},
        "disk_mb": ticks.disk_mb(), "capacity": cap, "lists": {k: v["label"] for k, v in fu.LISTS.items()},
        "indices": list(ticks.INDEX_NAMES),
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
    the disk guard drops old days early, then pauses) and universe {indices
    (NIFTY, SENSEX ... — the indices recorded, with their futures and options),
    lists, stocks, index_futures, stock_futures, next_futures, index_options,
    index_strikes, index_next_strikes, index_all (then every other listed strike
    / expiry of those indices, nearest first, to the budget), stock_options,
    stock_strikes, stock_next_expiry, roll_days, enabled}, chains {enabled, underlyings (F&O
    names), strategies, strikes 0-50, expiries 1-6, interval_s 15-3600: option-
    chain snapshots on that exchange-time grid} and long {enabled, symbols,
    strategies, days 1-1095, raw_days 0-365, raw_strikes 0-20: the research
    short list, trimmed out of each day leaving the window and kept longer}."""
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
            elif k == "indices":
                if not isinstance(v, list) or any(not isinstance(x, str) or x.upper() not in ticks.INDEX_NAMES for x in v):
                    raise HTTPException(422, detail=f"indices are some of {list(ticks.INDEX_NAMES)}")
                v = list(dict.fromkeys(x.upper() for x in v))
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
    for block, lists, ints in (("chains", ("underlyings",), {"strikes": (0, 50), "expiries": (1, 6),
                                                              "interval_s": (ticks.SNAP_MIN_S, 3600)}),
                               ("long", ("symbols",), {"days": (1, 1095), "raw_days": (0, 365),
                                                       "raw_strikes": (0, 20)}),
                               ("depth", ("symbols",), {"interval_s": (1, 60), "days": (1, 30)})):
        v = body.get(block)
        if v is None:
            continue
        defaults = {"chains": ticks.DEFAULT_CHAINS, "long": ticks.DEFAULT_LONG, "depth": ticks.DEFAULT_DEPTH}[block]
        if not isinstance(v, dict):
            raise HTTPException(422, detail=f"{block} must be an object")
        clean = {}
        for k, x in v.items():
            if k not in defaults:
                raise HTTPException(422, detail=f"unknown {block} setting {k!r}")
            if k in lists:
                if not isinstance(x, list) or len(x) > ticks.MAX_FOCUS or not all(isinstance(i, str) for i in x):
                    raise HTTPException(422, detail=f"{block}.{k} must be a list of at most {ticks.MAX_FOCUS} names")
            elif k in ints:
                lo, hi = ints[k]
                if not isinstance(x, int) or isinstance(x, bool) or not lo <= x <= hi:
                    raise HTTPException(422, detail=f"{block}.{k} must be {lo}-{hi}")
            elif not isinstance(x, bool):
                raise HTTPException(422, detail=f"{block}.{k} must be true or false")
            clean[k] = x
        patch[block] = clean
    ticks.save_config(patch)
    return _flow_status()


@router.get("/ticks/chain")
def ticks_chain(name: str, at: Optional[int] = None) -> dict[str, Any]:
    """The recorded option chain of `name` (NIFTY, BANKNIFTY, SBIN ...): its latest
    snapshot at or before `at` (default now) the same IST day — every contract's
    standing quote then (bid / ask / sizes / last / OI / volume) with `qts`, the
    quote's own exchange time, and `mid` / `spread` where both sides quote. The
    underlying and its current future are rows too (kind IDX / EQ / FUT)."""
    from app.algo import ticks

    n = (name or "").strip().upper()
    if not n or len(n) > 40 or not all(ch.isalnum() or ch in "&-_" for ch in n):
        raise HTTPException(422, detail="name is an F&O underlying, e.g. NIFTY")
    t, rows = ticks.store().chain_at(n, int(at if at is not None else time.time()))
    for r in rows:
        bid, ask = r.get("bid"), r.get("ask")
        r["mid"] = round((bid + ask) / 2, 4) if bid and ask and ask >= bid else None
        r["spread"] = round(ask - bid, 4) if bid and ask and ask >= bid else None
    order = {"IDX": 0, "EQ": 0, "FUT": 1}
    rows.sort(key=lambda r: (order.get(r.get("kind") or "", 2), r.get("expiry") or 0, r.get("strike") or 0,
                             r.get("kind") or ""))
    return {"name": n, "ts": t, "count": len(rows), "rows": rows}


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
    delta_low / high = the lowest / highest running delta inside the bar,
    book_bid, book_ask = the order book's total pending buy / sell quantity,
    averaged over the bar's minutes (null where the feed carried none).
    footprint=true adds [t, price, buy, sell]; prints=true the big trades
    [ts, price, qty, side]. Crypto: Binance's taker-buy volume per candle."""
    from app.services import crypto

    seconds = resolution.upper().endswith("S") and resolution[:-1].isdigit() and 0 < int(resolution[:-1]) <= 60
    if resolution != "D" and not resolution.isdigit() and not seconds:
        raise HTTPException(422, detail="resolution is minutes, seconds (5S) or D")
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
    if seconds:
        out = await asyncio.to_thread(_seconds_flow, symbol, int(resolution[:-1]), from_ts, to_ts, footprint, prints)
        if out is not None:
            return out
        # no raw frames for this symbol (only the focus symbols keep them): its minute flow
        out = await asyncio.to_thread(_recorded_flow, symbol, "1", from_ts, to_ts, footprint, prints)
        return {**out, "source": "minute"}
    return await asyncio.to_thread(_recorded_flow, symbol, resolution, from_ts, to_ts, footprint, prints)


_TB_CACHE: dict[tuple, tuple[int, Any]] = {}


def _raw_trades(symbol: str, from_ts: int, to_ts: int) -> tuple[list, str, int]:
    """(trades from the raw frames in [from, to), the key, frames read) — an index reads its future's."""
    from app.algo import tickbars, ticks

    st = ticks.store()
    fy = data.fyers_symbol(symbol)
    tr: list = []
    key, n = ticks.flow_key(fy), 0
    for day, key in sorted(st.keys_for(fy, from_ts, to_ts).items()):
        a, b = max(from_ts, day * 86400 - 19800), min(to_ts, (day + 1) * 86400 - 19800)
        frames = st.raw_frames(key, a, b)
        n += len(frames)
        tr += tickbars.trades(frames, exchange_hours=fy.upper().startswith(("NSE:", "BSE:")))
    return tr, key, n


def _seconds_flow(symbol: str, span: int, from_ts: int, to_ts: int, footprint: bool, prints: bool) -> Optional[dict[str, Any]]:
    """/ticks/flow at `span` seconds, from the raw frames; None when the symbol has none."""
    from app.algo import tickbars, ticks

    tr, key, n = _raw_trades(symbol, from_ts, to_ts)
    if not n:
        return None
    ck = ("sec", key, span, from_ts // 60, to_ts // 60)
    hit = _TB_CACHE.get(ck)
    if hit and hit[0] == n:
        bars, rows = hit[1]
    else:
        bars, rows = tickbars.time_bars(tr, span, exchange_hours=data.fyers_symbol(symbol).upper().startswith(("NSE:", "BSE:")))
        if len(_TB_CACHE) > 64:
            _TB_CACHE.clear()
        _TB_CACHE[ck] = (n, (bars, rows))
    out: dict[str, Any] = {"symbol": symbol, "key": key, "recorded": bool(bars), "source": "raw", "bars": bars}
    if footprint:
        out["footprint"] = rows[-FOOTPRINT_MAX_ROWS:]
        out["footprint_truncated"] = len(rows) > FOOTPRINT_MAX_ROWS
    if prints:
        st = ticks.store()
        out["prints"] = [[int(t), p, q, int(sd)] for t, p, q, sd in st.prints(key, from_ts, to_ts)]
    return out


@router.get("/ticks/tickbars")
async def ticks_tickbars(symbol: str, kind: str = "range", size: float = Query(..., gt=0),
                         from_ts: int = Query(..., alias="from"), to_ts: int = Query(..., alias="to")) -> dict[str, Any]:
    """Order-flow bars that close on price, quantity or trade count — built tick by tick from
    the raw frames (app/algo/tickbars.py; kept for the focus symbols, raw_days). `kind`: range
    (`size` = the bar's high - low in price), volume (`size` = buy + sell quantity) or tick
    (`size` = trades). Each bar {t, open, high, low, close, volume, buy, sell, delta, trades, unk,
    delta_low, delta_high} — t is the opening trade's time, nudged to keep one bar per second;
    footprint rows [t, price, buy, sell]. An index reads its future's, at futures prices."""
    from app.algo import tickbars

    if kind not in ("range", "volume", "tick"):
        raise HTTPException(422, detail="kind is range, volume or tick")
    if not 0 < to_ts - from_ts <= 3 * 86400:
        raise HTTPException(422, detail="from must be before to, at most 3 days apart")

    def work() -> dict[str, Any]:
        tr, key, n = _raw_trades(symbol, from_ts, to_ts)
        ck = ("tb", key, kind, size, from_ts // 60, to_ts // 60)
        hit = _TB_CACHE.get(ck)
        if hit and hit[0] == n:
            bars, rows = hit[1]
        else:
            bars, rows = tickbars.tick_bars(tr, kind, size)
            if len(_TB_CACHE) > 64:
                _TB_CACHE.clear()
            _TB_CACHE[ck] = (n, (bars, rows))
        return {"symbol": symbol, "key": key, "kind": kind, "size": size, "recorded": bool(n), "bars": bars,
                "footprint": rows[-FOOTPRINT_MAX_ROWS:]}

    return await asyncio.to_thread(work)


FOOTPRINT_MAX_ROWS = 250_000


def _flow_bucket(resolution: str, exchange_hours: bool):
    """Epoch -> the start of its chart candle (09:15 buckets, D = the session's day)."""
    span = 86400 if resolution == "D" else int(resolution) * 60

    def bucket(t: int) -> int:
        day0 = t - (t + 19800) % 86400
        if span == 86400:
            return day0
        o_s = day0 + 33300
        if exchange_hours:
            # the pre-open auction (before 09:15) and the closing session (after 15:30) have
            # no candle of their own: they go to the session's first / last one, never to a
            # bucket before the open the chart would fold into the previous day's last candle
            t = min(max(t, o_s), day0 + 55800 - 1)
        return o_s + ((t - o_s) // span) * span

    return bucket


def _options_flow_name(symbol: str) -> Optional[str]:
    """The F&O underlying behind an index, its future or one of its options (NIFTY, SENSEX ...)."""
    from app.algo import fno

    s = (symbol or "").strip().upper()
    if s in fno.INDICES:
        return fno.INDICES[s][0]
    if ":" not in s:
        return s if s.replace("&", "").replace("-", "").isalnum() and len(s) <= 40 else None
    return fno.underlying_of(s)


DEPTH_HOLD_S = 60          # a book sample stands at most this long (a gap in the feed isn't a minute-long book)


def _depth_cells(rows: list[tuple], bucket, span: int, start: int, end: int) -> list[list]:
    """Time-weighted resting quantity per (candle, price) from book samples: each sample's
    levels stand until the next sample (at most DEPTH_HOLD_S), split across the candles they
    span. [t, price, bid_qty, ask_qty], quantities averaged over the candle's recorded time."""
    acc: dict[tuple[int, float], list[float]] = {}
    covered: dict[int, float] = {}
    for i, r in enumerate(rows):
        t0 = max(int(r[0]), start)
        # a sample stands until the next (one is written only when the book changes), at most DEPTH_HOLD_S
        t1 = min(int(rows[i + 1][0]) if i + 1 < len(rows) else end, int(r[0]) + DEPTH_HOLD_S, end)
        while t0 < t1:
            b = bucket(t0)
            nxt = min(t1, b + span) if b + span > t0 else t0 + 1  # to candle b's end (a clamped one: a second)
            w = nxt - t0
            covered[b] = covered.get(b, 0.0) + w
            for side, off in ((0, 1), (1, 1 + 2 * 5)):
                for k in range(5):
                    px, q = r[off + 2 * k], r[off + 2 * k + 1]
                    if px and q:
                        c = acc.setdefault((b, float(px)), [0.0, 0.0])
                        c[side] += w * float(q)
            t0 = nxt
    return [[b, p, round(v[0] / covered[b], 1), round(v[1] / covered[b], 1)] for (b, p), v in sorted(acc.items())]


@router.get("/ticks/depth")
async def ticks_depth(symbol: str, resolution: str = "1", from_ts: int = Query(..., alias="from"),
                      to_ts: int = Query(..., alias="to")) -> dict[str, Any]:
    """The recorded order book as a heatmap: per chart candle and price, the resting bid / ask
    quantity, time-weighted over the candle ([t, price, bid_qty, ask_qty]). 5 levels a side
    (Fyers' DepthUpdate), recorded for the indices' current futures and `depth.symbols`, kept
    `depth.days`. An index reads its future's, at futures prices (`key`)."""
    from app.algo import ticks

    seconds = resolution.upper().endswith("S") and resolution[:-1].isdigit() and 0 < int(resolution[:-1]) <= 60
    if resolution != "D" and not resolution.isdigit() and not seconds:
        raise HTTPException(422, detail="resolution is minutes, seconds (5S) or D")
    if not 0 < to_ts - from_ts <= 7 * 86400:
        raise HTTPException(422, detail="from must be before to, at most 7 days apart")
    fy = data.fyers_symbol(symbol)
    st = ticks.store()
    span = int(resolution[:-1]) if seconds else 86400 if resolution == "D" else int(resolution) * 60
    if seconds:
        def bucket(t: int) -> int:
            return t - t % span
    else:
        bucket = _flow_bucket(resolution, fy.upper().startswith(("NSE:", "BSE:")))

    def work() -> tuple[list[list], str]:
        out: list[list] = []
        key = ticks.flow_key(fy)
        end = min(to_ts, int(time.time()) + 1)                 # the live book stands until now, not into the future
        for day, key in sorted(st.keys_for(fy, from_ts, end).items()):
            a, b = max(from_ts, day * 86400 - 19800), min(end, (day + 1) * 86400 - 19800)
            out += _depth_cells(st.depth_rows(key, a, b), bucket, span, a, b)
        return out, key

    cells, key = await asyncio.to_thread(work)
    return {"symbol": symbol, "key": key, "recorded": bool(cells), "cells": cells}


@router.get("/ticks/icebergs")
async def ticks_icebergs(symbol: str, from_ts: int = Query(..., alias="from"), to_ts: int = Query(..., alias="to"),
                         min_ratio: float = Query(2.0, ge=1.0, le=50.0)) -> dict[str, Any]:
    """Likely icebergs (app/algo/iceberg.py) from the raw frames: a price where the
    quantity traded reached `min_ratio` x the largest size the book ever showed
    there while it held. Each: {start, end, price, side, traded, shown, refills,
    ratio} — side is the hidden order's (SELL = an offer absorbing buyers). Raw
    frames are kept only for the focus symbols (raw_days); an index reads its
    future's, at futures prices (`key` says which)."""
    from app.algo import iceberg, ticks

    if not 0 < to_ts - from_ts <= 7 * 86400:
        raise HTTPException(422, detail="from must be before to, at most 7 days apart")
    fy = data.fyers_symbol(symbol)
    st = ticks.store()

    def work() -> tuple[list[dict[str, Any]], str, bool]:
        out: list[dict[str, Any]] = []
        key, any_raw = ticks.flow_key(fy), False
        for day, key in sorted(st.keys_for(fy, from_ts, to_ts).items()):
            a, b = max(from_ts, day * 86400 - 19800), min(to_ts, (day + 1) * 86400 - 19800)
            frames = st.raw_frames(key, a, b)
            any_raw = any_raw or bool(frames)
            out += iceberg.detect(frames, min_ratio=min_ratio)
        return out, key, any_raw

    found, key, any_raw = await asyncio.to_thread(work)
    return {"symbol": symbol, "key": key, "recorded": any_raw, "icebergs": found}


@router.get("/ticks/options-strikes")
async def ticks_options_strikes(symbol: str, from_ts: int = Query(..., alias="from"), to_ts: int = Query(..., alias="to"),
                                expiry: str = "near") -> dict[str, Any]:
    """Where the option money went, strike by strike: every recorded option of an
    index (or F&O underlying) in [from, to) added up per strike. `symbol`: the
    index, its future, one of its options or the name (NIFTY). Each row:
    [strike, ce_buy, ce_sell, pe_buy, pe_sell, ce_buy_value, ce_sell_value,
    pe_buy_value, pe_sell_value, net_value] — quantities, premium in ₹ the
    aggressors paid, and net = (calls b − s) − (puts b − s), + = bullish.
    `expiry`: near (the nearest running expiry, default) or all (strikes of
    every expiry added up)."""
    from app.algo import ticks

    if expiry not in ("all", "near"):
        raise HTTPException(422, detail="expiry is all or near")
    name = _options_flow_name(symbol)
    if not name:
        raise HTTPException(422, detail="symbol is an F&O underlying, its future or option, e.g. NIFTY")
    if not 0 < to_ts - from_ts <= 31 * 86400:
        raise HTTPException(422, detail="from must be before to, at most 31 days apart")
    rows = await asyncio.to_thread(ticks.store().options_by_strike, name, from_ts, to_ts, expiry == "near")
    by: dict[float, list[float]] = {}
    for strike, kind, buy, sell, bv, sv in rows:
        x = by.setdefault(strike, [0.0] * 8)
        o = 0 if kind == "CE" else 2
        x[o] += buy
        x[o + 1] += sell
        x[4 + o] += bv
        x[5 + o] += sv
    out = [[k, *(round(v, 2) for v in x), round((x[4] - x[5]) - (x[6] - x[7]), 2)] for k, x in sorted(by.items())]
    return {"symbol": symbol, "name": name, "expiry": expiry, "recorded": bool(out), "rows": out}


@router.get("/ticks/options-flow")
async def ticks_options_flow(symbol: str, resolution: str = "5", from_ts: int = Query(..., alias="from"),
                             to_ts: int = Query(..., alias="to"), expiry: str = "all") -> dict[str, Any]:
    """Every recorded option of an index (or any F&O underlying) together, per
    chart candle: who bought / sold calls and puts. `symbol`: the index, its
    future, one of its options or the name (NIFTY). Each bar: [t, ce_buy,
    ce_sell, pe_buy, pe_sell, ce_buy_value, ce_sell_value, pe_buy_value,
    pe_sell_value, net_value] — quantities, and premium in ₹ (price x qty) the
    aggressors paid; net_value = (calls bought − sold) − (puts bought − sold),
    + = bullish premium. `expiry`: all, or near (the nearest running expiry)."""
    from app.algo import ticks

    if resolution != "D" and not resolution.isdigit():
        raise HTTPException(422, detail="resolution is minutes or D")
    if expiry not in ("all", "near"):
        raise HTTPException(422, detail="expiry is all or near")
    name = _options_flow_name(symbol)
    if not name:
        raise HTTPException(422, detail="symbol is an F&O underlying, its future or option, e.g. NIFTY")
    if to_ts - from_ts > 31 * 86400:
        raise HTTPException(422, detail="at most 31 days at a time")

    # the store caches each day (the one being recorded for a few seconds): the chart and the panel poll this
    rows = await asyncio.to_thread(ticks.store().options_flow, name, from_ts, to_ts, expiry == "near")
    bucket = _flow_bucket(resolution, True)
    bars: dict[int, list[float]] = {}
    for m, kind, buy, sell, bv, sv in rows:
        x = bars.setdefault(bucket(int(m)), [0.0] * 8)
        o = 0 if kind == "CE" else 2
        x[o] += buy
        x[o + 1] += sell
        x[4 + o] += bv
        x[5 + o] += sv
    out = [[t, *(round(v, 2) for v in x), round((x[4] - x[5]) - (x[6] - x[7]), 2)] for t, x in sorted(bars.items())]
    return {"symbol": symbol, "name": name, "expiry": expiry, "recorded": bool(out), "bars": out}


def _recorded_flow(symbol: str, resolution: str, from_ts: int, to_ts: int, footprint: bool,
                   prints: bool = False) -> dict[str, Any]:
    from app.algo import fno, ticks

    st = ticks.store()
    fy = data.fyers_symbol(symbol)
    bucket = _flow_bucket(resolution, fy.upper().startswith(("NSE:", "BSE:")))

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
            t, buy, sell, unk, trades, _, _, _, _, c, tbq, tsq, oi, _ = r
            buy, sell = buy or 0.0, sell or 0.0
            x = bars.setdefault(bucket(int(t)), [0.0, 0.0, 0.0, 0, None, None, 0.0, 0.0, 0.0, 0.0, 0.0, 0])
            x[0] += buy
            x[1] += sell
            x[2] += unk or 0.0
            x[3] += int(trades or 0)
            x[4] = oi if oi is not None else x[4]
            x[5] = c if c is not None else x[5]
            x[6] += buy - sell                                     # running delta inside the bar
            x[7], x[8] = min(x[7], x[6]), max(x[8], x[6])
            if (tbq or 0) > 0 or (tsq or 0) > 0:                   # the book's totals: a mean over the bar's minutes
                x[9] += tbq or 0.0
                x[10] += tsq or 0.0
                x[11] += 1
    key_last = spans[-1][0] if spans else ticks.flow_key(fy)
    # an index's flow is its future's: name the contract, so the chart can open it
    future = None
    if fy.upper() in fno.INDICES and spans:
        future = (st.meta(ticks.ist_day(max(spans[-1][1], spans[-1][2] - 1))).get(key_last) or {}).get("symbol")
    out: dict[str, Any] = {
        "symbol": symbol, "key": key_last, "recorded": bool(bars), "future": future,
        "bars": [[t, round(v[0], 2), round(v[1], 2), round(v[0] - v[1], 2), v[3], round(v[2], 2), v[4], v[5],
                  round(v[7], 2), round(v[8], 2),
                  round(v[9] / v[11], 1) if v[11] else None, round(v[10] / v[11], 1) if v[11] else None]
                 for t, v in sorted(bars.items())],
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
    if s.enabled and (s.spec or {}).get("pair_leg"):
        # B3 is backtest-only: the runner would trade the first leg unhedged until its live half (A4) ships.
        raise HTTPException(422, detail="two-symbol positions (pair leg) are backtest-only for now — "
                                        "the live / paper half isn't built yet")
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


def _open_rows(book_only: bool = False) -> list[dict[str, Any]]:
    """Open automation trades as dicts — called off the event loop: the Algo
    Lab polls /status every second, and a sync query on the loop stalled every
    other request and websocket push (the Trade page's ticks too) meanwhile."""
    from app.algo.runner import _row
    from app.db import session as dbs

    q = select(AlgoTrade).where(AlgoTrade.status == "open")
    if book_only:      # shadows are tests: not in the book
        q = q.where(AlgoTrade.strategy_id.not_in(select(AlgoStrategy.id).where(AlgoStrategy.shadow_of.is_not(None))))
    with dbs.SessionLocal() as db:
        return [_row(t) for t in db.execute(q).scalars()]


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    from app.algo.runner import _ltp, _mtm

    r = _runner(request)
    open_rows = await asyncio.to_thread(_open_rows)
    prices = await _ltp([lg["symbol"] for t in open_rows for lg in t["legs"]] + [t["symbol"] for t in open_rows]) if open_rows else {}
    for t in open_rows:
        t["ltp"] = prices.get((t["symbol"] if t["ref"] == "u" else t["legs"][0]["symbol"]).upper())
        m = _mtm(t["legs"], prices)
        t["unrealized"] = round(m, 2) if m is not None else None
        for lg in t["legs"]:
            lg["ltp"] = prices.get(lg["symbol"].upper())
        t["entry_at"] = t["entry_at"].isoformat()
    return {"running": getattr(request.app.state, "algo_task", None) is not None,
            "screen": r.screen.status(),
            "last_tick": r.last_tick, "last_sync": r.last_sync, "events": list(r.events)[:150],
            "ticks": {"seen": r.ticks_seen, "last_exit_check": r.last_tick_exit, "watching": len(r._watch)},
            "open": open_rows}


@router.get("/book")
async def book_view(request: Request) -> dict[str, Any]:
    """Every automation's open positions as ONE book: per underlying the
    positions, net delta (lots; shares for cash), margin and MTM, plus the
    book's day P&L against ALGO_BOOK_MAX_DAY_LOSS and its margin against
    ALGO_BOOK_MAX_MARGIN, and `account_halt`: the account circuit breaker's
    reason while it stops automation entries. A delta that can't be priced is
    null, not guessed."""
    from app.algo import fno
    from app.algo.runner import _book_all, _ltp, _mtm, _portfolio_halt
    from app.config import get_settings

    rows = await asyncio.to_thread(_open_rows, True)
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
            "limits": {"max_day_loss": st.ALGO_BOOK_MAX_DAY_LOSS or None, "max_margin": st.ALGO_BOOK_MAX_MARGIN or None,
                       "follow_account_halt": bool(st.ALGO_BOOK_FOLLOW_PORTFOLIO_HALT)},
            "halted": await _runner(request).book_halted(now),
            # the account-wide circuit breaker (Settings → Circuit breakers / the kill switch): no entries while on
            "account_halt": await asyncio.to_thread(_portfolio_halt) if st.ALGO_BOOK_FOLLOW_PORTFOLIO_HALT else None}


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
