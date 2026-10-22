"""Live automation: run enabled Algo strategies on bar close, manage exits on LTP.

On every Fyers websocket tick for a symbol an open position holds (and in a
1 s sweep on REST-backed LTPs as the backstop) during the session:
  * open positions are re-priced and checked against
    stop / target / trailing (with activation) / breakeven on their reference
    price, the MTM (rupee) stop / target / trail on the whole position, the
    time stop, and the strategy's square-off time — exits never wait for a bar;
    scale-out levels close part of the position (booked as its own row);
  * per strategy, the day's realised + open P&L is checked against the daily
    max loss / max profit, which flatten the strategy and stop it for the day;
  * each enabled strategy whose timeframe just closed a bar fetches its last
    COMPLETED candles (and any higher timeframes its conditions use) from
    Fyers, evaluates the same engine the backtest runs, and enters / exits.

What gets traded follows the strategy's instrument: the stock or index
itself, its current/next-month future (from the F&O scrip master), or
option legs picked from the LIVE Fyers option chain (ATM / N strikes ITM or
OTM / closest to a target premium; weekly or monthly; current or next).

Modes, chosen per strategy on the Algo page:
  paper — fills at the live LTP, nothing is sent to a broker;
  live  — MARKET INTRADAY orders through Manager.place_manual_order on the
          chosen real Fyers account, so the global risk caps apply to entries.
          Multi-leg entries place bought legs before written ones (margin) and
          roll back the placed legs if any leg is refused. Exits bypass risk —
          closing must never be blocked — written legs first, and each leg is
          checked against the broker's net position before an order is sent.

Invariants kept: Fyers-only prices; no live price -> no entry and no booked
exit (never a synthetic fill); everything is flat by the square-off time and
anything left at 15:30 is closed — unless the strategy's square-off is switched
off: then it carries forward (live orders go out as NRML / CNC, not MIS).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select

from app.algo import data, engine, fno
from app.algo import indicators
from app.config import get_settings
from app.db import session as db_session
from app.db.models import AlgoStrategy, AlgoTrade, AuditLog, BrokerAccount
from app.logging_config import get_logger

log = get_logger(__name__)

IST = engine.IST
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
GRACE_S = 3          # let Fyers finalise the candle that just closed
STALE_BAR_S = 120    # a bar this old at first sight was missed, not acted on
GUARD_EVERY_S = 60   # how often each strategy's guard limits are read
GUARD_MIN_FILLS = 10 # the slippage guard needs at least this many live fills
REF_WAIT_S = 30      # wait this long for another symbol the conditions read to publish its candle
SYNC_AFTER_MIN = 15 * 60 + 45


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _naive(ts: float) -> datetime:
    return _utc(ts).replace(tzinfo=None)


def _carries(s: Optional[dict]) -> bool:
    """Square-off switched off: the position is meant to stay open overnight."""
    return s is not None and engine.session_window(s["spec"])[2] >= engine.CARRY


def _epoch(dt: datetime) -> float:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


# ---- DB helpers (run in a worker thread) ------------------------------------

def _snapshot() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with db_session.SessionLocal() as db:
        strategies = []
        for s in db.execute(select(AlgoStrategy)).scalars():
            try:
                spec = engine.normalize(s.spec)
            except ValueError as e:
                log.warning("algo.bad_spec", strategy=s.id, error=str(e))
                continue
            strategies.append({"id": s.id, "name": s.name, "enabled": s.enabled, "mode": s.mode,
                               "account_id": s.account_id, "spec": spec, "version": s.version or 1,
                               "shadow_of": s.shadow_of})
        trades = [_row(t) for t in db.execute(
            select(AlgoTrade).where(AlgoTrade.status == "open")).scalars()]
    return strategies, trades


def _guard_stats(sid: int, day_start: float, fills: int = 20) -> dict[str, Any]:
    """What a strategy's guard reads: its closed net P&L now and at its peak,
    entries rejected today, and the slippage of its last `fills` live fills."""
    with db_session.SessionLocal() as db:
        cum = peak = 0.0
        for (net,) in db.execute(select(AlgoTrade.net_pnl).where(
                AlgoTrade.strategy_id == sid, AlgoTrade.status == "closed").order_by(AlgoTrade.exit_at)):
            cum += net or 0.0
            peak = max(peak, cum)
        rejects = db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == sid, AlgoTrade.status == "rejected",
            AlgoTrade.entry_at >= _naive(day_start))).scalar_one()
        slip: list[float] = []
        for (legs,) in db.execute(select(AlgoTrade.legs).where(
                AlgoTrade.strategy_id == sid, AlgoTrade.mode == "live", AlgoTrade.status == "closed")
                .order_by(AlgoTrade.exit_at.desc()).limit(fills)):
            slip += [p for lg in legs or [] for p, _ in engine.fill_slippage(lg)]
    return {"closed": cum, "peak": peak, "rejects": rejects, "slippage": slip[:fills]}


def guard_breach(g: dict, st: dict, open_mtm: float) -> Optional[str]:
    """The first guard limit a strategy has breached, or None. Drawdown is
    from the peak of its closed P&L to now, open positions marked to market."""
    import statistics

    now = st["closed"] + open_mtm
    dd = max(st["peak"], now) - now
    if g.get("max_drawdown") and dd >= g["max_drawdown"]:
        return f"drawdown ₹{dd:,.0f} reached the ₹{g['max_drawdown']:,.0f} limit"
    if g.get("max_rejects") and st["rejects"] >= g["max_rejects"]:
        return f"{st['rejects']} entries rejected today (limit {g['max_rejects']})"
    sl = st["slippage"]
    if g.get("max_slippage_pct") and len(sl) >= GUARD_MIN_FILLS and statistics.median(sl) > g["max_slippage_pct"]:
        return (f"median slippage {statistics.median(sl):.3f}% over the last {len(sl)} live fills "
                f"is above the {g['max_slippage_pct']}% limit")
    return None


def _guard_off(sid: int, why: str) -> bool:
    """Switch a strategy off for its guard (audited). False = it already was."""
    with db_session.SessionLocal() as db:
        s = db.get(AlgoStrategy, sid)
        if s is None or not s.enabled:
            return False
        s.enabled = False
        db.add(AuditLog(actor="algo-guard", action="algo.guard_pause", target=f"algo_strategy:{sid}",
                        before={"enabled": True}, after={"enabled": False, "why": why}))
        db.commit()
        return True


def _row(t: AlgoTrade) -> dict[str, Any]:
    r = {c.name: getattr(t, c.name) for c in AlgoTrade.__table__.columns}
    if not r.get("legs"):        # rows from before F&O support: one equity leg
        r["legs"] = [{"symbol": r["symbol"], "kind": "EQ", "act": 1 if r["side"] == "BUY" else -1,
                      "qty": r["quantity"], "entry": r["entry_price"], "label": r["symbol"]}]
        r["ref"] = r.get("ref") or "u"
    return r


PARTIAL_NOTE = "partial exit of #"     # rows split off a position by a scale-out


def _trades_today(strategy_id: int, symbol: str, day_start: float) -> int:
    with db_session.SessionLocal() as db:
        return db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.symbol == symbol,
            AlgoTrade.status != "rejected", AlgoTrade.entry_at >= _naive(day_start),
            or_(AlgoTrade.note.is_(None), ~AlgoTrade.note.startswith(PARTIAL_NOTE)))).scalar_one()


def _last_exit(strategy_id: int, symbol: str) -> Optional[float]:
    with db_session.SessionLocal() as db:
        v = db.execute(select(func.max(AlgoTrade.exit_at)).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.symbol == symbol)).scalar_one()
        return _epoch(v) if v else None


def _book(strategy_id: int, day_start: float) -> dict[str, float]:
    """Realised P&L (all time / today), margin blocked and positions open."""
    with db_session.SessionLocal() as db:
        total = db.execute(select(func.coalesce(func.sum(AlgoTrade.net_pnl), 0.0)).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.status == "closed")).scalar_one()
        today = db.execute(select(func.coalesce(func.sum(AlgoTrade.net_pnl), 0.0)).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.status == "closed",
            AlgoTrade.exit_at >= _naive(day_start))).scalar_one()
        margin, n = db.execute(select(func.coalesce(func.sum(AlgoTrade.margin), 0.0), func.count()).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.status == "open")).one()
    return {"realized": float(total), "today": float(today), "margin": float(margin), "open": int(n)}


def _book_all(day_start: float) -> dict[str, float]:
    """Every automation together (shadows are tests: left out): P&L realised
    today and the margin their open positions block."""
    book = select(AlgoStrategy.id).where(AlgoStrategy.shadow_of.is_(None))
    with db_session.SessionLocal() as db:
        today = db.execute(select(func.coalesce(func.sum(AlgoTrade.net_pnl), 0.0)).where(
            AlgoTrade.status == "closed", AlgoTrade.exit_at >= _naive(day_start),
            AlgoTrade.strategy_id.in_(book))).scalar_one()
        margin = db.execute(select(func.coalesce(func.sum(AlgoTrade.margin), 0.0)).where(
            AlgoTrade.status == "open", AlgoTrade.strategy_id.in_(book))).scalar_one()
    return {"today": float(today), "margin": float(margin)}


ALLOC_MIN_DAYS = 10   # days with trades before a strategy's own volatility is trusted


def alloc_weights(daily: dict[int, list[float]], recent: dict[int, float]) -> dict[int, dict[str, Any]]:
    """Inverse-volatility weights. `daily` = each strategy's net P&L per
    trading day of the lookback (0 on a day it didn't trade), `recent` = its
    net over the decay window. A strategy with fewer than ALLOC_MIN_DAYS
    trading days borrows the median volatility of the others (no history, no
    special treatment); one losing over the decay window gets half weight.
    Weights sum to 1."""
    import statistics

    vol = {}
    for sid, xs in daily.items():
        if sum(1 for x in xs if x) >= ALLOC_MIN_DAYS and len(xs) > 1 and statistics.pstdev(xs) > 0:
            vol[sid] = statistics.pstdev(xs)
    fallback = statistics.median(vol.values()) if vol else 1.0
    raw = {}
    for sid in daily:
        v = vol.get(sid, fallback)
        raw[sid] = (1 / v) * (0.5 if recent.get(sid, 0.0) < 0 else 1.0)
    total = sum(raw.values()) or 1.0
    return {sid: {"weight": raw[sid] / total, "vol": vol.get(sid), "decayed": recent.get(sid, 0.0) < 0,
                  "recent": round(recent.get(sid, 0.0), 2)} for sid in daily}


def allocation(strategies: list[dict], now: float) -> dict[int, dict[str, Any]]:
    """ALGO_ALLOC_CAPITAL split across the enabled, non-shadow automations
    (empty when off): {strategy id: {capital, weight, vol, decayed, recent}}."""
    st = get_settings()
    total = st.ALGO_ALLOC_CAPITAL
    sids = [s["id"] for s in strategies if s["enabled"] and not s.get("shadow_of")]
    if not total or not sids:
        return {}
    today = int((now + IST) // 86400)
    days = [d for d in range(today - int(st.ALGO_ALLOC_LOOKBACK_DAYS), today) if (d + 3) % 7 < 5]   # weekdays
    decay_from = today - int(st.ALGO_ALLOC_DECAY_DAYS)
    with db_session.SessionLocal() as db:
        rows = db.execute(select(AlgoTrade.strategy_id, AlgoTrade.exit_at, AlgoTrade.net_pnl).where(
            AlgoTrade.strategy_id.in_(sids), AlgoTrade.status == "closed",
            AlgoTrade.exit_at >= _naive(days[0] * 86400 - IST if days else now))).all()
    per: dict[int, dict[int, float]] = {sid: {} for sid in sids}
    for sid, ex, net in rows:
        d = int((_epoch(ex) + IST) // 86400)
        per[sid][d] = per[sid].get(d, 0.0) + (net or 0.0)
    w = alloc_weights({sid: [per[sid].get(d, 0.0) for d in days] for sid in sids},
                      {sid: sum(v for d, v in per[sid].items() if d >= decay_from) for sid in sids})
    return {sid: {**x, "capital": round(total * x["weight"], 2)} for sid, x in w.items()}


def _insert(**fields: Any) -> int:
    with db_session.SessionLocal() as db:
        t = AlgoTrade(**fields)
        db.add(t)
        db.commit()
        return t.id


def _update(trade_id: int, **fields: Any) -> None:
    with db_session.SessionLocal() as db:
        t = db.get(AlgoTrade, trade_id)
        if t is not None:
            for k, v in fields.items():
                setattr(t, k, v)
            db.commit()


def _fresh(trade_id: int) -> Optional[dict[str, Any]]:
    with db_session.SessionLocal() as db:
        t = db.get(AlgoTrade, trade_id)
        return _row(t) if t is not None else None


def _split(trade_id: int, closed: Optional[dict[str, Any]], **open_fields: Any) -> None:
    """A scale-out in one commit: the closed part as its own row, the open row shrunk."""
    with db_session.SessionLocal() as db:
        t = db.get(AlgoTrade, trade_id)
        if t is None or t.status != "open":
            return
        if closed:
            db.add(AlgoTrade(**closed))
        for k, v in open_fields.items():
            setattr(t, k, v)
        db.commit()


def _account(account_id: Optional[int]) -> tuple[Optional[BrokerAccount], Optional[str]]:
    """The real Fyers account a live strategy trades, or why it can't."""
    if not account_id:
        return None, "live mode needs a broker account"
    with db_session.SessionLocal() as db:
        acc = db.get(BrokerAccount, account_id)
        if acc is None:
            return None, f"broker account {account_id} not found"
        if acc.paper_mode:
            return None, "the chosen account is a paper account"
        # Not `acc.enabled`: that is the Dashboard's bot switch; each Algo
        # strategy has its own paper / live mode.
        if not acc.access_token:
            return None, f"account {acc.name!r} has no Fyers login"
        db.expunge(acc)
        return acc, None


def _strategy_account(strategy_id: int) -> Optional[int]:
    with db_session.SessionLocal() as db:
        s = db.get(AlgoStrategy, strategy_id)
        return s.account_id if s else None


# ---- broker helpers ------------------------------------------------------------

async def _ltp(symbols: list[str]) -> dict[str, float]:
    """Live prices from the Fyers tick stream (keeping those symbols streaming);
    REST quotes only for symbols with no fresh tick — so the 1 s exit loop
    doesn't spend the Fyers rate budget orders need."""
    from app.api.market import fyers_quotes
    from app.api.orders import _bus_quote_is_fresh, _fyers_stream, _is_simulated, _manager

    syms = sorted({x.upper() for x in symbols if x})
    out: dict[str, float] = {}
    stream = _fyers_stream()
    md = _manager().market_data
    for sym in syms:
        if stream is not None:
            try:
                stream.touch_interest(sym)
            except Exception:  # noqa: BLE001
                pass
        q = await md.get_quote(sym)
        if q is not None and q.last_price and _bus_quote_is_fresh(q) and not _is_simulated(q):
            out[sym] = float(q.last_price)
    missing = [x for x in syms if x not in out]
    if missing:
        qs = await fyers_quotes(missing)
        out.update({k: float(q.last_price) for k, q in qs.items() if q.last_price})
    return out


async def _broker_net(account: BrokerAccount, symbol: str) -> Optional[int]:
    """Signed net quantity the broker holds in `symbol`; None when unknown.

    Reads the raw client because the backend's get_positions() turns a failed
    call into [] — indistinguishable from flat, which is the one answer that
    must never be guessed here."""
    from app.api.orders import _manager

    try:
        backend = _manager()._manual_backend_for(account)  # noqa: SLF001
        payload = await backend._client.get_positions()     # noqa: SLF001 — raises on failure
    except Exception as e:  # noqa: BLE001
        log.warning("algo.positions_failed", error=str(e)[:200])
        return None
    rows = payload.get("netPositions") or payload.get("data") or []
    return sum(int(r.get("netQty") or r.get("qty") or 0) for r in rows
               if str(r.get("symbol") or "").upper() == symbol.upper())


def _product(spec: dict, lg: dict) -> str:
    """MIS for an intraday strategy; a carry-forward one (square-off off) needs
    CNC / NRML, or Fyers auto-squares the MIS position at ~15:20 anyway."""
    if engine.session_window(spec)[2] < engine.CARRY:
        return "INTRADAY"
    return "DELIVERY" if lg["kind"] == "EQ" else "MARGIN"


def _slices(symbol: str, qty: int) -> list[int]:
    """`qty` as orders the exchange accepts: at most its freeze quantity each,
    in whole lots (an index option above the freeze limit is refused whole)."""
    cap = fno.freeze_qty(symbol)
    if not cap or qty <= cap:
        return [int(qty)]
    lot = fno.contract_lot(symbol) or 1
    step = max(lot, cap // lot * lot)
    return [step] * (qty // step) + ([qty % step] if qty % step else [])


def _backend(account: BrokerAccount) -> Any:
    from app.api.orders import _manager

    return _manager()._manual_backend_for(account)  # noqa: SLF001


async def _order(account: BrokerAccount, symbol: str, side: str, qty: int, *, exit_: bool, strategy_id: int,
                 product: str = "INTRADAY", order_type: str = "MARKET",
                 limit_price: Optional[float] = None) -> tuple[bool, Optional[str], str]:
    """(ok, broker order id(s), message). Above the exchange's freeze quantity
    the order goes out in slices, ids joined by commas. An ENTRY whose later
    slice is refused cancels and unwinds the slices that went through — never
    half a position the caller doesn't know about; an EXIT keeps them (its
    retry re-reads what the broker still holds)."""
    ids: list[str] = []
    done = 0
    for q in _slices(symbol, int(qty)):
        ok, oid, msg = await _place(account, symbol, side, q, exit_=exit_, strategy_id=strategy_id, product=product,
                                    order_type=order_type, limit_price=limit_price)
        if not ok:
            if ids and not exit_:
                await _cancel(account, ",".join(ids))
                undo, _, why = await _place(account, symbol, "SELL" if side == "BUY" else "BUY", done, exit_=True,
                                            strategy_id=strategy_id, product=product)
                if not undo:
                    log.error("algo.slice_unwind_failed", symbol=symbol, qty=done, error=why)
                ids = []
            return False, ",".join(ids) or oid, msg
        ids.append(str(oid))
        done += q
    return True, ",".join(ids), msg


async def _order_chase(account: BrokerAccount, symbol: str, side: str, qty: int, *, strategy_id: int, product: str,
                       quote: float, wait_s: float, steps: int) -> tuple[bool, Optional[str], str]:
    """A live entry as a LIMIT at the quote, re-priced to the latest price
    `steps` times over `wait_s`, then turned MARKET for whatever is left: a
    chance at the quote instead of paying the spread every time, never a
    missed entry. The caller books the broker's average fill as usual.
    ponytail: the runner waits while it chases (exits run on their own tick
    task); bounded by wait_s <= 30."""
    from app.execution.base import OrderState, OrderType

    sgn = 1 if side == "BUY" else -1
    tick = engine.tick_for(symbol)
    ok, oids, msg = await _order(account, symbol, side, qty, exit_=False, strategy_id=strategy_id, product=product,
                                 order_type="LIMIT", limit_price=engine.snap(quote, tick, sgn))
    if not ok:
        return ok, oids, msg
    backend = _backend(account)
    for k in range(steps + 1):
        await asyncio.sleep(wait_s / (steps + 1))
        working = []
        for oid in str(oids).split(","):
            try:
                st = await backend.get_order_status(oid)
            except Exception as e:  # noqa: BLE001
                log.warning("algo.chase_status_failed", order_id=oid, error=str(e)[:200])
                continue
            if st.state == OrderState.PENDING:
                working.append(oid)
        if not working:
            return True, oids, "limit filled"
        px = None if k == steps else (await _ltp([symbol])).get(symbol.upper())
        for oid in working:
            try:
                if k == steps:
                    await backend.modify_order(oid, order_type=OrderType.MARKET, symbol=symbol)
                elif px:
                    await backend.modify_order(oid, limit_price=engine.snap(px, tick, sgn), symbol=symbol)
            except Exception as e:  # noqa: BLE001 — the broker's answer is read on the next pass / by the fill check
                log.warning("algo.chase_modify_failed", order_id=oid, error=str(e)[:200])
    return True, oids, "chased to market"


async def _place(account: BrokerAccount, symbol: str, side: str, qty: int, *, exit_: bool, strategy_id: int,
                 product: str = "INTRADAY", order_type: str = "MARKET",
                 limit_price: Optional[float] = None) -> tuple[bool, Optional[str], str]:
    """(ok, broker order id, message) — ONE order (MARKET, or LIMIT at `limit_price`)."""
    from app.api.orders import _manager, market_problem

    # The Trade page's market gate: crypto / currency never reach Fyers, MCX only with live MCX on and in whole
    # lots (this path skipped it: an MCX "equity" leg of 1 went out as 1 Fyers lot = 100 of ours).
    problem = market_problem(symbol, int(qty), paper=False)
    if problem:
        return False, None, problem
    try:
        r = await _manager().place_manual_order(
            account=account, symbol=symbol, side=side, quantity=int(qty), order_type=order_type,
            limit_price=limit_price, product_type=product, bypass_risk=exit_, operator=f"algo:{strategy_id}")
    except Exception as e:  # noqa: BLE001 — a broker fault is reported, never raised into the loop
        return False, None, str(e)[:300]
    if r.get("ok"):
        return True, r.get("broker_order_id"), str(r.get("status"))
    return False, r.get("broker_order_id"), str(r.get("error") or r.get("risk_message") or r.get("status"))


async def _fill_price(account: Optional[BrokerAccount], order_id: Optional[str], tries: int = 4) -> Optional[float]:
    """Average traded price of a live order — of all its slices, weighted by
    their filled quantity — polled briefly; None until every slice reports a
    fill (the quote then stands in)."""
    if account is None or not order_id:
        return None
    from app.execution.base import OrderState

    backend = _backend(account)
    for k in range(tries):
        try:
            sts = [await backend.get_order_status(oid) for oid in str(order_id).split(",")]
            if all(st.state == OrderState.FILLED and st.average_price for st in sts):
                q = sum(st.filled_quantity or 0 for st in sts)
                if len(sts) == 1 or not q:
                    return float(sts[0].average_price)
                return sum(float(st.average_price) * (st.filled_quantity or 0) for st in sts) / q
        except Exception as e:  # noqa: BLE001
            log.warning("algo.fill_price_failed", order_id=order_id, error=str(e)[:200])
            return None
        if k + 1 < tries:
            await asyncio.sleep(0.6)
    return None


async def _cancel(account: Optional[BrokerAccount], order_id: Optional[str]) -> None:
    """Cancel a still-working order before acting on it again: a late fill of
    an order we gave up on would be an unmanaged (or reversed) position. A
    cancel of an order that already filled is a harmless "no"."""
    if account is None or not order_id:
        return
    for oid in str(order_id).split(","):                  # every slice
        try:
            await _backend(account).cancel_order(oid)
        except Exception as e:  # noqa: BLE001
            log.warning("algo.cancel_failed", order_id=oid, error=str(e)[:200])


def _mtm(legs: list[dict], prices: dict[str, float]) -> Optional[float]:
    total = 0.0
    for lg in legs:
        px = lg.get("exit") if lg.get("exit") is not None else prices.get(lg["symbol"].upper())
        if px is None:
            return None
        total += lg["act"] * (px - lg["entry"]) * lg["qty"]
    return total


def _pick_expiry(expiries: list[int], kind: str, name: str, which: str) -> Optional[int]:
    """From the chain's listed expiries (epochs, ascending): monthly = the last
    listed expiry of each month."""
    exps = sorted(expiries)
    if kind == "monthly" or name not in fno.WEEKLY:
        month = lambda e: datetime.fromtimestamp(e, fno.IST).strftime("%Y%m")  # noqa: E731
        exps = [e for k, e in enumerate(exps) if k + 1 == len(exps) or month(exps[k + 1]) != month(e)]
    idx = 0 if which == "current" else 1
    return exps[idx] if len(exps) > idx else None


async def build_legs(spec: dict, sym: str, side: str, u: float, now: float) -> list[dict[str, Any]]:
    """The concrete contracts to trade for a signal, each with a live price.
    Raises ValueError with an operator-readable reason."""
    inst = spec["instrument"]
    sign = 1 if side == "BUY" else -1
    name = fno.fno_name(sym)
    if inst["type"] == "equity":
        return [{"symbol": sym, "kind": "EQ", "act": sign, "per_set": 1, "label": sym, "price": u}]
    def lot_of(contract: str, chain_lot: Any = None) -> int:
        # The contract's own lot: never a guessed 1 (the exchange rejects it),
        # and right for both sides of an NSE lot revision.
        lot = (fno.contract_lot(contract, now) or (int(chain_lot) if chain_lot else None)
               or fno.known_lot(name, now))
        if not lot:
            raise ValueError(f"lot size for {contract} unknown — F&O scrip master not loaded")
        return lot

    if inst["type"] == "future":
        fs = fno.future_symbol(name, inst["expiry"], now)
        if not fs:
            raise ValueError(f"no {inst['expiry']} {name} future in the F&O master")
        px = (await _ltp([fs])).get(fs)
        return [{"symbol": fs, "kind": "FUT", "act": sign, "per_set": lot_of(fs), "label": fs.split(":")[-1], "price": px}]
    backend = data._backend()
    cfgs = inst["legs_long" if side == "BUY" else "legs_short"]
    count = min(50, max([c["steps"] for c in cfgs] + [0]) + 3) \
        if all(c["strike"] not in ("PREMIUM", "DELTA") for c in cfgs) else 30
    chains: dict[str, tuple[int, list[dict], float, float]] = {}
    nearest: list[dict] = []

    async def chain_for(which: str) -> tuple[int, list[dict], float, float]:
        """(expiry, strike rows, strike step, spot) of the current / next expiry's chain, fetched once."""
        if which in chains:
            return chains[which]
        if not nearest:
            nearest.append(await backend.get_option_chain(sym, strikecount=count))
        chain = nearest[0]
        listed = sorted(int(e["ts"]) for e in chain.get("expiries") or [] if str(e.get("ts")).isdigit())
        exp = _pick_expiry(listed, inst["expiry_kind"], name, which)
        if exp is None:
            raise ValueError(f"no {which} {inst['expiry_kind']} expiry listed for {name}")
        if listed and exp != listed[0]:
            chain = await backend.get_option_chain(sym, strikecount=count, timestamp=str(exp))
        rows = [r for r in chain.get("strikes") or [] if r.get("strike")]
        if not rows:
            raise ValueError(f"empty option chain for {name}")
        strikes = sorted(r["strike"] for r in rows)
        step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=fno.default_step(name, u))
        chains[which] = (exp, rows, step, chain.get("spot") or nearest[0].get("spot") or u)
        return chains[which]

    legs = []
    for src, c in enumerate(cfgs):
        if not c["lots"]:
            continue
        if c["right"] == "FUT":
            fs = fno.future_symbol(name, inst["expiry"], now)
            if not fs:
                raise ValueError(f"no {inst['expiry']} {name} future in the F&O master")
            legs.append({"symbol": fs, "kind": "FUT", "act": 1 if c["action"] == "BUY" else -1, "src": src,
                         "per_set": lot_of(fs) * c["lots"], "label": fs.split(":")[-1],
                         "price": (await _ltp([fs])).get(fs)})
            continue
        exp, rows, step, spot = await chain_for(c.get("expiry") or inst["expiry"])
        key = c["right"].lower()
        if c["strike"] in ("PREMIUM", "DELTA"):
            cands = [r for r in rows if (r.get(key) or {}).get("ltp")]
            if not cands:
                raise ValueError(f"no {c['right']} premiums in the chain")
            if c["strike"] == "PREMIUM":
                row = min(cands, key=lambda r: abs(r[key]["ltp"] - c["premium"]))
            else:                       # |delta| from each strike's own implied vol
                yrs = fno.years_to(exp, now)
                dlt = lambda r: abs(fno.bs_greeks(spot, r["strike"], yrs, fno.implied_vol(  # noqa: E731
                    r[key]["ltp"], spot, r["strike"], yrs, c["right"]), c["right"])["delta"])
                row = min(cands, key=lambda r: abs(dlt(r) - c["delta"]))
        else:
            k_ = fno.pick_strike(spot, step, c["right"], c["strike"], c["steps"])
            row = min(rows, key=lambda r: abs(r["strike"] - k_))
        leg = row.get(key) or {}
        if not leg.get("symbol"):
            raise ValueError(f"{name} {row['strike']:g} {c['right']} not in the chain")
        legs.append({"symbol": leg["symbol"].upper(), "kind": c["right"], "act": 1 if c["action"] == "BUY" else -1,
                     "per_set": lot_of(leg["symbol"], leg.get("lot_size")) * c["lots"],
                     "label": leg["symbol"].split(":")[-1],
                     "price": leg.get("ltp"), "K": row["strike"], "exp": exp, "src": src,
                     **{k: c[k] for k in ("sl_pct", "tg_pct") if c.get(k)}})
    return legs


def _notify_entry(payload: dict[str, Any]) -> None:
    from app.services.event_bus import event_bus

    asyncio.ensure_future(event_bus.publish("algo.entry", payload))


def _notify_exit(payload: dict[str, Any]) -> None:
    from app.services.event_bus import event_bus

    asyncio.ensure_future(event_bus.publish("trade.closed", payload))


def _syms(t: dict) -> set[str]:
    """Every symbol a position's exits read: its legs and the underlying."""
    return {lg["symbol"].upper() for lg in t["legs"]} | {t["symbol"].upper()}


class AlgoRunner:
    TICK_S = 1.0          # candle entries + an exit sweep on REST-backed LTP every second
    FAST_GAP_S = 0.05     # tick-driven exits: at most one pass per 50 ms, however fast ticks arrive
    TICK_MAX_AGE_S = 30.0  # a leg's last streamed price older than this waits for the 1 s sweep

    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=300)
        self.last_tick: Optional[float] = None
        self.last_sync: Optional[dict[str, Any]] = None
        self._last_bar: dict[tuple[int, str], int] = {}
        self._retry_at: dict[int, float] = {}
        self._sync_day: Optional[int] = None
        self._master_day: Optional[int] = None
        self._halted: set[tuple[int, int]] = set()
        # Pullback / breakout entries waiting for their trigger price.
        # ponytail: in memory — a restart drops pending triggers (they expire
        # within a few bars anyway); persist them if that ever costs a trade.
        self._armed: dict[tuple[int, str], dict[str, Any]] = {}
        self._last_tc: dict[tuple[int, str], int] = {}   # volume/turnover: last candle close acted on
        self._busy: set[int] = set()     # trades with an exit / scale-out in flight
        self._guard_at = 0.0             # last time the guards were read
        self._book_halt: Optional[int] = None   # IST day the book loss limit (or a kill) stopped every automation
        self._alloc: tuple[Optional[int], dict[int, dict[str, Any]]] = (None, {})   # (IST day, allocation)
        self._names: dict[int, str] = {}
        # Tick-driven exits: every websocket tick for a symbol an open position
        # holds wakes `_tick_loop`, which runs the same stop / target / trail
        # checks on that tick's price — no waiting for the next 1 s sweep.
        # One lock serialises both paths so neither works on a stale copy of
        # a trade's trail / best price.
        self._mlock = asyncio.Lock()
        self._open: list[dict[str, Any]] = []
        self._by_id: dict[int, dict[str, Any]] = {}
        self._watch: set[str] = set()
        self._live_px: dict[str, tuple[float, float]] = {}   # symbol -> (price, received)
        self._dirty: set[str] = set()
        self._wake: Optional[asyncio.Event] = None
        self.ticks_seen = 0
        self.last_tick_exit: Optional[float] = None

    def event(self, level: str, msg: str, **kw: Any) -> None:
        self.events.appendleft({"t": time.time(), "level": level, "msg": msg, **kw})
        getattr(log, "warning" if level == "error" else "info")("algo.event", msg=msg, **kw)

    def label(self, sid: int, version: Optional[int] = None) -> str:
        """"name v3" for the runner log, so every line says whose trade it is."""
        name = self._names.get(sid) or f"strategy #{sid}"
        return f"{name} v{version}" if version else name

    async def run(self) -> None:
        log.info("algo_runner.start")
        bus = self._listen()
        fast = asyncio.create_task(self._tick_loop(), name="algo-tick-exits")
        try:
            while True:
                await asyncio.sleep(self.TICK_S)
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 — never kill the loop
                    log.exception("algo_runner.tick_failed")
                    self.event("error", f"tick failed: {e}"[:300])
        finally:
            fast.cancel()
            if bus is not None:
                bus.remove_tick_listener(self.on_quote)

    def _listen(self) -> Any:
        try:
            from app.api.orders import _manager

            bus = _manager().market_data
            bus.add_tick_listener(self.on_quote)
            return bus
        except Exception:  # noqa: BLE001 — the 1 s sweep still manages exits
            log.exception("algo_runner.tick_listen_failed")
            return None

    def on_quote(self, q: Any) -> None:
        """Market-data bus listener (runs inside the publisher — keep it cheap)."""
        sym = (q.symbol or "").upper()
        if sym not in self._watch or not q.last_price or (q.extra or {}).get("simulated"):
            return
        self._live_px[sym] = (float(q.last_price), time.time())
        self._dirty.add(sym)
        self.ticks_seen += 1
        if self._wake is not None:
            self._wake.set()

    async def _tick_loop(self) -> None:
        self._wake = asyncio.Event()
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.on_ticks()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never kill the loop
                log.exception("algo_runner.tick_exit_failed")
                self.event("error", f"tick exit check failed: {e}"[:300])
            await asyncio.sleep(self.FAST_GAP_S)     # coalesce a burst of ticks into one pass

    async def on_ticks(self, now: Optional[float] = None) -> None:
        """Run the exit checks of every open position a fresh tick touched, on
        the streamed prices. A position with a leg that has no recent streamed
        price is left to the 1 s sweep (which falls back to REST quotes)."""
        dirty, self._dirty = self._dirty, set()
        now = now or time.time()
        day = int((now + IST) // 86400)
        mins = int((now + IST) % 86400) // 60
        if not dirty or (day + 3) % 7 >= 5 or not OPEN_MIN <= mins < CLOSE_MIN:
            return
        async with self._mlock:
            hit = [t for t in self._open if t.get("status") == "open" and dirty & _syms(t)]
            prices = {sym: px for sym, (px, at) in self._live_px.items() if now - at <= self.TICK_MAX_AGE_S}
            ready = [t for t in hit if _syms(t) <= prices.keys()]
            if ready:
                self.last_tick_exit = now
                await self._manage(ready, self._by_id, now, mins, day * 86400 - IST, prices=prices, book=self._open)

    async def tick(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.last_tick = now
        day = int((now + IST) // 86400)
        if (day + 3) % 7 >= 5:                 # Saturday / Sunday
            return
        mins = int((now + IST) % 86400) // 60
        day_start = day * 86400 - IST
        async with self._mlock:      # the tick path waits: this pass re-reads every trade from the DB
            strategies, open_trades = await asyncio.to_thread(_snapshot)
            by_id = {s["id"]: s for s in strategies}
            self._names = {s["id"]: s["name"] for s in strategies}
            # Only MIS legs are auto-squared by the broker; a CNC / NRML leg (even
            # of a strategy since edited or deleted) is still open at Fyers.
            stale = [t for t in open_trades if _naive(day_start) > t["entry_at"].replace(tzinfo=None)
                     and not _carries(by_id.get(t["strategy_id"]))
                     and all(lg.get("product", "INTRADAY") == "INTRADAY" for lg in t["legs"])]
            if stale:
                await self._close_stale(stale)
                open_trades = [t for t in open_trades if t not in stale]
            if mins >= OPEN_MIN:
                await self._manage(open_trades, by_id, now, min(mins, CLOSE_MIN), day_start)  # past close: every square-off
            self._open, self._by_id = open_trades, by_id
            self._watch = {x for t in open_trades if t.get("status") == "open" for x in _syms(t)}
            self._live_px = {k: v for k, v in self._live_px.items() if k in self._watch}
        # Record real ticks for everything an enabled strategy trades.
        from app.algo import ticks

        ticks.set_strategy_symbols({x for s in strategies if s["enabled"] for x in s["spec"]["symbols"]})
        if self._master_day != day and any(s["spec"]["instrument"]["type"] != "equity" for s in strategies):
            self._master_day = day
            try:
                await fno.ensure_master()
            except Exception as e:  # noqa: BLE001
                self.event("error", f"F&O master refresh failed: {e}"[:200])
        if OPEN_MIN <= mins < CLOSE_MIN:
            await self._guards(strategies, now, day_start)
            if self._armed:
                await self._check_armed(by_id, now, day, day_start)
            for s in strategies:
                if s["enabled"] and (s["id"], day) not in self._halted:
                    await self._on_bar(s, now, day_start)
        elif mins >= CLOSE_MIN:
            if mins >= SYNC_AFTER_MIN and self._sync_day != day and strategies:
                self._sync_day = day
                await self.sync(strategies, days=5)

    # -- exits ---------------------------------------------------------------

    async def _manage(self, trades: list[dict], by_id: dict, now: float, mins: int, day_start: float,
                      prices: Optional[dict[str, float]] = None, book: Optional[list[dict]] = None) -> None:
        """`prices`: the streamed tick prices (tick path); None reads LTPs (1 s sweep).
        `book`: every open trade, when `trades` is only the ones a tick touched —
        the daily max loss / profit is on a strategy's whole book, never part of it."""
        if not trades:
            return
        if prices is None:
            syms = {lg["symbol"] for t in trades for lg in t["legs"]} | {t["symbol"] for t in trades}
            prices = await _ltp(list(syms))
        for t in trades:
            s = by_id.get(t["strategy_id"])
            spec = s["spec"] if s else None
            sq = engine.session_window(spec)[2] if spec else 15 * 60 + 15
            if mins >= sq:
                await self._exit(t, prices, "SQUARE_OFF", now)
                continue
            reason = None
            if any(lg.get("exit") is None and (lg.get("sl") is not None or lg.get("tg") is not None)
                   for lg in t["legs"]):
                reason = await self._leg_exits(t, spec, prices, now)
            reason = reason or await self._check(t, spec, prices, now)
            if reason:
                await self._exit(t, prices, reason, now)
            elif t.get("scale") and t["scale"]["levels"]:
                await self._scale_out(t, prices, now)
        day = int((now + IST) // 86400)
        for sid in {t["strategy_id"] for t in trades}:
            s = by_id.get(sid)
            if not s or not (s["spec"]["daily"]["max_loss"] or s["spec"]["daily"]["max_profit"]):
                continue
            mine = [t for t in (trades if book is None else book) if t["strategy_id"] == sid and t["status"] == "open"]
            opens = [_mtm(t["legs"], prices) for t in mine]
            if any(m is None for m in opens):
                continue
            pnl = (await asyncio.to_thread(_book, sid, day_start))["today"] + sum(opens)
            dl = s["spec"]["daily"]
            hit = ("DAILY_MAX_LOSS" if dl["max_loss"] and pnl <= -dl["max_loss"] else
                   "DAILY_TARGET" if dl["max_profit"] and pnl >= dl["max_profit"] else None)
            if hit:
                self._halted.add((sid, day))
                self.event("exit", f"{s['name']}: {hit} (day P&L ₹{pnl:.0f}) — flattening, no more entries today",
                           strategy_id=sid)
                for t in mine:
                    await self._exit(t, prices, hit, now)
        lim = get_settings().ALGO_BOOK_MAX_DAY_LOSS
        if lim and self._book_halt != day:
            every = [t for t in (trades if book is None else book) if t["status"] == "open"
                     and not (by_id.get(t["strategy_id"]) or {}).get("shadow_of")]
            marks = [_mtm(t["legs"], prices) for t in every]
            if None not in marks:
                pnl = (await asyncio.to_thread(_book_all, day_start))["today"] + sum(marks)
                if pnl <= -lim:
                    await self.halt_book(day, f"book day loss ₹{-pnl:,.0f} reached the ₹{lim:,.0f} limit",
                                         every, prices, now, "BOOK_MAX_LOSS")

    async def allocated(self, now: float) -> dict[int, dict[str, Any]]:
        """Today's capital allocation (ALGO_ALLOC_CAPITAL), worked out once a
        day — and again when the setting changes."""
        day = int((now + IST) // 86400)
        key = (day, get_settings().ALGO_ALLOC_CAPITAL)
        if self._alloc[0] != key:
            strategies, _ = await asyncio.to_thread(_snapshot)
            self._alloc = (key, await asyncio.to_thread(allocation, strategies, now))
        return self._alloc[1]

    async def halt_book(self, day: int, why: str, trades: list[dict], prices: dict[str, float], now: float,
                        reason: str) -> None:
        """Every automation stops entering for the rest of `day` and every open
        automation position is flattened (the book loss limit, or the
        operator's kill). Alerted as a risk halt."""
        self._book_halt = day
        msg = f"ALL AUTOMATIONS halted for today — {why}; flattening {len(trades)} position(s)"
        self.event("error", msg)
        from app.services.event_bus import event_bus

        asyncio.ensure_future(event_bus.publish("risk.halt", {"code": reason, "message": msg}))
        for t in trades:
            await self._exit(t, prices, reason, now)

    async def _guards(self, strategies: list[dict], now: float, day_start: float) -> None:
        """Each enabled strategy's guard (spec "guard"), once a minute: a breach
        switches it off — no new entries; its open positions stay managed, or
        are flattened with on_breach "flatten" — with an event, an audit row and
        a risk-halt alert. Switching it back on is the operator's call."""
        if now - self._guard_at < GUARD_EVERY_S:
            return
        self._guard_at = now
        for s in strategies:
            g = s["spec"].get("guard")
            if not (s["enabled"] and g):
                continue
            mine = [t for t in self._open if t["strategy_id"] == s["id"] and t.get("status") == "open"]
            prices = await _ltp(list({lg["symbol"] for t in mine for lg in t["legs"]})) if mine else {}
            marks = [_mtm(t["legs"], prices) for t in mine]
            why = guard_breach(g, await asyncio.to_thread(_guard_stats, s["id"], day_start),
                               sum(m for m in marks if m is not None))
            if not why or not await asyncio.to_thread(_guard_off, s["id"], why):
                continue
            s["enabled"] = False
            flatten = g.get("on_breach") == "flatten"
            msg = f"{s['name']}: GUARD switched it off — {why}" + (
                "; flattening its positions" if flatten else "; its open positions stay managed")
            self.event("error", msg, strategy_id=s["id"])
            from app.services.event_bus import event_bus

            asyncio.ensure_future(event_bus.publish("risk.halt", {"code": "ALGO_GUARD", "message": msg,
                                                                   "symbol": s["name"]}))
            for t in mine if flatten else []:
                await self._exit(t, prices, "GUARD", now)

    async def _check(self, t: dict, spec: Optional[dict], prices: dict[str, float], now: float) -> Optional[str]:
        """Update trailing / breakeven / MTM peak and return an exit reason, if any."""
        legs = t["legs"]
        sign = t["ref_sign"] or (1 if t["side"] == "BUY" else -1)
        ref_px = prices.get((t["symbol"] if t["ref"] == "u" else legs[0]["symbol"]).upper())
        changed: dict[str, Any] = {}
        reason = None
        if ref_px is not None and t["entry_price"]:
            best = t["best_price"] or t["entry_price"]
            best = max(best, ref_px) if sign > 0 else min(best, ref_px)
            if best != t["best_price"]:
                changed["best_price"] = best
            gain = sign * (best - t["entry_price"])
            if t["trail_dist"] and gain >= (t["trail_activate"] or 0):
                nt = engine.snap(best - sign * t["trail_dist"], engine.tick_for(t["symbol"]))
                old = t["trail_stop"]
                trail = nt if old is None else (max(old, nt) if sign > 0 else min(old, nt))
                if trail != old:
                    changed["trail_stop"] = trail
            if t["breakeven"] and not t["be_on"] and gain >= t["breakeven"]:
                changed["be_on"] = True
            t.update(changed)
            stops = [x for x in (t["stop_loss"], t["trail_stop"], t["entry_price"] if t["be_on"] else None)
                     if x is not None]
            stop = (max(stops) if sign > 0 else min(stops)) if stops else None
            if stop is not None and sign * (ref_px - stop) <= 0:
                reason = ("TRAIL" if stop == t["trail_stop"] else
                          "BREAKEVEN" if t["be_on"] and stop == t["entry_price"] and stop != t["stop_loss"] else "SL")
            elif t["target"] is not None and sign * (ref_px - t["target"]) >= 0:
                reason = "TARGET"
        m = _mtm(legs, prices)
        if spec and m is not None and not reason:
            r = spec["mtm"]
            m_stop, m_target = engine.mtm_limits(r, legs)      # ₹, or the tighter % of entry premium
            peak = m if t["mtm_peak"] is None else max(t["mtm_peak"], m)
            if peak != t["mtm_peak"]:
                changed["mtm_peak"] = t["mtm_peak"] = peak
            if m_stop and m <= -m_stop:
                reason = "MTM_SL"
            elif r["trail_start"] and peak >= r["trail_start"] and m <= peak - r["trail_gap"]:
                reason = "MTM_TRAIL"
            elif m_target and m >= m_target:
                reason = "MTM_TARGET"
        if spec and not reason and spec["max_bars"]:
            if now >= _epoch(t["entry_at"]) + spec["max_bars"] * engine.bar_minutes(spec) * 60:
                reason = "TIME_STOP"
        if changed:
            await asyncio.to_thread(_update, t["id"], **{k: v for k, v in changed.items()})
        return reason

    async def _leg_exits(self, t: dict, spec: Optional[dict], prices: dict[str, float], now: float) -> Optional[str]:
        """Each leg's own stop / target: a leg whose premium reached its level is
        closed on its own; the rest of the position stays open (a stop can move
        the other legs' stops to cost). Closed legs keep their fill in the open
        trade, so its MTM and the final booking include them. Returns a reason
        when the whole position should now exit: every leg is done, or the
        strategy exits all legs on a leg's stop."""
        if self._retry_at.get(t["id"], 0) > now or t["id"] in self._busy:
            return None
        on = (spec["instrument"].get("on_leg_stop") if spec else None) or "leg"
        self._busy.add(t["id"])
        try:
            fresh = await asyncio.to_thread(_fresh, t["id"])
            if fresh is None or fresh["status"] != "open":
                return None
            t.update(fresh)
            hits = [(lg, why) for lg in t["legs"] if lg.get("exit") is None
                    and (px := prices.get(lg["symbol"].upper())) is not None and (why := engine.leg_hit(lg, px))]
            if not hits:
                return None
            if on == "all" and any(why != "TARGET" for _, why in hits):
                return "LEG_SL"
            last = None
            for lg, why in sorted(hits, key=lambda h: h[0]["act"]):          # written legs first
                reason = f"LEG_{why}"
                r = await self._close_leg(t, lg, lg["qty"], prices[lg["symbol"].upper()], reason)
                if r is None:
                    self._retry_at[t["id"]] = now + 30
                    break
                lg["exit"], lg["exit_reason"] = r["px"], reason
                if r["oid"]:
                    lg["exit_order_id"] = r["oid"]
                if r["external"]:
                    lg["external"] = True
                if why != "TARGET" and on == "cost":
                    engine.legs_to_cost(t["legs"])
                last = reason
                self.event("exit", f"{self.label(t['strategy_id'], t.get('version'))}: "
                                   f"{reason} {lg.get('label') or lg['symbol']} @ {r['px']:.2f} — the rest of "
                                   f"{t.get('instrument') or t['symbol']} stays open", trade_id=t["id"],
                           strategy_id=t["strategy_id"])
            await asyncio.to_thread(_update, t["id"], legs=t["legs"])
        finally:
            self._busy.discard(t["id"])
        if all(lg.get("exit") is not None for lg in t["legs"]):
            return last or "LEG_SL"
        return None

    async def _last_close(self, symbol: str) -> Optional[float]:
        try:
            b = await data.recent_bars(symbol, 1, bars=1)
            return b["c"][-1] if b["c"] else None
        except Exception:  # noqa: BLE001
            return None

    async def _close_stale(self, trades: list[dict]) -> None:
        """A position from an earlier session (the server was down at square-off).
        The broker auto-squares MIS; book it at the previous close so P&L is real."""
        from app.api.market import fyers_quotes

        qs = await fyers_quotes(sorted({lg["symbol"] for t in trades for lg in t["legs"]}))
        for t in trades:
            for lg in t["legs"]:
                if lg.get("exit") is None:
                    q = qs.get(lg["symbol"].upper())
                    lg["exit"] = (q.prev_close if q is not None and q.prev_close else None) or lg["entry"]
            await self._book(t, "STALE_SESSION", time.time())

    async def _exit(self, t: dict, prices: dict[str, float], reason: str, now: float) -> bool:
        """Close every remaining leg. Partial progress (a leg that filled) is
        saved, so a retry only touches what is still open."""
        if self._retry_at.get(t["id"], 0) > now or t["id"] in self._busy:
            return False
        self._busy.add(t["id"])
        try:
            fresh = await asyncio.to_thread(_fresh, t["id"])
            if fresh is not None and fresh["status"] != "open":
                t["status"] = fresh["status"]
                return True                             # already closed (e.g. by a concurrent exit)
            if fresh is not None:
                t.update(fresh)                         # the DB's legs: a scale-out may have shrunk them
            return await self._exit_legs(t, prices, reason, now)
        finally:
            self._busy.discard(t["id"])

    async def _exit_legs(self, t: dict, prices: dict[str, float], reason: str, now: float) -> bool:
        legs = t["legs"]
        todo = sorted([lg for lg in legs if lg.get("exit") is None], key=lambda lg: lg["act"])  # written legs first
        for lg in todo:
            px = prices.get(lg["symbol"].upper())
            if px is None:
                px = await self._last_close(lg["symbol"])
            if px is None:
                self._retry_at[t["id"]] = now + 15
                self.event("error", f"{self.label(t['strategy_id'], t.get('version'))}: {t['symbol']}: no price "
                                    f"for {lg['symbol']} — exit ({reason}) retried", trade_id=t["id"],
                           strategy_id=t["strategy_id"])
                return False
            r = await self._close_leg(t, lg, lg["qty"], px, reason)
            if r is None:
                self._retry_at[t["id"]] = now + 30
                return False
            lg["exit"] = r["px"]
            if r["oid"]:
                lg["exit_order_id"] = r["oid"]
            if r["external"]:
                lg["external"] = True                   # already flat at the broker: no order was sent
        await self._book(t, reason, now)
        return True

    async def _close_leg(self, t: dict, lg: dict, qty: int, px: float, reason: str) -> Optional[dict[str, Any]]:
        """Exit `qty` of one leg — paper: at `px`; live: a MARKET order, never
        for more than the broker holds. {"px", "qty", "oid", "external"}
        (external = the broker no longer holds it: no order sent), or None when
        it failed — the trade note and an event say why, nothing is booked."""
        lg["exit_quote"] = px                       # what the exit was decided at (TCA: fill vs quote)
        if t["mode"] != "live":
            return {"px": px, "qty": qty, "oid": None, "external": False}
        acc, why = await asyncio.to_thread(_account, await asyncio.to_thread(_strategy_account, t["strategy_id"]))
        net = await _broker_net(acc, lg["symbol"]) if acc is not None else None
        held = net if net is not None and (net > 0) == (lg["act"] > 0) else 0
        if acc is not None and net is not None and held == 0:
            return {"px": px, "qty": 0, "oid": None, "external": True}
        ok, oid, msg = (False, None, why or "positions unavailable") if acc is None or net is None \
            else await _order(acc, lg["symbol"], "SELL" if lg["act"] > 0 else "BUY",
                              min(qty, abs(held)), exit_=True, strategy_id=t["strategy_id"],
                              product=lg.get("product", "INTRADAY"))
        if not ok:
            await asyncio.to_thread(_update, t["id"], legs=t["legs"], note=f"exit failed: {msg}"[:500])
            self.event("error", f"{self.label(t['strategy_id'], t.get('version'))}: EXIT FAILED {lg['symbol']} "
                                f"({reason}): {msg}", trade_id=t["id"], strategy_id=t["strategy_id"])
            return None
        fill = await _fill_price(acc, oid)         # the broker's average, when it reports one
        if fill is None:
            # Accepted is not filled: a reject after the ack leaves the
            # position open. Booked only once the broker holds less.
            after = await _broker_net(acc, lg["symbol"])
            if after is None or (after * lg["act"] > 0 and abs(after) >= abs(held)):
                await _cancel(acc, oid)            # else the retry's order doubles it into a reversal
                await asyncio.to_thread(_update, t["id"], legs=t["legs"], note=f"exit {oid} not filled yet"[:500])
                self.event("error", f"{self.label(t['strategy_id'], t.get('version'))}: EXIT NOT FILLED "
                                    f"{lg['symbol']} ({reason})", trade_id=t["id"], strategy_id=t["strategy_id"])
                return None
        return {"px": fill or px, "qty": min(qty, abs(held)), "oid": oid, "external": False}

    async def _scale_out(self, t: dict, prices: dict[str, float], now: float) -> None:
        """Take the next scale-out level once the reference price reaches it:
        close that many sets of every leg, book them as their own closed row
        and shrink the open one.

        Safety: the level is committed as taken BEFORE any order, so a crash
        or error later can never re-send it; written legs go first and the
        first leg that can't be reduced stops the step — a hedge is never sold
        while the short it protects is still on. What isn't reduced stays in
        the open position for its final exit. A per-trade lock keeps a manual
        square-off (or any exit) from running in the middle of it."""
        sc = t["scale"]
        lvl, n = sc["levels"][0]
        sign = t["ref_sign"] or (1 if t["side"] == "BUY" else -1)
        ref_px = prices.get((t["symbol"] if t["ref"] == "u" else t["legs"][0]["symbol"]).upper())
        if ref_px is None or sign * (ref_px - lvl) < 0 or self._retry_at.get(t["id"], 0) > now \
                or t["id"] in self._busy:
            return
        k, left = sc["done"] + 1, sc["left"]
        reason = f"SCALE_OUT_{k}"
        if n >= left:                                   # the last sets: an ordinary full exit
            await self._exit(t, prices, reason, now)
            return
        self._busy.add(t["id"])
        try:
            await self._scale_part(t, prices, now, reason, n, left, k)
        finally:
            self._busy.discard(t["id"])

    async def _scale_part(self, t: dict, prices: dict[str, float], now: float, reason: str,
                          n: int, left: int, k: int) -> None:
        fresh = await asyncio.to_thread(_fresh, t["id"])
        if fresh is None or fresh["status"] != "open" or fresh["scale"] != t["scale"]:
            return                                      # closed, or this level was taken meanwhile
        t.update(fresh)
        sc = t["scale"]
        t["scale"] = {**sc, "left": left - n, "levels": sc["levels"][1:], "done": k}
        await asyncio.to_thread(_update, t["id"], scale=t["scale"])        # taken: never re-sent
        legs = t["legs"]
        sets0 = sc.get("sets") or left
        # Whole sets of the ENTRY size, so a leg left over-sized by an earlier
        # failed step still closes in whole lots.
        q_of = lambda lg: min(lg["qty"], (lg.get("qty0") or lg["qty"]) // sets0 * n)  # noqa: E731
        todo = [j for j in sorted(range(len(legs)), key=lambda j: legs[j]["act"])   # written legs first
                if legs[j].get("exit") is None and q_of(legs[j]) > 0]
        done: dict[int, dict] = {}
        for j in todo:
            lg = legs[j]
            px = prices.get(lg["symbol"].upper())
            r = await self._close_leg(t, lg, q_of(lg), px, reason) if px is not None else None
            if r is None:
                break                                   # stop: never reduce a hedge past its short
            if not r["qty"]:
                continue                                # the broker no longer holds it
            lg.setdefault("qty0", lg["qty"])
            lg["qty"] -= r["qty"]
            done[j] = {**{x: lg[x] for x in ("symbol", "kind", "act", "entry", "label", "K", "exp", "product", "qty0",
                                             "entry_quote", "exit_quote")
                          if x in lg},
                       "qty": r["qty"], "exit": r["px"], **({"exit_order_id": r["oid"]} if r["oid"] else {})}
        part = [done[j] for j in sorted(done)]
        closed, gross, ch = None, 0.0, 0.0
        if part:
            for p in part:
                g, c = engine.leg_pnl(p["act"], p["qty"], p["entry"], p["exit"], p["kind"], True, p.get("qty0"))
                gross, ch = gross + g, ch + c
            closed = dict(
                strategy_id=t["strategy_id"], symbol=t["symbol"], side=t["side"], quantity=part[0]["qty"],
                mode=t["mode"], status="closed", entry_at=t["entry_at"], entry_price=t["entry_price"],
                stop_loss=t["stop_loss"], target=t["target"], exit_at=_utc(now), exit_price=round(part[0]["exit"], 2),
                exit_reason=reason, gross_pnl=round(gross, 2), charges=round(ch, 2), net_pnl=round(gross - ch, 2),
                instrument=t.get("instrument"), legs=part, ref=t["ref"], ref_sign=t["ref_sign"],
                margin=round((t["margin"] or 0) * n / left, 2), u_entry=t.get("u_entry"), version=t.get("version"),
                entry_order_id=t.get("entry_order_id"), note=f"{PARTIAL_NOTE}{t['id']}",
                exit_order_id=",".join(str(p["exit_order_id"]) for p in part if p.get("exit_order_id"))[:64] or None)
            t["margin"] = round((t["margin"] or 0) * (left - n) / left, 2)
            if t.get("mtm_peak") is not None:           # the MTM trail follows what is still open
                t["mtm_peak"] = t["mtm_peak"] * (left - n) / left
        await asyncio.to_thread(_split, t["id"], closed, legs=legs, quantity=legs[0]["qty"],
                                margin=t["margin"], mtm_peak=t.get("mtm_peak"))
        if part:
            self.event("exit", f"{self.label(t['strategy_id'], t.get('version'))}: "
                               f"{reason} {t.get('instrument') or t['symbol']}: {n} of {left} set(s) closed, "
                               f"net ₹{gross - ch:.2f}", trade_id=t["id"], strategy_id=t["strategy_id"])
            _notify_exit({"symbol": t.get("instrument") or t["symbol"], "reason": reason, "quantity": part[0]["qty"],
                          "entry": part[0]["entry"], "exit": part[0]["exit"], "pnl": round(gross - ch, 2)})
        if len(part) < len(todo):
            self.event("error", f"{self.label(t['strategy_id'], t.get('version'))}: "
                                f"{reason} {t['symbol']}: {len(todo) - len(part)} leg(s) not reduced — they stay in "
                                "the open position for its final exit", trade_id=t["id"], strategy_id=t["strategy_id"])

    async def _adjust(self, s: dict, t: dict, sym: str, now: float) -> None:
        """Re-entries, rolls and hedges on a candle's close — engine.adjustments,
        the backtest's own rule. Safety: a rolled leg closes BEFORE its
        replacement opens, new BUY legs open before SELL legs, every step is
        saved as it lands, and a failed open stops the rest of this candle's
        adjustments (an event + note) — it never unwinds what is already on."""
        spec = s["spec"]
        mins = int((now + IST) % 86400) // 60
        _, end, sq = engine.session_window(spec)
        if mins >= sq or t["id"] in self._busy or self._retry_at.get(t["id"], 0) > now:
            return
        self._busy.add(t["id"])
        try:
            fresh = await asyncio.to_thread(_fresh, t["id"])
            if fresh is None or fresh["status"] != "open":
                return
            t.update(fresh)
            prices = await _ltp([lg["symbol"] for lg in t["legs"] if lg.get("exit") is None] + [sym])
            u = prices.get(sym.upper())
            if u is None:
                return
            acts = engine.adjustments(spec, t["side"], t["legs"], u, now, fno.known_lot(fno.fno_name(sym), now) or 1,
                                      lambda lg: prices.get(lg["symbol"].upper()), None, mins <= end)
            name = self.label(t["strategy_id"], t.get("version"))
            for kind, j, _ in acts:                     # closes first: a roll never stacks a second short
                lg = t["legs"][j]
                if kind != "roll" or prices.get(lg["symbol"].upper()) is None:
                    continue
                r = await self._close_leg(t, lg, lg["qty"], prices[lg["symbol"].upper()], "LEG_ROLL")
                if r is None:
                    continue                            # still on: no replacement for it
                lg["exit"], lg["exit_reason"] = r["px"], "LEG_ROLL"
                if r["oid"]:
                    lg["exit_order_id"] = r["oid"]
                await asyncio.to_thread(_update, t["id"], legs=t["legs"])
            first = t["legs"][0]
            sets = max(1, first["qty"] // (first.get("per_set") or first["qty"]))
            for kind, j, cfg in sorted(acts, key=lambda a: a[2]["action"] != "BUY"):      # bought legs first
                if kind == "roll" and t["legs"][j].get("exit") is None:
                    continue
                new = await self._add_leg(t, spec, sym, cfg, u, now, 1 if kind == "hedge" else sets)   # hedge: total lots
                if new is None:
                    break
                via = {"reenter": "REENTRY", "roll": "ROLL", "hedge": "HEDGE"}[kind]
                new["via"] = via
                if kind == "hedge":
                    new.pop("src", None)
                    new["hedge"] = True
                else:
                    new["src"] = t["legs"][j].get("src")
                    if kind == "reenter":
                        t["legs"][j]["redone"] = True
                t["legs"].append(new)
                await asyncio.to_thread(_update, t["id"], legs=t["legs"])
                self.event("entry", f"{name}: {via} {'BUY' if new['act'] > 0 else 'SELL'} {new['qty']} {new['label']} "
                                    f"@ {new['entry']:.2f}", trade_id=t["id"], strategy_id=t["strategy_id"])
        finally:
            self._busy.discard(t["id"])

    async def _add_leg(self, t: dict, spec: dict, sym: str, cfg: dict, u: float, now: float,
                       sets: int) -> Optional[dict[str, Any]]:
        """Open one more leg of an open position (paper: at the quote; live: a
        MARKET order on the strategy's account, booked at the broker's fill).
        None when it couldn't — the event and the trade note say why."""
        key = "legs_long" if t["side"] == "BUY" else "legs_short"
        one = {**spec, "instrument": {**spec["instrument"], key: [{**cfg, "lots": cfg.get("lots") or 1}]}}
        name = self.label(t["strategy_id"], t.get("version"))

        async def fail(why: str) -> None:
            await asyncio.to_thread(_update, t["id"], note=f"adjustment failed: {why}"[:500])
            self.event("error", f"{name}: adjustment of {t['symbol']} failed: {why}"[:300],
                       trade_id=t["id"], strategy_id=t["strategy_id"])
        try:
            lg = (await build_legs(one, sym, t["side"], u, now))[0]
        except Exception as e:  # noqa: BLE001
            await fail(str(e))
            return None
        if not lg.get("price"):
            await fail(f"no live price for {lg['symbol']}")       # no synthetic fills
            return None
        lg["qty"] = lg["qty0"] = lg["per_set"] * sets
        lg["entry"] = lg["entry_quote"] = lg["price"]
        if t["mode"] == "live":
            acc, why = await asyncio.to_thread(_account, await asyncio.to_thread(_strategy_account, t["strategy_id"]))
            if acc is None:
                await fail(why or "no live account")
                return None
            lg["product"] = _product(spec, lg)
            ok, oid, msg = await _order(acc, lg["symbol"], "BUY" if lg["act"] > 0 else "SELL", lg["qty"],
                                        exit_=False, strategy_id=t["strategy_id"], product=lg["product"])
            if not ok:
                await fail(f"{lg['symbol']}: {msg}")
                return None
            lg["order_id"] = oid
            fill = await _fill_price(acc, oid)
            if fill:
                lg["entry"] = fill
            else:
                net = await _broker_net(acc, lg["symbol"])
                if net is None or net * lg["act"] <= 0:
                    await _cancel(acc, oid)            # a late fill would be unmanaged
                    await fail(f"{lg['symbol']}: not filled at the broker")
                    return None
        engine.leg_levels([lg])
        return lg

    async def _book(self, t: dict, reason: str, now: float) -> None:
        gross = ch = 0.0
        for lg in t["legs"]:
            g, c = engine.leg_pnl(lg["act"], lg["qty"], lg["entry"], lg["exit"], lg["kind"], True, lg.get("qty0"))
            gross, ch = gross + g, ch + c
        if all(lg.get("external") for lg in t["legs"]) and reason not in ("STALE_SESSION",):
            reason = "CLOSED_EXTERNAL"
        first = t["legs"][0]
        await asyncio.to_thread(
            _update, t["id"], status="closed", exit_at=_utc(now), exit_price=round(first["exit"], 2),
            exit_reason=reason, gross_pnl=round(gross, 2), charges=round(ch, 2), net_pnl=round(gross - ch, 2),
            legs=t["legs"], exit_order_id=",".join(str(lg.get("exit_order_id")) for lg in t["legs"]
                                                   if lg.get("exit_order_id"))[:64] or None)
        self._retry_at.pop(t["id"], None)
        t["status"] = "closed"
        self.event("exit", f"{self.label(t['strategy_id'], t.get('version'))}: "
                           f"{reason} {t.get('instrument') or t['symbol']} net ₹{gross - ch:.2f}",
                   trade_id=t["id"], strategy_id=t["strategy_id"])
        _notify_exit({"symbol": t.get("instrument") or t["symbol"], "reason": reason, "quantity": first["qty"],
                      "entry": first["entry"], "exit": first["exit"], "pnl": round(gross - ch, 2)})

    # -- entries -------------------------------------------------------------

    async def _on_bar(self, s: dict, now: float, day_start: float) -> None:
        spec = s["spec"]
        tf_s = spec["timeframe"] * 60
        open_s = day_start + OPEN_MIN * 60
        k = int((now - open_s - GRACE_S) // tf_s)
        if k < 1:
            return
        bar = int(open_s + (k - 1) * tf_s)                # start of the bar that just closed
        ranks = bool(spec.get("rank"))
        rank_key = (s["id"], "#rank")                      # in _last_bar: the bar this strategy last ranked
        ready: dict[str, dict] = {}
        for sym in spec["symbols"]:
            key = (s["id"], sym)
            if self._last_bar.get(key, 0) >= bar:
                continue
            if now - (bar + tf_s) > STALE_BAR_S:
                self._last_bar[key] = bar                  # startup mid-bar: wait for the next one
                continue
            try:
                d = await data.live_bundle(spec, sym, now)
            except Exception as e:  # noqa: BLE001
                self._last_bar[key] = bar
                self.event("error", f"{s['name']} {sym}: candles unavailable: {e}"[:300], strategy_id=s["id"])
                continue
            if spec["bars"]["type"] != "time":
                # Volume/turnover candles close whenever enough has traded: this
                # minute is done; act only if a NEW candle completed since.
                self._last_bar[key] = bar
                if not d["t"] or d["tc"][-1] <= self._last_tc.get(key, 0):
                    continue
                first_seen = key not in self._last_tc
                self._last_tc[key] = d["tc"][-1]
                if first_seen or now - d["tc"][-1] > STALE_BAR_S:
                    continue                               # closed before we were watching
                try:
                    await self._decide(s, sym, d, now, day_start)
                except Exception as e:  # noqa: BLE001
                    log.exception("algo.decide_failed", symbol=sym)
                    self.event("error", f"{s['name']} {sym}: {e}"[:300], strategy_id=s["id"])
                continue
            if not d["t"] or d["t"][-1] < bar:
                # Newest candle from an earlier day, well after this bar closed:
                # no session today (exchange holiday / suspended symbol). Mark
                # the bar done instead of re-polling Fyers every 5s all day.
                if (not d["t"] or (d["t"][-1] + IST) // 86400 < (now + IST) // 86400) \
                        and now > bar + tf_s + 20:
                    self._last_bar[key] = bar
                continue                                   # Fyers hasn't published it yet; next tick
            if now < bar + tf_s + REF_WAIT_S and any(not x["t"] or x["t"][-1] < bar for x in (d.get("xs") or {}).values()):
                continue                                   # an index / peer the conditions read isn't published yet;
                                                           # past the wait it counts as its last closed candle (as backtests do)
            if ranks and self._last_bar.get(rank_key, 0) < bar:
                ready[sym] = d                             # entries are ranked against the other symbols, below
                continue
            self._last_bar[key] = bar
            try:     # ranked already: a candle that came in late only manages its position
                await self._decide(s, sym, d, now, day_start, may_enter=not ranks)
            except Exception as e:  # noqa: BLE001 — one symbol must not stop the others
                log.exception("algo.decide_failed", symbol=sym)
                self.event("error", f"{s['name']} {sym}: {e}"[:300], strategy_id=s["id"])
        if not ready:
            return
        if now < bar + tf_s + REF_WAIT_S and any(self._last_bar.get((s["id"], x), 0) < bar and x not in ready
                                                 for x in spec["symbols"]):
            return                                         # rank against every symbol's candle, not the first few in
        self._last_bar[rank_key] = bar
        try:
            order, allowed = engine.rank_pick(spec, ready)
        except Exception as e:  # noqa: BLE001 — no ranking, no entries; exits still run
            self.event("error", f"{s['name']}: ranking failed, no entries this candle: {e}"[:300], strategy_id=s["id"])
            order, allowed = list(ready), set()
        for sym in order:                                  # best first: capital and max positions go to them
            self._last_bar[(s["id"], sym)] = bar
            try:
                await self._decide(s, sym, ready[sym], now, day_start, may_enter=sym in allowed)
            except Exception as e:  # noqa: BLE001
                log.exception("algo.decide_failed", symbol=sym)
                self.event("error", f"{s['name']} {sym}: {e}"[:300], strategy_id=s["id"])

    async def _decide(self, s: dict, sym: str, d: dict, now: float, day_start: float, may_enter: bool = True) -> None:
        spec = s["spec"]
        engine.prepare(d, spec)
        sig = engine.signals(d, spec, {})
        _, open_trades = await asyncio.to_thread(_snapshot)
        mine = next((t for t in open_trades if t["strategy_id"] == s["id"] and t["symbol"] == sym), None)
        if mine:
            if sig["exit_long" if mine["side"] == "BUY" else "exit_short"][-1]:
                prices = await _ltp([lg["symbol"] for lg in mine["legs"]])
                await self._exit(mine, prices, "SIGNAL", now)
            elif spec["instrument"]["type"] == "option" and (spec.get("position_rules") or {}).get("exit"):
                prices = await _ltp([lg["symbol"] for lg in mine["legs"]] + [sym])
                m = engine.position_metrics(mine["legs"], prices.get(sym.upper()) or d["c"][-1], now,
                                            fno.known_lot(fno.fno_name(sym)) or 1,
                                            lambda lg: prices.get(lg["symbol"].upper()))
                hit = engine.exit_rule_hit(spec, m)
                if hit:
                    self.event("exit", f"{s['name']}: {sym} exit rule {engine.rule_label(hit)} "
                                       f"({hit['metric']} = {m[hit['metric']]:.3f})", strategy_id=s["id"])
                    await self._exit(mine, prices, f"RULE_{hit['metric'].upper()}", now)
                    return
            if engine.adjusts(spec):
                await self._adjust(s, mine, sym, now)
            return
        close_min = int((indicators.close_time(d, len(d["t"]) - 1) + IST) % 86400) // 60
        start, end, _ = engine.session_window(spec)
        if not start <= close_min <= end or not engine.day_allowed(spec, now):
            return
        side = "BUY" if sig["entry_long"][-1] else "SELL" if sig["entry_short"][-1] else None
        if side is None or not may_enter:                  # not among the candle's best N (rank)
            return
        if await asyncio.to_thread(_trades_today, s["id"], sym, day_start) >= spec["max_trades_per_day"]:
            return
        if spec["cooldown_bars"]:
            last = await asyncio.to_thread(_last_exit, s["id"], sym)
            if last and now < last + spec["cooldown_bars"] * engine.bar_minutes(spec) * 60:
                return
        level = engine.entry_trigger(spec, side, d["c"][-1], engine.tick_for(sym))
        if level is not None:
            eo = spec["entry_order"]
            self._armed[(s["id"], sym)] = {"side": side, "level": level, "kind": eo["type"], "d": d,
                                           "expires": now + eo["valid_bars"] * engine.bar_minutes(spec) * 60}
            self.event("info", f"{s['name']}: {side} {sym} armed — {eo['type']} entry at {level:.2f}",
                       strategy_id=s["id"])
            return
        await self._try_enter(s, sym, side, d, now, day_start)

    async def _try_enter(self, s: dict, sym: str, side: str, d: dict, now: float, day_start: float) -> None:
        spec = s["spec"]
        # A trigger can fire minutes after its signal: re-check what may have
        # changed meanwhile — never two positions in one symbol, never past the
        # day's trade cap, never after the entry window closed.
        _, open_trades = await asyncio.to_thread(_snapshot)
        if any(t["strategy_id"] == s["id"] and t["symbol"] == sym for t in open_trades):
            return
        if await asyncio.to_thread(_trades_today, s["id"], sym, day_start) >= spec["max_trades_per_day"]:
            return
        if int((now + IST) % 86400) // 60 >= engine.session_window(spec)[2]:
            return
        if self._book_halt == int((now + IST) // 86400):
            return                                         # the whole book stopped for today
        book = await asyncio.to_thread(_book, s["id"], day_start)
        if book["open"] >= spec["portfolio"]["max_positions"]:
            self.event("info", f"{s['name']}: {side} {sym} skipped — {book['open']} positions open (max)",
                       strategy_id=s["id"])
            return
        await self._enter(s, sym, side, d, now, book)

    async def _check_armed(self, by_id: dict, now: float, day: int, day_start: float) -> None:
        """Fire pullback / breakout entries whose trigger the underlying touched."""
        prices = await _ltp([sym for _, sym in self._armed])
        for key, a in list(self._armed.items()):
            sid, sym = key
            s = by_id.get(sid)
            if s is None or not s["enabled"] or (sid, day) in self._halted or now > a["expires"]:
                self._armed.pop(key, None)
                if s is not None and now > a["expires"]:
                    self.event("info", f"{s['name']}: {a['side']} {sym} {a['kind']} entry expired untouched",
                               strategy_id=sid)
                continue
            px = prices.get(sym.upper())
            if px is None:
                continue
            dip = (a["side"] == "BUY") == (a["kind"] == "pullback")
            if (px <= a["level"]) if dip else (px >= a["level"]):
                self._armed.pop(key, None)
                await self._try_enter(s, sym, a["side"], a["d"], now, day_start)

    async def _enter(self, s: dict, sym: str, side: str, d: dict, now: float, book: dict) -> None:
        spec, inst, pf = s["spec"], s["spec"]["instrument"], s["spec"]["portfolio"]
        if inst["type"] != "equity" and bool(getattr(get_settings(), "GATE_FNO_BAN_ENABLED", False)):
            from app.services.exchange_lists import lists

            why = lists.fno_ban_reason(fno.fno_name(sym))
            if why:
                self.event("info", f"{s['name']}: {side} {sym} skipped — {why}", strategy_id=s["id"])
                return
        u = (await _ltp([sym])).get(sym.upper())
        if u is None:
            self.event("error", f"{s['name']}: {side} {sym} skipped — no live price (no synthetic fills)",
                       strategy_id=s["id"])
            return
        try:
            legs = await build_legs(spec, sym, side, u, now)
        except Exception as e:  # noqa: BLE001
            self.event("error", f"{s['name']}: {side} {sym} skipped — {e}"[:300], strategy_id=s["id"])
            return
        if any(not lg.get("price") for lg in legs):
            self.event("error", f"{s['name']}: {side} {sym} skipped — no live price for "
                                f"{', '.join(lg['symbol'] for lg in legs if not lg.get('price'))}", strategy_id=s["id"])
            return
        if inst["type"] == "option" and (spec.get("position_rules") or {}).get("entry"):
            one = [{**lg, "qty": lg["per_set"], "entry": lg["price"]} for lg in legs]
            m0 = engine.position_metrics(one, u, now, fno.known_lot(fno.fno_name(sym)) or 1, lambda lg: lg["price"])
            miss = engine.entry_rules_fail(spec, m0)
            if miss:
                got = m0.get(miss["metric"])
                self.event("info", f"{s['name']}: {side} {sym} skipped — entry rule {engine.rule_label(miss)} not met "
                                   f"({miss['metric']} = {'n/a' if got is None else f'{got:.3f}'})", strategy_id=s["id"])
                return
        ref = "u" if inst["type"] == "equity" or inst["levels_on"] == "underlying" else "0"
        sign = 1 if side == "BUY" else -1
        rsign = sign if ref == "u" else legs[0]["act"]
        atr_vals = {p: engine.series(d, {"ind": "ATR", "params": {"period": p}}, {})[-1]
                    for p in engine.atr_periods(spec)}
        if ref != "u":                                   # levels on the option premium: its own ATR
            atr_vals = engine.premium_atr(atr_vals, legs[0], u, time.time())
        # Sizing on the quoted prices (the stop distance only needs the shape).
        tick = engine.tick_for(sym)
        pre = engine.entry_levels(spec, rsign, u if ref == "u" else legs[0]["price"], atr_vals, tick)
        per_set_margin = engine.position_margin(legs, u, pf["leverage"], price_key="price", model=spec.get("margin"),
                                                t=now, name=fno.fno_name(sym))
        equity = pf["capital"] + (book["realized"] if pf["compounding"] else 0.0)
        alloc = await self.allocated(now)
        if s["id"] in alloc:                      # the book's split replaces the strategy's own capital
            equity = alloc[s["id"]]["capital"]
        sets = engine.units(spec, equity, per_set_margin,
                            pre["sl_dist"] * legs[0]["per_set"] if pre["sl_dist"] else None)
        if per_set_margin > 0:
            sets = min(sets, int(max(0.0, equity - book["margin"]) // per_set_margin))
        cap = get_settings().ALGO_BOOK_MAX_MARGIN
        if cap and per_set_margin > 0 and sets >= 1:
            used = (await asyncio.to_thread(_book_all, now - (now + IST) % 86400))["margin"]
            sets = min(sets, int(max(0.0, cap - used) // per_set_margin))
            if sets < 1:
                self.event("info", f"{s['name']}: {side} {sym} skipped — the book margin cap is full "
                                   f"(₹{used:,.0f} of ₹{cap:,.0f} in use)", strategy_id=s["id"])
                return
        if sets < 1:
            self.event("error", f"{s['name']}: {side} {sym} skipped — sizing / free capital gives 0 "
                                f"(needs ₹{per_set_margin:,.0f} per set)", strategy_id=s["id"])
            return
        for lg in legs:
            lg["qty"] = lg["qty0"] = lg["per_set"] * sets
            lg["entry"] = lg["entry_quote"] = lg["price"]        # the fill replaces entry; the quote stays (TCA)
        order_ids: list[str] = []
        if s["mode"] == "live":
            acc, why = await asyncio.to_thread(_account, s["account_id"])
            placed: list[dict] = []
            failure = why
            if acc is not None:
                for lg in sorted(legs, key=lambda x: -x["act"]):       # bought legs first
                    lg["product"] = _product(spec, lg)
                    lim = spec["entry_order"].get("limit")
                    if lim and lg.get("price"):
                        ok, oid, msg = await _order_chase(acc, lg["symbol"], "BUY" if lg["act"] > 0 else "SELL", lg["qty"],
                                                          strategy_id=s["id"], product=lg["product"], quote=lg["price"],
                                                          wait_s=lim["wait_s"], steps=lim["steps"])
                    else:
                        ok, oid, msg = await _order(acc, lg["symbol"], "BUY" if lg["act"] > 0 else "SELL",
                                                    lg["qty"], exit_=False, strategy_id=s["id"], product=lg["product"])
                    if not ok:
                        failure = f"{lg['symbol']}: {msg}"
                        break
                    lg["order_id"] = oid
                    placed.append(lg)
                    order_ids.append(str(oid))
            if not failure:
                # "ok" is Fyers ACCEPTING an order; a leg can still be rejected
                # after it (RMS, exchange). A leg with no fill that the broker
                # doesn't hold is not a hedge — unwind the rest.
                for lg in legs:                                    # book the broker's fill, not the quote
                    fill = await _fill_price(acc, lg.get("order_id"))
                    if fill:
                        lg["entry"] = fill
                        continue
                    net = await _broker_net(acc, lg["symbol"])
                    if net is not None and net * lg["act"] <= 0:
                        failure = f"{lg['symbol']}: not filled at the broker"
                        break
            if failure:
                for lg in placed:                                   # roll back what did fill
                    await _cancel(acc, lg.get("order_id"))          # a late fill would be unmanaged
                    qty = lg["qty"]
                    net = await _broker_net(acc, lg["symbol"])
                    if net is not None:                             # never sell what the broker doesn't hold
                        qty = min(qty, abs(net)) if net * lg["act"] > 0 else 0
                    if qty:
                        await _order(acc, lg["symbol"], "SELL" if lg["act"] > 0 else "BUY", qty,
                                     exit_=True, strategy_id=s["id"], product=lg["product"])
                await asyncio.to_thread(
                    _insert, strategy_id=s["id"], symbol=sym, side=side, quantity=legs[0]["qty"], mode="live",
                    status="rejected", entry_at=_utc(now), entry_price=round(u, 2), legs=_clean(legs),
                    instrument=" + ".join(lg["label"] for lg in legs)[:160], version=s.get("version"),
                    note=f"{failure}{' (placed legs rolled back)' if placed else ''}"[:500])
                self.event("error", f"{s['name']}: LIVE {side} {sym} rejected: {failure}", strategy_id=s["id"])
                return
        ref_entry = u if ref == "u" else legs[0]["entry"]
        engine.leg_levels(legs, tick)                  # each leg's own stop / target, on its fill
        lv = engine.entry_levels(spec, rsign, ref_entry, atr_vals, tick)
        plan = engine.scale_plan(lv["so"], sets)
        label = " + ".join(lg["label"] for lg in legs)[:160]
        tid = await asyncio.to_thread(
            _insert, strategy_id=s["id"], symbol=sym, side=side, quantity=legs[0]["qty"], mode=s["mode"],
            status="open", entry_at=_utc(now), entry_price=round(ref_entry, 2),
            stop_loss=lv["sl"] and round(lv["sl"], 2), target=lv["tg"] and round(lv["tg"], 2),
            trail_dist=lv["trd"], trail_activate=lv["tra"], breakeven=lv["be"], best_price=ref_entry,
            legs=_clean(legs), ref=ref, ref_sign=rsign, margin=round(per_set_margin * sets, 2),
            instrument=label, u_entry=u, entry_order_id=",".join(order_ids)[:64] or None,
            version=s.get("version"), scale={"left": sets, "sets": sets, "levels": plan, "done": 0} if plan else None)
        self.event("entry", f"{s['name']} v{s.get('version', 1)}: {side} → {label} x{sets} set(s) ({s['mode']})",
                   trade_id=tid, strategy_id=s["id"])
        _notify_entry({"side": side, "symbol": label, "quantity": legs[0]["qty"], "entry": round(legs[0]["entry"], 2),
                       "stop_loss": lv["sl"] and round(lv["sl"], 2), "target": lv["tg"] and round(lv["tg"], 2)})

    # -- data ----------------------------------------------------------------

    async def sync(self, strategies: list[dict], days: int = 5) -> dict[str, Any]:
        """Top up the 1-minute store for every symbol any strategy trades."""
        syms = sorted({x for s in strategies for x in s["spec"]["symbols"]})
        if any(s["spec"]["instrument"]["type"] == "option" or engine.vix_timeframes(s["spec"]) for s in strategies):
            syms.append(data.VIX)
        now = int(time.time())
        ok, failed = 0, []
        for sym in syms:
            try:
                await data.download(sym, now - days * 86400, now)
                ok += 1
            except Exception as e:  # noqa: BLE001
                failed.append(f"{sym}: {e}"[:200])
        self.last_sync = {"t": time.time(), "symbols": len(syms), "ok": ok, "failed": failed}
        self.event("sync", f"candle sync: {ok}/{len(syms)} symbols", failed=len(failed))
        return self.last_sync


def _clean(legs: list[dict]) -> list[dict]:
    keep = ("symbol", "kind", "act", "qty", "qty0", "entry", "label", "K", "exp", "order_id", "product",
            "per_set", "sl", "tg", "src", "via", "hedge", "entry_quote")
    return [{k: lg[k] for k in keep if k in lg} for lg in legs]
