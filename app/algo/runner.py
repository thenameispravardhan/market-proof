"""Live automation: run enabled Algo strategies on bar close, manage exits on LTP.

Every 5 seconds during the session:
  * open positions are checked against the live Fyers LTP for stop / target /
    trailing stop and the strategy's square-off time — exits do not wait for
    a bar to close;
  * each enabled strategy whose timeframe just closed a bar fetches its last
    completed candles from Fyers, evaluates the SAME engine the backtest uses
    on that bar, and enters / exits.

Modes, chosen per strategy on the Algo page:
  paper — fills at the live LTP, nothing is sent to a broker;
  live  — orders go through Manager.place_manual_order (MARKET, INTRADAY) on the
          chosen real Fyers account, so the global risk caps apply to entries.
          Exits pass bypass_risk: closing a position must never be blocked.

Invariants kept: Fyers-only prices; no live price -> no entry (never a
synthetic fill); intraday only — everything is flat by the square-off time and
anything left at 15:30 is closed. After the close, every symbol any strategy
uses gets the day's 1-minute candles downloaded into the store.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select

from app.algo import data, engine
from app.db.models import AlgoStrategy, AlgoTrade, BrokerAccount
from app.db import session as db_session
from app.logging_config import get_logger

log = get_logger(__name__)

IST = engine.IST
OPEN_MIN, CLOSE_MIN = 9 * 60 + 15, 15 * 60 + 30
GRACE_S = 3          # let Fyers finalise the candle that just closed
STALE_BAR_S = 120    # a bar this old at first sight was missed, not acted on
SYNC_AFTER_MIN = 15 * 60 + 45


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


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
                               "account_id": s.account_id, "spec": spec})
        trades = [_row(t) for t in db.execute(
            select(AlgoTrade).where(AlgoTrade.status == "open")).scalars()]
    return strategies, trades


def _row(t: AlgoTrade) -> dict[str, Any]:
    return {c.name: getattr(t, c.name) for c in AlgoTrade.__table__.columns}


def _trades_today(strategy_id: int, symbol: str, day_start: float) -> int:
    with db_session.SessionLocal() as db:
        return db.execute(select(func.count()).select_from(AlgoTrade).where(
            AlgoTrade.strategy_id == strategy_id, AlgoTrade.symbol == symbol,
            AlgoTrade.status != "rejected",
            AlgoTrade.entry_at >= _utc(day_start).replace(tzinfo=None))).scalar_one()


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


async def _ltp(symbols: list[str]) -> dict[str, float]:
    from app.api.market import fyers_quotes

    qs = await fyers_quotes(sorted(set(symbols)))
    return {k: float(q.last_price) for k, q in qs.items() if q.last_price}


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


class AlgoRunner:
    TICK_S = 5.0

    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=200)
        self.last_tick: Optional[float] = None
        self.last_sync: Optional[dict[str, Any]] = None
        self._last_bar: dict[tuple[int, str], int] = {}
        self._retry_at: dict[int, float] = {}
        self._sync_day: Optional[int] = None

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
        stale = [t for t in open_trades if _utc(day_start).replace(tzinfo=None) > t["entry_at"].replace(tzinfo=None)]
        if stale:
            await self._close_stale(stale, by_id)
            open_trades = [t for t in open_trades if t not in stale]
        if OPEN_MIN <= mins < CLOSE_MIN:
            await self._manage(open_trades, by_id, now, mins)
            for s in strategies:
                if s["enabled"]:
                    await self._on_bar(s, now, day_start)
        elif mins >= CLOSE_MIN:
            if open_trades:
                await self._manage(open_trades, by_id, now, 24 * 60)   # past every square-off
            if mins >= SYNC_AFTER_MIN and self._sync_day != day and strategies:
                self._sync_day = day
                await self.sync(strategies, days=5)

    # -- exits ---------------------------------------------------------------

    async def _manage(self, trades: list[dict], by_id: dict, now: float, mins: int) -> None:
        if not trades:
            return
        prices = await _ltp([t["symbol"] for t in trades])
        for t in trades:
            s = by_id.get(t["strategy_id"])
            sq = engine._hhmm(s["spec"]["session"]["square_off"]) if s else 15 * 60 + 15
            ltp = prices.get(t["symbol"].upper())
            if mins >= sq:
                if ltp is None:
                    ltp = await self._last_close(t["symbol"])
                if ltp is not None:
                    await self._exit(t, ltp, "SQUARE_OFF", now)
                continue
            if ltp is None:
                continue
            buy = t["side"] == "BUY"
            if t["trail_dist"]:
                best = max(t["best_price"] or ltp, ltp) if buy else min(t["best_price"] or ltp, ltp)
                nt = best - t["trail_dist"] if buy else best + t["trail_dist"]
                old = t["trail_stop"]
                trail = nt if old is None else (max(old, nt) if buy else min(old, nt))
                if best != t["best_price"] or trail != old:
                    t["best_price"], t["trail_stop"] = best, trail
                    await asyncio.to_thread(_update, t["id"], best_price=best, trail_stop=trail)
            stops = [x for x in (t["stop_loss"], t["trail_stop"]) if x is not None]
            stop = (max(stops) if buy else min(stops)) if stops else None
            if stop is not None and (ltp <= stop if buy else ltp >= stop):
                await self._exit(t, ltp, "TRAIL" if stop == t["trail_stop"] and stop != t["stop_loss"] else "SL", now)
            elif t["target"] is not None and (ltp >= t["target"] if buy else ltp <= t["target"]):
                await self._exit(t, ltp, "TARGET", now)

    async def _last_close(self, symbol: str) -> Optional[float]:
        try:
            b = await data.recent_bars(symbol, 1, bars=1)
            return b["c"][-1] if b["c"] else None
        except Exception:  # noqa: BLE001
            return None

    async def _close_stale(self, trades: list[dict], by_id: dict) -> None:
        """A position from an earlier session (the server was down at square-off).
        The broker auto-squares MIS; book it at the previous close so P&L is real."""
        from app.api.market import fyers_quotes

        qs = await fyers_quotes(sorted({t["symbol"] for t in trades}))
        for t in trades:
            q = qs.get(t["symbol"].upper())
            px = (q.prev_close if q is not None and q.prev_close else None) or t["entry_price"]
            await self._book(t, px, "STALE_SESSION", time.time(), None)

    async def _exit(self, t: dict, price: float, reason: str, now: float) -> bool:
        if self._retry_at.get(t["id"], 0) > now:
            return False
        order_id = None
        if t["mode"] == "live":
            s_acc = await asyncio.to_thread(_strategy_account, t["strategy_id"])
            acc, why = await asyncio.to_thread(_account, s_acc)
            # Never exit blind: if the broker already flattened it (MIS auto
            # square-off, a manual close in the Fyers app), a MARKET exit would
            # OPEN the opposite position.
            net = await _broker_net(acc, t["symbol"]) if acc is not None else None
            mine = net if net is not None and (net > 0) == (t["side"] == "BUY") else 0
            if acc is not None and net is not None and mine == 0:
                await self._book(t, price, "CLOSED_EXTERNAL", now, None)
                return True
            ok, order_id, msg = (False, None, why or "positions unavailable") if acc is None or net is None \
                else await _order(acc, t["symbol"], "SELL" if t["side"] == "BUY" else "BUY",
                                  min(t["quantity"], abs(mine)), exit_=True, strategy_id=t["strategy_id"])
            if not ok:
                self._retry_at[t["id"]] = now + 30
                await asyncio.to_thread(_update, t["id"], note=f"exit failed: {msg}"[:500])
                self.event("error", f"EXIT FAILED {t['symbol']} ({reason}): {msg}", trade_id=t["id"])
                return False
        await self._book(t, price, reason, now, order_id)
        return True

    async def _book(self, t: dict, price: float, reason: str, now: float, order_id: Optional[str]) -> None:
        gross, ch = engine.trade_pnl(t["side"], t["quantity"], t["entry_price"], price, True)
        await asyncio.to_thread(_update, t["id"], status="closed", exit_at=_utc(now), exit_price=round(price, 2),
                                exit_reason=reason, gross_pnl=gross, charges=ch, net_pnl=round(gross - ch, 2),
                                exit_order_id=order_id)
        self._retry_at.pop(t["id"], None)
        self.event("exit", f"{reason} {t['side']} {t['symbol']} x{t['quantity']} @ {price:.2f} "
                           f"net ₹{gross - ch:.2f}", trade_id=t["id"], strategy_id=t["strategy_id"])

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
                bars = await data.recent_bars(sym, spec["timeframe"], now=now)
            except Exception as e:  # noqa: BLE001
                self._last_bar[key] = bar
                self.event("error", f"{s['name']} {sym}: candles unavailable: {e}"[:300], strategy_id=s["id"])
                continue
            if not bars["t"] or bars["t"][-1] < bar:
                continue                                   # Fyers hasn't published it yet; next tick
            self._last_bar[key] = bar
            await self._decide(s, sym, bars, now, day_start)

    async def _decide(self, s: dict, sym: str, bars: dict, now: float, day_start: float) -> None:
        spec = s["spec"]
        sig = engine.signals(bars, spec, {})
        _, open_trades = await asyncio.to_thread(_snapshot)
        mine = next((t for t in open_trades if t["strategy_id"] == s["id"] and t["symbol"] == sym), None)
        if mine:
            if sig["exit_long" if mine["side"] == "BUY" else "exit_short"][-1]:
                px = (await _ltp([sym])).get(sym.upper())
                if px is not None:
                    await self._exit(mine, px, "SIGNAL", now)
            return
        close_min = int((bars["t"][-1] + bars["tf_s"] + IST) % 86400) // 60
        sess = spec["session"]
        if not engine._hhmm(sess["start"]) <= close_min <= engine._hhmm(sess["end"]):
            return
        side = "BUY" if sig["entry_long"][-1] else "SELL" if sig["entry_short"][-1] else None
        if side is None:
            return
        if await asyncio.to_thread(_trades_today, s["id"], sym, day_start) >= spec["max_trades_per_day"]:
            return
        await self._enter(s, sym, side, bars, now)

    async def _enter(self, s: dict, sym: str, side: str, bars: dict, now: float) -> None:
        spec = s["spec"]
        ltp = (await _ltp([sym])).get(sym.upper())
        if ltp is None:
            self.event("error", f"{s['name']}: {side} {sym} skipped — no live price (no synthetic fills)",
                       strategy_id=s["id"])
            return
        atr_vals = {p: engine.series(bars, {"ind": "ATR", "params": {"period": p}}, {})[-1]
                    for p in engine.atr_periods(spec)}
        sl, tg, trd = engine.entry_levels(spec, side, ltp, atr_vals)
        qty = engine.size(spec, ltp, sl)
        if qty < 1:
            self.event("error", f"{s['name']}: {side} {sym} skipped — sizing gave 0 shares", strategy_id=s["id"])
            return
        base = dict(strategy_id=s["id"], symbol=sym, side=side, quantity=qty, mode=s["mode"],
                    entry_at=_utc(now), entry_price=round(ltp, 2), stop_loss=sl and round(sl, 2),
                    target=tg and round(tg, 2), trail_dist=trd, best_price=ltp)
        order_id = None
        if s["mode"] == "live":
            acc, why = await asyncio.to_thread(_account, s["account_id"])
            ok, order_id, msg = (False, None, why or "") if acc is None else await _order(
                acc, sym, side, qty, exit_=False, strategy_id=s["id"])
            if not ok:
                await asyncio.to_thread(_insert, **base, status="rejected", entry_order_id=order_id,
                                        note=msg[:500])
                self.event("error", f"{s['name']}: LIVE {side} {sym} rejected: {msg}", strategy_id=s["id"])
                return
        # ponytail: the entry is booked at the LTP seen at order time; a live
        # MARKET fill can differ by the spread. Reconcile from the order book
        # if the gap ever matters for reporting.
        tid = await asyncio.to_thread(_insert, **base, status="open", entry_order_id=order_id)
        self.event("entry", f"{s['name']}: {side} {sym} x{qty} @ {ltp:.2f} ({s['mode']})",
                   trade_id=tid, strategy_id=s["id"])

    # -- data ----------------------------------------------------------------

    async def sync(self, strategies: list[dict], days: int = 5) -> dict[str, Any]:
        """Top up the 1-minute store for every symbol any strategy trades."""
        syms = sorted({x for s in strategies for x in s["spec"]["symbols"]})
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


def _strategy_account(strategy_id: int) -> Optional[int]:
    with db_session.SessionLocal() as db:
        s = db.get(AlgoStrategy, strategy_id)
        return s.account_id if s else None
