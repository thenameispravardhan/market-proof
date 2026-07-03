"""Trade management — the post-entry half of the workflow.

The execution Manager opens positions. The TradeManager *closes* them:
it watches the quote feed for every open position and exits when the
price hits the stop-loss, the target, or the time-based exit window
closes, and computes realised P&L on the way out. It also backs the
manual "close" / "square-off all" controls on the dashboard.

Flow:

  trade.executed (FILLED, BUY)
      -> load the signal's entry/SL/target levels
      -> register a ManagedPosition, ask the QuoteFeed to watch it
  every QUOTE_REFRESH_SECONDS:
      -> for each managed position, read the latest quote
      -> TIME_EXIT:  exit if now - opened_at >= max_hold_seconds
      -> long:  exit if last <= stop_loss (STOP) or last >= target (TARGET)
         short: exit if last >= stop_loss (STOP) or last <= target (TARGET)
      -> on exit: settle the position (qty -> 0), write a SELL/BUY
         trades row with realised P&L, mark the signal closed, publish
         `trade.closed` (notifications + UI relay pick it up)

Paper exits are settled directly in the DB + the paper backend's
in-memory book (authoritative, simple P&L). The design leaves a clear
seam for routing live exits through the broker.

Idempotent and crash-safe: a handler error is logged and the loop
keeps running; a position is only settled once (popped from the book
under the exit path).

Speed-trading rules (NEW):
  - TIME_EXIT: every position carries a `max_hold_seconds` (default
    `Settings.MAX_HOLD_SECONDS`, currently 1800s = 30 min). When the
    window closes we exit at the latest quote regardless of where
    price is relative to SL/target — captures the "20-30 min spike"
    rule. Re-armed after a restart from `Position.opened_at`.
  - Per-position `max_hold_seconds` overrides are honoured if the
    caller passes one (used by tests; future feature could let
    strategies set their own window).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.execution.market_data import MarketDataBus
from app.execution.quote_feed import QuoteFeed
from app.config import get_settings
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)


CHANNEL_TRADE_EXECUTED = "trade.executed"
CHANNEL_TRADE_CLOSED = "trade.closed"


@dataclass
class ManagedPosition:
    symbol: str
    quantity: int                 # signed: >0 long, <0 short
    entry: float
    stop_loss: Optional[float]
    target: Optional[float]
    signal_id: Optional[int] = None
    strategy_id: Optional[int] = None
    broker_account_id: Optional[int] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Maximum time (seconds) the position may stay open before the
    # trade manager force-closes it at the latest quote (TIME_EXIT).
    # Speed-trading rule: captures the "20-30 min spike" window so a
    # slow-mover never stays in the book past its useful life.
    # 0 / None disables the time exit (only SL/target apply).
    max_hold_seconds: int = 0
    # --- Trailing-stop / scale-out state (RISK.md §3). R = the initial
    # per-share risk (|entry - stop_loss| at open). The trailing stop
    # arms at +`trail_activate_r`·R and then trails the best price by
    # `trail_distance_r`·R; with scale-out on, half the position is taken
    # at +`scale_out_r`·R and the rest is trailed with no hard target.
    initial_risk: float = 0.0
    peak_price: Optional[float] = None      # best price seen (high long / low short)
    trail_active: bool = False
    scaled_out: bool = False
    scale_out_enabled: bool = False
    scale_out_r: float = 2.0
    trail_activate_r: float = 1.5
    trail_distance_r: float = 0.5

    def r_multiple_at(self, price: float) -> Optional[float]:
        """Reward (in R) at `price`, or None when R is unknown."""
        if not self.initial_risk or self.initial_risk <= 0:
            return None
        if self.quantity > 0:
            return (float(price) - self.entry) / self.initial_risk
        return (self.entry - float(price)) / self.initial_risk

    def time_exit_expired(self, now: Optional[datetime] = None) -> bool:
        """True iff a non-zero max_hold_seconds has elapsed since opened_at.

        `now` is injectable for tests. Naive `opened_at` is treated as
        UTC (matches the DB convention) before comparing.
        """
        if not self.max_hold_seconds or self.max_hold_seconds <= 0:
            return False
        cur = now or datetime.now(timezone.utc)
        opened = self.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=timezone.utc)
        return (cur - opened).total_seconds() >= float(self.max_hold_seconds)

    @property
    def has_hard_target(self) -> bool:
        """True when an explicit target is set.

        A hard target pins a full-exit level and takes precedence over
        scale-out / trailing: the operator's (or the analysis') bracket
        wins, so a configured target ALWAYS triggers a full exit and is
        never silently overridden. Scale-out + trailing only manage a
        position that has NO explicit target (an open-ended winner we let
        run on a trailing stop). See RISK.md §3.
        """
        return self.target is not None

    def exit_reason(self, last: float) -> Optional[str]:
        """Return 'STOP' / 'TARGET' if `last` triggers an exit, else None.

        Both levels are honoured whenever they're set: a hard target always
        fires a full exit (it is never overridden by scale-out), and the
        stop always applies. Scale-out / trailing only manage a position
        with no explicit target — see `has_hard_target` and `_sweep`.
        """
        if self.quantity > 0:  # long
            if self.stop_loss is not None and last <= self.stop_loss:
                return "STOP"
            if self.target is not None and last >= self.target:
                return "TARGET"
        elif self.quantity < 0:  # short
            if self.stop_loss is not None and last >= self.stop_loss:
                return "STOP"
            if self.target is not None and last <= self.target:
                return "TARGET"
        return None

    def apply_trailing(self, last: float) -> bool:
        """Update the peak and (once armed) ratchet the trailing stop.

        Returns True if the trailing stop became active / moved — purely
        informational. R must be known (initial_risk > 0); otherwise this
        is a no-op and only the original SL/target apply.
        """
        R = self.initial_risk
        if not R or R <= 0:
            return False
        # Track the best price seen since entry.
        if self.quantity > 0:
            self.peak_price = last if self.peak_price is None else max(self.peak_price, last)
            peak_r = (self.peak_price - self.entry) / R
        else:
            self.peak_price = last if self.peak_price is None else min(self.peak_price, last)
            peak_r = (self.entry - self.peak_price) / R
        if peak_r >= self.trail_activate_r:
            self.trail_active = True
        if not self.trail_active:
            return False
        # Ratchet the stop toward the best price, never loosening it.
        if self.quantity > 0:
            trail_stop = self.peak_price - self.trail_distance_r * R
            if self.stop_loss is None or trail_stop > self.stop_loss:
                self.stop_loss = trail_stop
        else:
            trail_stop = self.peak_price + self.trail_distance_r * R
            if self.stop_loss is None or trail_stop < self.stop_loss:
                self.stop_loss = trail_stop
        return True

    def realised_pnl(self, exit_price: float) -> float:
        # Long: (exit-entry)*qty ; Short: (entry-exit)*|qty|.
        return (exit_price - self.entry) * self.quantity


def _default_session_factory() -> Callable[[], Any]:
    from app.db.session import SessionLocal
    return SessionLocal


class TradeManager:
    """Watches open positions and exits them on SL / target / manual."""

    def __init__(
        self,
        *,
        market_data: MarketDataBus,
        quote_feed: Optional[QuoteFeed] = None,
        session_factory: Optional[Callable[[], Any]] = None,
        paper_backend: Optional[Any] = None,
    ) -> None:
        self._md = market_data
        self._quote_feed = quote_feed
        self._session_factory = session_factory or _default_session_factory()
        self._paper = paper_backend
        self._book: dict[str, ManagedPosition] = {}
        self._lock = asyncio.Lock()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._sub_queue: Optional[asyncio.Queue[Any]] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._task is not None and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._ready_event.clear()
        self._task = asyncio.create_task(self._run(), name="trade-manager")
        return self._task

    async def wait_until_ready(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._sub_queue is not None:
            try:
                event_bus.unsubscribe(CHANNEL_TRADE_EXECUTED, self._sub_queue)
            except Exception:  # noqa: BLE001
                pass
            self._sub_queue = None

    async def wait_until_stopped(self) -> None:
        if self._task is None:
            return
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # -- registration ----------------------------------------------------

    async def register(
        self,
        *,
        symbol: str,
        quantity: int,
        entry: float,
        stop_loss: Optional[float],
        target: Optional[float],
        signal_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        broker_account_id: Optional[int] = None,
        max_hold_seconds: Optional[int] = None,
    ) -> None:
        symbol = symbol.upper().strip()
        # Default the hold window to the global setting so the
        # speed-trading "20-30 min spike" rule applies unless an
        # override is passed (per-strategy in the future).
        if max_hold_seconds is None:
            try:
                max_hold_seconds = int(get_settings().MAX_HOLD_SECONDS)
            except Exception:  # noqa: BLE001
                max_hold_seconds = 0
        st = get_settings()
        # R = the per-share risk at open. Drives the trailing stop and the
        # scale-out take-profit. Zero when there's no stop (trailing off).
        initial_risk = (
            abs(float(entry) - float(stop_loss)) if stop_loss is not None else 0.0
        )
        mp = ManagedPosition(
            symbol=symbol,
            quantity=int(quantity),
            entry=float(entry),
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            target=float(target) if target is not None else None,
            signal_id=signal_id,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
            max_hold_seconds=int(max_hold_seconds or 0),
            initial_risk=initial_risk,
            scale_out_enabled=bool(getattr(st, "SCALE_OUT_ENABLED", True)),
            scale_out_r=float(getattr(st, "SCALE_OUT_R", 2.0)),
            trail_activate_r=float(getattr(st, "TRAIL_ACTIVATE_R", 1.5)),
            trail_distance_r=float(getattr(st, "TRAIL_DISTANCE_R", 0.5)),
        )
        async with self._lock:
            self._book[symbol] = mp
        if self._quote_feed is not None:
            self._quote_feed.watch(symbol, entry)
        # Persist the exit levels onto the position row so the book can be
        # rebuilt with them after a restart (it's otherwise in-memory only).
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_position_levels,
            self._session_factory, symbol, stop_loss, target,
        )
        log.info(
            "trade_manager.registered",
            symbol=symbol, quantity=quantity, entry=entry,
            stop_loss=stop_loss, target=target,
            max_hold_seconds=mp.max_hold_seconds,
        )

    async def update_levels(
        self,
        symbol: str,
        *,
        stop_loss: Optional[float],
        target: Optional[float],
    ) -> Optional[dict[str, Any]]:
        """Edit the stop-loss / target of an open position.

        Works for any open position, not just ones already in the
        in-memory managed book: if the symbol isn't being actively
        managed (opened before this process started, after a restart, or
        seeded), we reconstruct it from the DB and arm it so the new
        levels take effect immediately. Pass None for a level to clear it
        (disarms that exit). Returns the updated managed view, or None
        when there is no open position for the symbol.
        """
        symbol = symbol.upper().strip()
        loop = asyncio.get_running_loop()
        async with self._lock:
            mp = self._book.get(symbol)
        if mp is None:
            loaded = await loop.run_in_executor(
                None, _open_position_as_managed, self._session_factory, symbol
            )
            if loaded is None:
                return None
            mp, _ = loaded
            async with self._lock:
                self._book[symbol] = mp
            if self._quote_feed is not None:
                try:
                    self._quote_feed.watch(symbol, mp.entry)
                except Exception:  # noqa: BLE001
                    pass
        new_sl = float(stop_loss) if stop_loss is not None else None
        new_target = float(target) if target is not None else None
        async with self._lock:
            mp.stop_loss = new_sl
            mp.target = new_target
            # An explicit edit is an operator override: reset the trailing
            # baseline so the next sweep can't silently ratchet the new stop
            # back toward a previous peak (the "my edit doesn't stick" bug).
            # R is recomputed from the new stop so any trailing (which only
            # runs when there's no hard target) re-arms from here.
            mp.peak_price = None
            mp.trail_active = False
            mp.initial_risk = abs(mp.entry - new_sl) if new_sl is not None else 0.0
        # Persist onto the position row so the levels survive a restart.
        await loop.run_in_executor(
            None, _persist_position_levels,
            self._session_factory, symbol, new_sl, new_target,
        )
        log.info(
            "trade_manager.levels_updated",
            symbol=symbol, stop_loss=new_sl, target=new_target,
        )
        return {
            "symbol": mp.symbol,
            "quantity": mp.quantity,
            "entry": mp.entry,
            "stop_loss": mp.stop_loss,
            "target": mp.target,
            "signal_id": mp.signal_id,
            "strategy_id": mp.strategy_id,
            "opened_at": mp.opened_at.isoformat(),
        }

    async def _hydrate_book(self) -> None:
        """Rebuild the managed book from open position rows so stop-loss /
        target re-arm after a restart (the book is in-memory)."""
        loop = asyncio.get_running_loop()
        symbols = await loop.run_in_executor(
            None, _open_position_symbols, self._session_factory
        )
        for symbol in symbols:
            async with self._lock:
                if symbol in self._book:
                    continue
            loaded = await loop.run_in_executor(
                None, _open_position_as_managed, self._session_factory, symbol
            )
            if loaded is None:
                continue
            mp, _ = loaded
            async with self._lock:
                self._book[symbol] = mp
            if self._quote_feed is not None:
                try:
                    self._quote_feed.watch(symbol, mp.entry)
                except Exception:  # noqa: BLE001
                    pass
        log.info("trade_manager.hydrated", managed=len(self._book))

    def managed_positions(self) -> list[ManagedPosition]:
        return list(self._book.values())

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        self._sub_queue = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)
        # Re-arm stop-loss / target for positions opened before this
        # process started (their levels were persisted to the DB).
        try:
            await self._hydrate_book()
        except Exception:  # noqa: BLE001
            log.exception("trade_manager.hydrate_failed")
        log.info("trade_manager.start")
        self._ready_event.set()
        try:
            while not self._stop_event.is_set():
                # Drain any fill events first.
                drained = True
                while drained:
                    try:
                        evt = self._sub_queue.get_nowait()
                        await self._on_trade_executed(evt.payload)
                    except asyncio.QueueEmpty:
                        drained = False
                    except Exception:  # noqa: BLE001
                        log.exception("trade_manager.fill_handler_crashed")
                        drained = False
                # Then sweep the book for exits.
                try:
                    await self._sweep()
                except Exception:  # noqa: BLE001
                    log.exception("trade_manager.sweep_crashed")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
                    break
                except asyncio.TimeoutError:
                    pass
        finally:
            log.info("trade_manager.stop")
            if self._sub_queue is not None:
                event_bus.unsubscribe(CHANNEL_TRADE_EXECUTED, self._sub_queue)
                self._sub_queue = None

    async def _on_trade_executed(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state") or "").upper()
        side = str(payload.get("side") or "").upper()
        if state != "FILLED":
            return
        # Only entries open a managed position. Exits (the SELL we
        # issue) also publish trade.executed-like events via the
        # backend, but we route those through _exit, not here.
        signal_id = payload.get("signal_id")
        symbol = str(payload.get("symbol") or "").upper().strip()
        if not symbol:
            return
        async with self._lock:
            if symbol in self._book:
                return  # already managing this symbol
        # Prefer the coherent levels the manager computed (entry = the
        # real fill price). Only fall back to the signal rationale /
        # last quote when the payload omits them (older publishers).
        entry = payload.get("entry")
        stop_loss = payload.get("stop_loss")
        target = payload.get("target")
        if entry is None or stop_loss is None or target is None:
            levels = await asyncio.get_running_loop().run_in_executor(
                None, _load_signal_levels, self._session_factory, signal_id
            )
            entry = entry if entry is not None else levels.get("entry")
            stop_loss = stop_loss if stop_loss is not None else levels.get("stop_loss")
            target = target if target is not None else levels.get("target")
        if entry is None:
            quote = await self._md.get_quote(symbol)
            entry = float(quote.last_price) if quote is not None else None
        if entry is None:
            log.warning("trade_manager.no_entry_for_managed", symbol=symbol)
            return
        qty = int(payload.get("quantity") or 0)
        signed_qty = qty if side == "BUY" else -qty
        if signed_qty == 0:
            return
        # `max_hold_seconds` may be supplied by the publisher (e.g.
        # the execution manager can read the strategy config and pass
        # a per-trade override); otherwise `register` defaults to the
        # global MAX_HOLD_SECONDS setting.
        mh_payload = payload.get("max_hold_seconds")
        max_hold_seconds = int(mh_payload) if mh_payload is not None else None
        await self.register(
            symbol=symbol,
            quantity=signed_qty,
            entry=float(entry),
            stop_loss=stop_loss,
            target=target,
            signal_id=signal_id if isinstance(signal_id, int) else None,
            strategy_id=payload.get("strategy_id"),
            broker_account_id=payload.get("account_id"),
            max_hold_seconds=max_hold_seconds,
        )

    async def _sweep(self) -> None:
        loop = asyncio.get_running_loop()
        # EOD square-off (RISK.md §5): once the IST cutoff passes, flatten
        # everything and skip the rest of the sweep. No-op under TESTING.
        if await self.square_off_if_due():
            return
        # Track EVERY open position (not just the in-memory managed book —
        # which is empty after a restart). For each: make sure the quote
        # feed is fetching its price, mark-to-market the DB row (so the
        # dashboard shows live, moving P&L), and — for the ones we manage —
        # check SL/target exits AND the time-based exit window.
        db_symbols = await loop.run_in_executor(
            None, _open_position_symbols, self._session_factory
        )
        async with self._lock:
            book = dict(self._book)
        now = datetime.now(timezone.utc)
        for symbol in set(db_symbols) | set(book.keys()):
            # Idempotent: ensures the feed pulls a live price for this
            # symbol even if it was opened before this process started.
            try:
                self._quote_feed.watch(symbol)
            except Exception:  # noqa: BLE001
                pass
            quote = await self._md.get_quote(symbol)
            if quote is None:
                continue
            last = float(quote.last_price)
            # Persist the live price + unrealised P&L (no-op if unchanged).
            await loop.run_in_executor(
                None, _mark_to_market, self._session_factory, symbol, last
            )
            mp = book.get(symbol)
            if mp is not None:
                # TIME_EXIT runs FIRST — the speed-trading rule says the
                # hold window closes whether or not SL/target has been
                # hit. After a restart the in-memory book may not hold
                # the position yet (it rebuilds async from the DB), but
                # `Position.opened_at` is preserved on disk so the time
                # exit is still correct on the next sweep.
                if mp.time_exit_expired(now):
                    await self._exit(symbol, reason="TIME_EXIT", exit_price=last)
                    continue
                # Scale-out + trailing manage ONLY a position with no
                # explicit (hard) target — an open-ended winner we let run on
                # a trailing stop (RISK.md §3 — preserves the fat tail). When
                # a target IS set the operator's / analysis' bracket wins: the
                # hard stop + target govern the exit and we skip both here, so
                # a configured target is never silently overridden.
                if not mp.has_hard_target:
                    if mp.scale_out_enabled and not mp.scaled_out and mp.initial_risk > 0:
                        rmult = mp.r_multiple_at(last)
                        if rmult is not None and rmult >= mp.scale_out_r:
                            await self._scale_out(symbol, mp, last)
                    # Ratchet the trailing stop (mutates mp.stop_loss in place).
                    mp.apply_trailing(last)
                reason = mp.exit_reason(last)
                if reason is not None:
                    await self._exit(symbol, reason=reason, exit_price=last)

    # -- exits -----------------------------------------------------------

    async def close_position(
        self, symbol: str, *, reason: str = "MANUAL"
    ) -> Optional[dict[str, Any]]:
        """Manually close a position at the latest price.

        Works for any open position, not just ones in the in-memory
        managed book: if the symbol isn't being actively managed (e.g.
        it was opened before the trade manager started, after a restart,
        or seeded), we reconstruct a position from the DB so the
        operator can always flatten it from the dashboard.
        """
        symbol = symbol.upper().strip()
        quote = await self._md.get_quote(symbol)
        async with self._lock:
            mp = self._book.get(symbol)
        fallback_price: Optional[float] = None
        if mp is None:
            loaded = await asyncio.get_running_loop().run_in_executor(
                None, _open_position_as_managed, self._session_factory, symbol
            )
            if loaded is None:
                return None
            mp, fallback_price = loaded
        exit_price = (
            float(quote.last_price)
            if quote is not None
            else (fallback_price if fallback_price is not None else mp.entry)
        )
        return await self._exit(symbol, reason=reason, exit_price=exit_price, mp=mp)

    async def close_all(self, *, reason: str = "SQUARE_OFF") -> list[dict[str, Any]]:
        # Union of the managed book and every open position row, so
        # "square off all" really flattens everything.
        async with self._lock:
            symbols = set(self._book.keys())
        db_syms = await asyncio.get_running_loop().run_in_executor(
            None, _open_position_symbols, self._session_factory
        )
        symbols.update(db_syms)
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            res = await self.close_position(symbol, reason=reason)
            if res is not None:
                out.append(res)
        return out

    async def square_off_if_due(
        self, now: Optional[datetime] = None, *, force: bool = False
    ) -> list[dict[str, Any]]:
        """Flatten everything once the IST square-off time (default 15:10)
        passes — RISK.md §5, "all intraday positions squared off".

        Gated off under TESTING so unit tests aren't time-of-day
        dependent; pass `force=True` (or a `now` in the window) to
        exercise it directly. Returns the list of closed positions.
        """
        from app.risk import market_clock

        if not force and getattr(get_settings(), "TESTING", 0):
            return []
        if not market_clock.square_off_due(now):
            return []
        return await self.close_all(reason="EOD_SQUAREOFF")

    async def _exit(
        self,
        symbol: str,
        *,
        reason: str,
        exit_price: float,
        mp: Optional[ManagedPosition] = None,
    ) -> Optional[dict[str, Any]]:
        # Always drop the symbol from the managed book if present; use
        # the caller-supplied position (DB fallback) when it isn't.
        async with self._lock:
            booked = self._book.pop(symbol, None)
        mp = mp or booked
        if mp is None:
            return None
        pnl = mp.realised_pnl(exit_price)
        r_multiple = mp.r_multiple_at(exit_price)
        # Settle in the DB + in-memory paper book.
        await asyncio.get_running_loop().run_in_executor(
            None, _settle_exit,
            self._session_factory, mp, exit_price, pnl, reason, r_multiple,
        )
        if self._paper is not None:
            try:
                self._paper._positions.pop(symbol, None)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if self._quote_feed is not None:
            self._quote_feed.unwatch(symbol)
        log.info(
            "trade_manager.exit",
            symbol=symbol, reason=reason, exit_price=round(exit_price, 2),
            pnl=round(pnl, 2),
        )
        result = {
            "symbol": symbol,
            "reason": reason,
            "exit_price": exit_price,
            "quantity": mp.quantity,
            "entry": mp.entry,
            "pnl": pnl,
            "r_multiple": r_multiple,
            "signal_id": mp.signal_id,
            "strategy_id": mp.strategy_id,
        }
        await event_bus.publish(CHANNEL_TRADE_CLOSED, result)
        return result

    async def _scale_out(
        self, symbol: str, mp: ManagedPosition, last: float
    ) -> Optional[float]:
        """Take partial profit at the take-profit R and trail the rest.

        Closes half the position at `last`, books the realised P&L, moves
        the stop on the remainder to breakeven, and flags `scaled_out` so
        this fires once. If the position is too small to split (1 share)
        we skip the partial and just lock breakeven.
        """
        loop = asyncio.get_running_loop()
        sign = 1 if mp.quantity > 0 else -1
        half = abs(int(mp.quantity)) // 2
        if half < 1:
            mp.scaled_out = True
            mp.stop_loss = mp.entry
            await loop.run_in_executor(
                None, _persist_position_levels,
                self._session_factory, symbol, mp.stop_loss, mp.target,
            )
            return None
        closed_signed = half * sign
        pnl = (float(last) - mp.entry) * closed_signed
        r_multiple = mp.r_multiple_at(last)
        # Persist the partial close + reduce the position row (before we
        # mutate mp.quantity, so the close side is derived from the
        # original direction).
        await loop.run_in_executor(
            None, _partial_settle,
            self._session_factory, mp, closed_signed, float(last), pnl, r_multiple,
        )
        # Reduce the paper backend's in-memory book to match.
        if self._paper is not None:
            try:
                pos = self._paper._positions.get(symbol)  # type: ignore[attr-defined]
                if pos is not None:
                    pos.quantity -= closed_signed
            except Exception:  # noqa: BLE001
                pass
        # Remainder runs with a breakeven stop (then the trailing logic
        # ratchets it up from there).
        mp.quantity -= closed_signed
        mp.scaled_out = True
        mp.stop_loss = mp.entry
        async with self._lock:
            booked = self._book.get(symbol)
            if booked is not None:
                booked.quantity = mp.quantity
                booked.scaled_out = True
                booked.stop_loss = mp.entry
                booked.peak_price = mp.peak_price
        await loop.run_in_executor(
            None, _persist_position_levels,
            self._session_factory, symbol, mp.stop_loss, mp.target,
        )
        log.info(
            "trade_manager.scale_out",
            symbol=symbol, closed=half, remainder=mp.quantity,
            price=round(float(last), 2), pnl=round(pnl, 2),
        )
        await event_bus.publish(
            CHANNEL_TRADE_CLOSED,
            {
                "symbol": symbol, "reason": "SCALE_OUT", "exit_price": float(last),
                "quantity": closed_signed, "entry": mp.entry, "pnl": pnl,
                "r_multiple": r_multiple, "signal_id": mp.signal_id,
                "strategy_id": mp.strategy_id, "partial": True,
            },
        )
        return pnl


# -- DB helpers (sync, run in executor) ----------------------------------


def _open_position_symbols(session_factory: Callable[[], Any]) -> list[str]:
    """Symbols of every open (non-zero quantity) position row."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        rows = session.query(PositionRow).filter(PositionRow.quantity != 0).all()
        return [r.symbol for r in rows]


def _persist_position_levels(
    session_factory: Callable[[], Any],
    symbol: str,
    stop_loss: Optional[float],
    target: Optional[float],
) -> None:
    """Save the managed exit levels onto the position row so the book can
    be rebuilt with them after a restart."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        pos = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if pos is None:
            return
        pos.stop_loss = float(stop_loss) if stop_loss is not None else None
        pos.target = float(target) if target is not None else None
        session.commit()


def _mark_to_market(
    session_factory: Callable[[], Any], symbol: str, last_price: float
) -> None:
    """Update a position's last_price + unrealised P&L from the live quote
    so the dashboard shows moving P&L. Skips the write when the price hasn't
    changed (the sweep runs far more often than prices tick), keeping DB
    churn down."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        pos = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if pos is None or pos.quantity == 0 or last_price <= 0:
            return
        if pos.last_price is not None and abs(float(pos.last_price) - float(last_price)) < 0.01:
            return  # unchanged — skip the write
        pos.last_price = float(last_price)
        pos.unrealized_pnl = (float(last_price) - float(pos.average_price)) * pos.quantity
        session.commit()


def _open_position_as_managed(
    session_factory: Callable[[], Any], symbol: str
) -> Optional[tuple[ManagedPosition, float]]:
    """Reconstruct a ManagedPosition from a DB position row so it can be
    closed even when it isn't in the in-memory managed book.

    Returns (managed_position, fallback_exit_price) or None when there
    is no open position for the symbol.

    `max_hold_seconds` is re-derived from the live `Settings` so a
    position opened before a restart still honours the current rule
    (the alternative — persisting per-position max_hold_seconds —
    would silently override an operator's setting change for old
    positions, which is rarely what you want).
    """
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        row = (
            session.query(PositionRow)
            .filter(PositionRow.symbol == symbol, PositionRow.quantity != 0)
            .one_or_none()
        )
        if row is None:
            return None
        # `opened_at` is a naive UTC datetime in the SQLite schema.
        opened_at = row.opened_at
        if opened_at is None:
            opened_at = datetime.now(timezone.utc)
        st = get_settings()
        try:
            max_hold_seconds = int(st.MAX_HOLD_SECONDS)
        except Exception:  # noqa: BLE001
            max_hold_seconds = 0
        entry = float(row.average_price)
        stop_loss = float(row.stop_loss) if row.stop_loss is not None else None
        # Best-effort R after a restart (the persisted stop may already be
        # trailed, so this can understate R — trailing then resumes from
        # the current stop, which is safe).
        initial_risk = abs(entry - stop_loss) if stop_loss is not None else 0.0
        mp = ManagedPosition(
            symbol=row.symbol,
            quantity=int(row.quantity),
            entry=entry,
            stop_loss=stop_loss,
            target=float(row.target) if row.target is not None else None,
            signal_id=None,
            strategy_id=row.strategy_id,
            broker_account_id=None,
            opened_at=opened_at,
            max_hold_seconds=max_hold_seconds,
            initial_risk=initial_risk,
            scale_out_enabled=bool(getattr(st, "SCALE_OUT_ENABLED", True)),
            scale_out_r=float(getattr(st, "SCALE_OUT_R", 2.0)),
            trail_activate_r=float(getattr(st, "TRAIL_ACTIVATE_R", 1.5)),
            trail_distance_r=float(getattr(st, "TRAIL_DISTANCE_R", 0.5)),
        )
        fallback_price = float(
            row.last_price if row.last_price and row.last_price > 0 else row.average_price
        )
        return (mp, fallback_price)


def _load_signal_levels(
    session_factory: Callable[[], Any], signal_id: Optional[int]
) -> dict[str, float]:
    if not isinstance(signal_id, int):
        return {}
    from app.db.models import Signal
    from app.execution.manager import parse_rationale_levels

    with session_factory() as session:
        sig = session.get(Signal, signal_id)
        if sig is None:
            return {}
        return parse_rationale_levels(sig.rationale)


def _settle_exit(
    session_factory: Callable[[], Any],
    mp: ManagedPosition,
    exit_price: float,
    pnl: float,
    reason: str,
    r_multiple: Optional[float] = None,
) -> None:
    from app.db.models import AuditLog, Position as PositionRow, Signal, Trade as TradeRow

    close_side = "SELL" if mp.quantity > 0 else "BUY"
    with session_factory() as session:
        session.add(
            TradeRow(
                signal_id=mp.signal_id,
                broker_account_id=mp.broker_account_id,
                symbol=mp.symbol,
                side=close_side,
                quantity=abs(int(mp.quantity)),
                price=float(exit_price),
                order_type="market",
                status="filled",
                broker_order_id=None,
                pnl=float(pnl),
                r_multiple=float(r_multiple) if r_multiple is not None else None,
                executed_at=datetime.now(timezone.utc),
            )
        )
        # Flatten the position row.
        pos = session.query(PositionRow).filter_by(symbol=mp.symbol).one_or_none()
        if pos is not None:
            pos.quantity = 0
            pos.last_price = float(exit_price)
            pos.unrealized_pnl = 0.0
        # Mark the originating signal closed.
        if mp.signal_id is not None:
            sig = session.get(Signal, mp.signal_id)
            if sig is not None:
                sig.status = "closed"
        session.add(
            AuditLog(
                actor="system",
                action="trade.closed",
                target=f"signal:{mp.signal_id}" if mp.signal_id else f"symbol:{mp.symbol}",
                after={
                    "symbol": mp.symbol,
                    "reason": reason,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "r_multiple": r_multiple,
                },
            )
        )
        session.commit()


def _partial_settle(
    session_factory: Callable[[], Any],
    mp: ManagedPosition,
    closed_signed: int,
    exit_price: float,
    pnl: float,
    r_multiple: Optional[float] = None,
) -> None:
    """Book a partial (scale-out) close: write the realised-P&L trade
    row for the closed slice and reduce the position toward zero, leaving
    the remainder open with a breakeven stop. Does NOT close the signal."""
    from app.db.models import Position as PositionRow, Trade as TradeRow

    # Close side is opposite the position's direction (sign taken BEFORE
    # mp.quantity is reduced by the caller).
    close_side = "SELL" if mp.quantity > 0 else "BUY"
    with session_factory() as session:
        session.add(
            TradeRow(
                signal_id=mp.signal_id,
                broker_account_id=mp.broker_account_id,
                symbol=mp.symbol,
                side=close_side,
                quantity=abs(int(closed_signed)),
                price=float(exit_price),
                order_type="market",
                status="filled",
                broker_order_id=None,
                pnl=float(pnl),
                r_multiple=float(r_multiple) if r_multiple is not None else None,
                executed_at=datetime.now(timezone.utc),
            )
        )
        pos = session.query(PositionRow).filter_by(symbol=mp.symbol).one_or_none()
        if pos is not None:
            pos.quantity = int(pos.quantity) - int(closed_signed)
            pos.last_price = float(exit_price)
            pos.stop_loss = mp.entry  # breakeven on the remainder
        session.commit()
