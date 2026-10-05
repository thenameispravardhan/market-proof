"""Live automation: run enabled Algo strategies on bar close, manage exits on LTP.

Every 5 seconds during the session:
  * open positions are re-priced from live Fyers LTPs and checked against
    stop / target / trailing (with activation) / breakeven on their reference
    price, the MTM (rupee) stop / target / trail on the whole position, the
    time stop, and the strategy's square-off time — exits never wait for a bar;
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
exit (never a synthetic fill); intraday only — everything is flat by the
square-off time and anything left at 15:30 is closed.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from app.algo import data, engine, fno
from app.algo import indicators
from app.db import session as db_session
from app.db.models import AlgoStrategy, AlgoTrade, BrokerAccount
from app.logging_config import get_logger

log = get_logger(__name__)

IST = engine.IST
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
GRACE_S = 3          # let Fyers finalise the candle that just closed
STALE_BAR_S = 120    # a bar this old at first sight was missed, not acted on
SYNC_AFTER_MIN = 15 * 60 + 45


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _naive(ts: float) -> datetime:
    return _utc(ts).replace(tzinfo=None)


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
                               "account_id": s.account_id, "spec": spec, "version": s.version or 1})
        trades = [_row(t) for t in db.execute(
            select(AlgoTrade).where(AlgoTrade.status == "open")).scalars()]
    return strategies, trades


def _row(t: AlgoTrade) -> dict[str, Any]:
    r = {c.name: getattr(t, c.name) for c in AlgoTrade.__table__.columns}
    if not r.get("legs"):        # rows from before F&O support: one equity leg
        r["legs"] = [{"symbol": r["symbol"], "kind": "EQ", "act": 1 if r["side"] == "BUY" else -1,
                      "qty": r["quantity"], "entry": r["entry_price"], "label": r["symbol"]}]
        r["ref"] = r.get("ref") or "u"
    return r


def _trades_today(strategy_id: int, symbol: str, day_start: float) -> int:
    with db_session.SessionLocal() as db:
        return db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.symbol == symbol,
            AlgoTrade.status != "rejected", AlgoTrade.entry_at >= _naive(day_start))).scalar_one()


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
        if not acc.enabled:
            return None, f"account {acc.name!r} is switched off"
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


async def _order(account: BrokerAccount, symbol: str, side: str, qty: int, *, exit_: bool,
                 strategy_id: int) -> tuple[bool, Optional[str], str]:
    """(ok, broker order id, message) — one MARKET INTRADAY order."""
    from app.api.orders import _manager

    try:
        r = await _manager().place_manual_order(
            account=account, symbol=symbol, side=side, quantity=int(qty), order_type="MARKET",
            product_type="INTRADAY", bypass_risk=exit_, operator=f"algo:{strategy_id}")
    except Exception as e:  # noqa: BLE001 — a broker fault is reported, never raised into the loop
        return False, None, str(e)[:300]
    if r.get("ok"):
        return True, r.get("broker_order_id"), str(r.get("status"))
    return False, r.get("broker_order_id"), str(r.get("error") or r.get("risk_message") or r.get("status"))


async def _fill_price(account: Optional[BrokerAccount], order_id: Optional[str], tries: int = 4) -> Optional[float]:
    """Average traded price of a live order, polled briefly; None if the broker
    hasn't reported a fill yet (the quote then stands in)."""
    if account is None or not order_id:
        return None
    from app.api.orders import _manager
    from app.execution.base import OrderState

    backend = _manager()._manual_backend_for(account)  # noqa: SLF001
    for k in range(tries):
        try:
            st = await backend.get_order_status(str(order_id))
            if st.state == OrderState.FILLED and st.average_price:
                return float(st.average_price)
        except Exception as e:  # noqa: BLE001
            log.warning("algo.fill_price_failed", order_id=order_id, error=str(e)[:200])
            return None
        if k + 1 < tries:
            await asyncio.sleep(0.6)
    return None


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
    lot = fno.lot_size(name)
    if inst["type"] == "future":
        fs = fno.future_symbol(name, inst["expiry"], now)
        if not fs:
            raise ValueError(f"no {inst['expiry']} {name} future in the F&O master")
        px = (await _ltp([fs])).get(fs)
        return [{"symbol": fs, "kind": "FUT", "act": sign, "per_set": lot, "label": fs.split(":")[-1], "price": px}]
    backend = data._backend()
    cfgs = inst["legs_long" if side == "BUY" else "legs_short"]
    count = min(50, max([c["steps"] for c in cfgs] + [0]) + 3 if all(c["strike"] != "PREMIUM" for c in cfgs) else 30)
    chain = await backend.get_option_chain(sym, strikecount=count)
    exp = _pick_expiry([int(e["ts"]) for e in chain.get("expiries") or [] if str(e.get("ts")).isdigit()],
                       inst["expiry_kind"], name, inst["expiry"])
    if exp is None:
        raise ValueError(f"no {inst['expiry']} {inst['expiry_kind']} expiry listed for {name}")
    listed = sorted(int(e["ts"]) for e in chain.get("expiries") or [] if str(e.get("ts")).isdigit())
    if listed and exp != listed[0]:
        chain = await backend.get_option_chain(sym, strikecount=count, timestamp=str(exp))
    rows = [r for r in chain.get("strikes") or [] if r.get("strike")]
    if not rows:
        raise ValueError(f"empty option chain for {name}")
    strikes = sorted(r["strike"] for r in rows)
    step = min((b - a for a, b in zip(strikes, strikes[1:]) if b > a), default=fno.default_step(name, u))
    spot = chain.get("spot") or u
    legs = []
    for c in cfgs:
        key = c["right"].lower()
        if c["strike"] == "PREMIUM":
            cands = [r for r in rows if (r.get(key) or {}).get("ltp")]
            if not cands:
                raise ValueError(f"no {c['right']} premiums in the chain")
            row = min(cands, key=lambda r: abs(r[key]["ltp"] - c["premium"]))
        else:
            k_ = fno.pick_strike(spot, step, c["right"], c["strike"], c["steps"])
            row = min(rows, key=lambda r: abs(r["strike"] - k_))
        leg = row.get(key) or {}
        if not leg.get("symbol"):
            raise ValueError(f"{name} {row['strike']:g} {c['right']} not in the chain")
        legs.append({"symbol": leg["symbol"].upper(), "kind": c["right"], "act": 1 if c["action"] == "BUY" else -1,
                     "per_set": lot * c["lots"], "label": leg["symbol"].split(":")[-1],
                     "price": leg.get("ltp"), "K": row["strike"], "exp": exp})
    return legs


def _notify_entry(payload: dict[str, Any]) -> None:
    from app.services.event_bus import event_bus

    asyncio.ensure_future(event_bus.publish("algo.entry", payload))


def _notify_exit(payload: dict[str, Any]) -> None:
    from app.services.event_bus import event_bus

    asyncio.ensure_future(event_bus.publish("trade.closed", payload))


class AlgoRunner:
    TICK_S = 1.0   # exits / stops checked on live LTP every second

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

    def event(self, level: str, msg: str, **kw: Any) -> None:
        self.events.appendleft({"t": time.time(), "level": level, "msg": msg, **kw})
        getattr(log, "warning" if level == "error" else "info")("algo.event", msg=msg, **kw)

    async def run(self) -> None:
        log.info("algo_runner.start")
        while True:
            await asyncio.sleep(self.TICK_S)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — never kill the loop
                log.exception("algo_runner.tick_failed")
                self.event("error", f"tick failed: {e}"[:300])

    async def tick(self, now: Optional[float] = None) -> None:
        now = now or time.time()
        self.last_tick = now
        day = int((now + IST) // 86400)
        if (day + 3) % 7 >= 5:                 # Saturday / Sunday
            return
        mins = int((now + IST) % 86400) // 60
        strategies, open_trades = await asyncio.to_thread(_snapshot)
        by_id = {s["id"]: s for s in strategies}
        day_start = day * 86400 - IST
        # Record real ticks for everything an enabled strategy trades.
        from app.algo import ticks

        ticks.recorder().extra = sorted({x for s in strategies if s["enabled"] for x in s["spec"]["symbols"]})
        if self._master_day != day and any(s["spec"]["instrument"]["type"] != "equity" for s in strategies):
            self._master_day = day
            try:
                await fno.ensure_master()
            except Exception as e:  # noqa: BLE001
                self.event("error", f"F&O master refresh failed: {e}"[:200])
        stale = [t for t in open_trades if _naive(day_start) > t["entry_at"].replace(tzinfo=None)]
        if stale:
            await self._close_stale(stale)
            open_trades = [t for t in open_trades if t not in stale]
        if OPEN_MIN <= mins < CLOSE_MIN:
            await self._manage(open_trades, by_id, now, mins, day_start)
            if self._armed:
                await self._check_armed(by_id, now, day, day_start)
            for s in strategies:
                if s["enabled"] and (s["id"], day) not in self._halted:
                    await self._on_bar(s, now, day_start)
        elif mins >= CLOSE_MIN:
            if open_trades:
                await self._manage(open_trades, by_id, now, 24 * 60, day_start)   # past every square-off
            if mins >= SYNC_AFTER_MIN and self._sync_day != day and strategies:
                self._sync_day = day
                await self.sync(strategies, days=5)

    # -- exits ---------------------------------------------------------------

    async def _manage(self, trades: list[dict], by_id: dict, now: float, mins: int, day_start: float) -> None:
        if not trades:
            return
        syms = {lg["symbol"] for t in trades for lg in t["legs"]} | {t["symbol"] for t in trades}
        prices = await _ltp(list(syms))
        for t in trades:
            s = by_id.get(t["strategy_id"])
            spec = s["spec"] if s else None
            sq = engine.session_window(spec)[2] if spec else 15 * 60 + 15
            if mins >= sq:
                await self._exit(t, prices, "SQUARE_OFF", now)
                continue
            reason = await self._check(t, spec, prices, now)
            if reason:
                await self._exit(t, prices, reason, now)
        day = int((now + IST) // 86400)
        for sid in {t["strategy_id"] for t in trades}:
            s = by_id.get(sid)
            if not s or not (s["spec"]["daily"]["max_loss"] or s["spec"]["daily"]["max_profit"]):
                continue
            mine = [t for t in trades if t["strategy_id"] == sid and t["status"] == "open"]
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
                nt = best - sign * t["trail_dist"]
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
            peak = m if t["mtm_peak"] is None else max(t["mtm_peak"], m)
            if peak != t["mtm_peak"]:
                changed["mtm_peak"] = t["mtm_peak"] = peak
            if r["stop"] and m <= -r["stop"]:
                reason = "MTM_SL"
            elif r["trail_start"] and peak >= r["trail_start"] and m <= peak - r["trail_gap"]:
                reason = "MTM_TRAIL"
            elif r["target"] and m >= r["target"]:
                reason = "MTM_TARGET"
        if spec and not reason and spec["max_bars"]:
            if now >= _epoch(t["entry_at"]) + spec["max_bars"] * engine.bar_minutes(spec) * 60:
                reason = "TIME_STOP"
        if changed:
            await asyncio.to_thread(_update, t["id"], **{k: v for k, v in changed.items()})
        return reason

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
        if self._retry_at.get(t["id"], 0) > now:
            return False
        legs = t["legs"]
        todo = sorted([lg for lg in legs if lg.get("exit") is None], key=lambda lg: lg["act"])  # written legs first
        for lg in todo:
            px = prices.get(lg["symbol"].upper())
            if px is None:
                px = await self._last_close(lg["symbol"])
            if px is None:
                self._retry_at[t["id"]] = now + 15
                self.event("error", f"{t['symbol']}: no price for {lg['symbol']} — exit ({reason}) retried",
                           trade_id=t["id"])
                return False
            if t["mode"] == "live":
                acc, why = await asyncio.to_thread(_account, await asyncio.to_thread(_strategy_account, t["strategy_id"]))
                net = await _broker_net(acc, lg["symbol"]) if acc is not None else None
                held = net if net is not None and (net > 0) == (lg["act"] > 0) else 0
                if acc is not None and net is not None and held == 0:
                    lg["exit"], lg["external"] = px, True       # already flat at the broker: no order
                    continue
                ok, oid, msg = (False, None, why or "positions unavailable") if acc is None or net is None \
                    else await _order(acc, lg["symbol"], "SELL" if lg["act"] > 0 else "BUY",
                                      min(lg["qty"], abs(held)), exit_=True, strategy_id=t["strategy_id"])
                if not ok:
                    self._retry_at[t["id"]] = now + 30
                    await asyncio.to_thread(_update, t["id"], legs=legs, note=f"exit failed: {msg}"[:500])
                    self.event("error", f"EXIT FAILED {lg['symbol']} ({reason}): {msg}", trade_id=t["id"])
                    return False
                lg["exit_order_id"] = oid
                px = await _fill_price(acc, oid) or px     # the broker's average, when it reports one
            lg["exit"] = px
        await self._book(t, reason, now)
        return True

    async def _book(self, t: dict, reason: str, now: float) -> None:
        gross = ch = 0.0
        for lg in t["legs"]:
            g, c = engine.leg_pnl(lg["act"], lg["qty"], lg["entry"], lg["exit"], lg["kind"], True)
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
        self.event("exit", f"{reason} {t.get('instrument') or t['symbol']} net ₹{gross - ch:.2f}",
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
            self._last_bar[key] = bar
            try:
                await self._decide(s, sym, d, now, day_start)
            except Exception as e:  # noqa: BLE001 — one symbol must not stop the others
                log.exception("algo.decide_failed", symbol=sym)
                self.event("error", f"{s['name']} {sym}: {e}"[:300], strategy_id=s["id"])

    async def _decide(self, s: dict, sym: str, d: dict, now: float, day_start: float) -> None:
        spec = s["spec"]
        engine.prepare(d, spec)
        sig = engine.signals(d, spec, {})
        _, open_trades = await asyncio.to_thread(_snapshot)
        mine = next((t for t in open_trades if t["strategy_id"] == s["id"] and t["symbol"] == sym), None)
        if mine:
            if sig["exit_long" if mine["side"] == "BUY" else "exit_short"][-1]:
                prices = await _ltp([lg["symbol"] for lg in mine["legs"]])
                await self._exit(mine, prices, "SIGNAL", now)
            return
        close_min = int((indicators.close_time(d, len(d["t"]) - 1) + IST) % 86400) // 60
        start, end, _ = engine.session_window(spec)
        if not start <= close_min <= end:
            return
        side = "BUY" if sig["entry_long"][-1] else "SELL" if sig["entry_short"][-1] else None
        if side is None:
            return
        if await asyncio.to_thread(_trades_today, s["id"], sym, day_start) >= spec["max_trades_per_day"]:
            return
        if spec["cooldown_bars"]:
            last = await asyncio.to_thread(_last_exit, s["id"], sym)
            if last and now < last + spec["cooldown_bars"] * engine.bar_minutes(spec) * 60:
                return
        level = engine.entry_trigger(spec, side, d["c"][-1])
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
        ref = "u" if inst["type"] == "equity" or inst["levels_on"] == "underlying" else "0"
        sign = 1 if side == "BUY" else -1
        rsign = sign if ref == "u" else legs[0]["act"]
        atr_vals = {p: engine.series(d, {"ind": "ATR", "params": {"period": p}}, {})[-1]
                    for p in engine.atr_periods(spec)}
        # Sizing on the quoted prices (the stop distance only needs the shape).
        pre = engine.entry_levels(spec, rsign, u if ref == "u" else legs[0]["price"], atr_vals)
        per_set_margin = engine.position_margin(legs, u, pf["leverage"], price_key="price")
        equity = pf["capital"] + (book["realized"] if pf["compounding"] else 0.0)
        sets = engine.units(spec, equity, per_set_margin,
                            pre["sl_dist"] * legs[0]["per_set"] if pre["sl_dist"] else None)
        if per_set_margin > 0:
            sets = min(sets, int(max(0.0, equity - book["margin"]) // per_set_margin))
        if sets < 1:
            self.event("error", f"{s['name']}: {side} {sym} skipped — sizing / free capital gives 0 "
                                f"(needs ₹{per_set_margin:,.0f} per set)", strategy_id=s["id"])
            return
        for lg in legs:
            lg["qty"] = lg["per_set"] * sets
            lg["entry"] = lg["price"]
        order_ids: list[str] = []
        if s["mode"] == "live":
            acc, why = await asyncio.to_thread(_account, s["account_id"])
            placed: list[dict] = []
            failure = why
            if acc is not None:
                for lg in sorted(legs, key=lambda x: -x["act"]):       # bought legs first
                    ok, oid, msg = await _order(acc, lg["symbol"], "BUY" if lg["act"] > 0 else "SELL",
                                                lg["qty"], exit_=False, strategy_id=s["id"])
                    if not ok:
                        failure = f"{lg['symbol']}: {msg}"
                        break
                    lg["order_id"] = oid
                    placed.append(lg)
                    order_ids.append(str(oid))
            if failure:
                for lg in placed:                                   # roll back what did fill
                    await _order(acc, lg["symbol"], "SELL" if lg["act"] > 0 else "BUY", lg["qty"],
                                 exit_=True, strategy_id=s["id"])
                await asyncio.to_thread(
                    _insert, strategy_id=s["id"], symbol=sym, side=side, quantity=legs[0]["qty"], mode="live",
                    status="rejected", entry_at=_utc(now), entry_price=round(u, 2), legs=_clean(legs),
                    instrument=" + ".join(lg["label"] for lg in legs)[:160], version=s.get("version"),
                    note=f"{failure}{' (placed legs rolled back)' if placed else ''}"[:500])
                self.event("error", f"{s['name']}: LIVE {side} {sym} rejected: {failure}", strategy_id=s["id"])
                return
            for lg in legs:                                        # book the broker's fill, not the quote
                fill = await _fill_price(acc, lg.get("order_id"))
                if fill:
                    lg["entry"] = fill
        ref_entry = u if ref == "u" else legs[0]["entry"]
        lv = engine.entry_levels(spec, rsign, ref_entry, atr_vals)
        label = " + ".join(lg["label"] for lg in legs)[:160]
        tid = await asyncio.to_thread(
            _insert, strategy_id=s["id"], symbol=sym, side=side, quantity=legs[0]["qty"], mode=s["mode"],
            status="open", entry_at=_utc(now), entry_price=round(ref_entry, 2),
            stop_loss=lv["sl"] and round(lv["sl"], 2), target=lv["tg"] and round(lv["tg"], 2),
            trail_dist=lv["trd"], trail_activate=lv["tra"], breakeven=lv["be"], best_price=ref_entry,
            legs=_clean(legs), ref=ref, ref_sign=rsign, margin=round(per_set_margin * sets, 2),
            instrument=label, u_entry=u, entry_order_id=",".join(order_ids)[:64] or None,
            version=s.get("version"))
        self.event("entry", f"{s['name']} v{s.get('version', 1)}: {side} → {label} x{sets} set(s) ({s['mode']})",
                   trade_id=tid, strategy_id=s["id"])
        _notify_entry({"side": side, "symbol": label, "quantity": legs[0]["qty"], "entry": round(legs[0]["entry"], 2),
                       "stop_loss": lv["sl"] and round(lv["sl"], 2), "target": lv["tg"] and round(lv["tg"], 2)})

    # -- data ----------------------------------------------------------------

    async def sync(self, strategies: list[dict], days: int = 5) -> dict[str, Any]:
        """Top up the 1-minute store for every symbol any strategy trades."""
        syms = sorted({x for s in strategies for x in s["spec"]["symbols"]})
        if any(s["spec"]["instrument"]["type"] == "option" for s in strategies):
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
    keep = ("symbol", "kind", "act", "qty", "entry", "label", "K", "exp", "order_id")
    return [{k: lg[k] for k in keep if k in lg} for lg in legs]
