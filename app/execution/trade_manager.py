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

Exit execution routes by account: paper exits settle directly in the
DB + the paper backend's in-memory book (the bot IS the paper broker);
LIVE exits go to the real broker first — a marketable-limit order
(bid/ask ∓ EXIT_BUFFER_PCT) that falls back to a MARKET order for any
quantity still unfilled at EXIT_FILL_TIMEOUT. A rejected exit retries
at market with backoff (EXIT_RETRY_MAX attempts); a broker call that
hangs past EXIT_BROKER_TIMEOUT gets ONE market retry, then the
position is flagged CRITICAL (system.error → operator alert) and
retried on the next cycle. The position is only settled locally once
the broker actually confirms (or the account is paper).

Realtime: every managed symbol has a tick listener on the shared
MarketDataBus — the Fyers WebSocket publishes straight into it — so
stop-loss / target / trailing / breakeven checks run ON EVERY TICK,
not on the old 5-second poll. The 0.5s sweep stays as a backstop for
time-based exits, mark-to-market persistence, hydration and the EOD
square-off. Price-triggered exits require a FRESH live tick (younger
than STALE_QUOTE_SECONDS); a frozen feed can never fire an exit at a
stale price — the static stop simply waits for the feed to return.

A 60-second reconciliation loop compares the broker's positions with
the managed book: a position the broker shows flat (manual close in
the Fyers app, margin-call square-off, missed fill callback) is marked
CLOSED_EXTERNAL locally and management stops.

Idempotent and crash-safe: a handler error is logged and the loop
keeps running; a position is only settled once (popped from the book
under the exit path).

Speed-trading rules:
  - TIME_EXIT: every position carries a `max_hold_seconds` (default
    `Settings.MAX_HOLD_SECONDS`). When the window closes we exit at
    the latest REAL price regardless of where price sits vs SL/target.
    Re-armed after a restart from `Position.opened_at`.
  - BREAKEVEN: once profit reaches BREAKEVEN_AT_PCT the stop locks to
    entry ± BREAKEVEN_LOCK_PCT — a winner can't become a loser.
  - CONSOLIDATION exit: profit in the [1%, 2.5%] band while the price
    is pinned inside CONSOLIDATION_RANGE_PCT for the whole window →
    take the small profit, free the capital.
  - STALL exit: a bigger winner (3–6%) whose rate-of-change collapses
    below STALL_ROC_PCT over the window → capture before reversion.
  - Hard stop ALWAYS wins over target when both are true on one tick
    (gap through both → the protective exit is the one that fires).
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    ProductType,
)
from app.execution.market_data import MarketDataBus
from app.execution.quote_feed import QuoteFeed
from app.config import get_settings
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)


CHANNEL_TRADE_EXECUTED = "trade.executed"
CHANNEL_TRADE_CLOSED = "trade.closed"
# Exit-failure escalations publish here — the NotificationManager relays
# `system.error` events to the operator's channels (Telegram / email).
CHANNEL_SYSTEM_ERROR = "system.error"


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
    # --- Exit-rule state (breakeven / consolidation / stall) and broker
    # exit bookkeeping. `price_history` holds (utc_ts, price) samples of
    # REAL ticks only, pruned to the largest rule window; the
    # consolidation / stall detectors read it. `exit_failures` counts
    # consecutive broker exit failures (rejections / timeouts); after the
    # retry budget the position is escalated ONCE (`escalated`) and
    # retried on a cooldown (`next_exit_retry_at`) instead of hammering
    # the broker every tick.
    breakeven_armed: bool = False
    scaling: bool = False
    exit_failures: int = 0
    escalated: bool = False
    next_exit_retry_at: Optional[datetime] = None
    price_history: deque = field(default_factory=deque)

    # Longest look-back any history-based rule needs; samples older than
    # this are pruned on every record.
    HISTORY_MAX_SECONDS = 300.0

    def record_price(self, price: float, ts: Optional[datetime] = None) -> None:
        """Append a REAL tick to the history and prune the window."""
        now = ts or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self.price_history.append((now, float(price)))
        cutoff = now.timestamp() - self.HISTORY_MAX_SECONDS
        while self.price_history and self.price_history[0][0].timestamp() < cutoff:
            self.price_history.popleft()

    def profit_pct_at(self, price: float) -> float:
        """Signed profit %% vs entry, direction-aware (+ = winning)."""
        if not self.entry or self.entry <= 0:
            return 0.0
        raw = (float(price) - self.entry) / self.entry * 100.0
        return raw if self.quantity > 0 else -raw

    def _window_samples(
        self, window_s: float, now: Optional[datetime] = None
    ) -> Optional[list[float]]:
        """Prices inside the trailing window, or None until the history
        actually COVERS the window (a rule must never fire off 3 ticks
        collected in the last second)."""
        cur = now or datetime.now(timezone.utc)
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=timezone.utc)
        start = cur.timestamp() - float(window_s)
        if not self.price_history:
            return None
        if self.price_history[0][0].timestamp() > start:
            return None  # oldest sample is younger than the window
        return [p for (t, p) in self.price_history if t.timestamp() >= start]

    def range_pct_over(
        self, window_s: float, now: Optional[datetime] = None
    ) -> Optional[float]:
        """(high - low) as %% of entry across the window; None until the
        window is fully covered."""
        prices = self._window_samples(window_s, now)
        if not prices or self.entry <= 0:
            return None
        return (max(prices) - min(prices)) / self.entry * 100.0

    def roc_pct_over(
        self, window_s: float, now: Optional[datetime] = None
    ) -> Optional[float]:
        """|last - first| as %% of entry across the window (rate of
        change); None until the window is fully covered."""
        prices = self._window_samples(window_s, now)
        if not prices or len(prices) < 2 or self.entry <= 0:
            return None
        return abs(prices[-1] - prices[0]) / self.entry * 100.0

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
        exit_backend_fn: Optional[
            Callable[[Optional[int]], Awaitable[Optional[Any]]]
        ] = None,
        live_positions_fn: Optional[Callable[[], Awaitable[Optional[list[Any]]]]] = None,
    ) -> None:
        self._md = market_data
        self._quote_feed = quote_feed
        self._session_factory = session_factory or _default_session_factory()
        self._paper = paper_backend
        # Broker seams. `exit_backend_fn(broker_account_id)` returns the
        # LIVE backend an exit order must be routed through, or None for
        # paper accounts (settle directly — the bot IS the paper broker).
        # `live_positions_fn()` returns the broker's current positions for
        # the reconciliation loop (None = feed unavailable, skip cycle).
        self._exit_backend_fn = exit_backend_fn
        self._live_positions_fn = live_positions_fn
        self._book: dict[str, ManagedPosition] = {}
        self._lock = asyncio.Lock()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._sub_queue: Optional[asyncio.Queue[Any]] = None
        # Realtime tick listeners: one task per managed symbol reading the
        # MarketDataBus subscription queue (the Fyers WS publishes into
        # it), so exits react to every tick instead of the sweep cadence.
        self._tick_tasks: dict[str, asyncio.Task[None]] = {}
        # Per-symbol evaluation locks: the sweep and the tick listener run
        # the SAME check concurrently — serialising them per symbol makes
        # an exit atomic (no double-exit on a tick storm, and a sweep
        # returns only after an in-flight tick exit fully settled).
        self._check_locks: dict[str, asyncio.Lock] = {}
        self._last_reconcile: float = 0.0

    def _check_lock(self, symbol: str) -> asyncio.Lock:
        lock = self._check_locks.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            self._check_locks[symbol] = lock
        return lock

    def attach_exit_router(
        self, fn: Callable[[Optional[int]], Awaitable[Optional[Any]]]
    ) -> None:
        """Wire the broker-account → live-backend resolver (the lifespan
        points this at the execution Manager). Without it every exit
        settles directly (pure paper mode)."""
        self._exit_backend_fn = fn

    def attach_live_positions_provider(
        self, fn: Callable[[], Awaitable[Optional[list[Any]]]]
    ) -> None:
        """Wire the broker positions feed for the reconciliation loop."""
        self._live_positions_fn = fn

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
        for task in list(self._tick_tasks.values()):
            task.cancel()
        self._tick_tasks.clear()
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
        # Validate the target's SIDE (T-17): a BUY target at/below entry
        # (or a SELL target at/above) is malformed — usually a bad LLM
        # level. Clamp it to a minimum 0.5R reward instead of managing a
        # position whose "target" would fire instantly; with no stop to
        # derive R from, drop the target (stop/time exits still protect).
        if target is not None and entry is not None:
            t, e = float(target), float(entry)
            wrong_side = (t <= e) if int(quantity) > 0 else (t >= e)
            if wrong_side:
                if stop_loss is not None and abs(e - float(stop_loss)) > 1e-9:
                    half_r = 0.5 * abs(e - float(stop_loss))
                    target = e + half_r if int(quantity) > 0 else e - half_r
                else:
                    target = None
                log.warning(
                    "trade_manager.target_clamped",
                    symbol=symbol, entry=e, bad_target=t,
                    clamped_target=target,
                )
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
        # Realtime: react to every tick for this symbol, not just sweeps.
        self._ensure_tick_listener(symbol)
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
            self._ensure_tick_listener(symbol)
        log.info("trade_manager.hydrated", managed=len(self._book))

    def managed_positions(self) -> list[ManagedPosition]:
        return list(self._book.values())

    def _is_real_quote(self, quote: Any) -> bool:
        """True when `quote` is a price we may settle / mark a real-entry
        position against.

        Mirrors the entry guard (`QuoteFeed.seed_symbol`): an entry is only
        ever filled at a real Fyers price, so the exit and mark-to-market
        must use the same source — otherwise a real entry gets exited at a
        synthetic hash anchor and books a phantom P&L (PPLPHARMA
        ₹172.98→₹1,641, SOLEX ₹1,060→₹654). A quote is real when:

          - no live feed is wired at all (pure offline paper / tests): the
            synthetic simulator is the only, self-consistent source, so
            entry AND marking share it — safe to settle against; or
          - a live feed IS wired and this tick came from Fyers
            (``source`` in {fyers, fyers_ws}) and is NOT flagged
            ``simulated``.

        When a live feed is wired but the tick is simulated (feed cold,
        fell back to the hash walk) this returns False and the caller holds
        the position rather than settling at a fabricated price.
        """
        if quote is None:
            return False
        if self._quote_feed is None or not self._quote_feed.has_live_feed():
            return True
        extra = quote.extra or {}
        if extra.get("simulated"):
            return False
        return extra.get("source") in ("fyers", "fyers_ws")

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
                # Broker/book reconciliation on its own slower cadence
                # (catches manual closes in the Fyers app, margin-call
                # square-offs, and any fill callback we missed).
                interval = float(
                    getattr(get_settings(), "POSITION_RECONCILE_SECONDS", 60) or 60
                )
                if time.monotonic() - self._last_reconcile >= interval:
                    self._last_reconcile = time.monotonic()
                    try:
                        await self._reconcile_positions()
                    except Exception:  # noqa: BLE001
                        log.exception("trade_manager.reconcile_crashed")
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
            # A real entry must be marked AND exited against a REAL price. If
            # a live feed is wired but this tick is the synthetic fallback
            # (feed cold → hash walk), HOLD: don't mark a phantom P&L and
            # don't fire SL/target/TIME_EXIT at a fabricated price. The
            # position resumes as soon as a real Fyers tick lands (or is
            # flattened at the last real price by the EOD square-off).
            if not self._is_real_quote(quote):
                log.debug(
                    "trade_manager.skip_synthetic_quote",
                    symbol=symbol, last=round(float(quote.last_price), 2),
                )
                continue
            last = float(quote.last_price)
            # Persist the live price + unrealised P&L (no-op if unchanged).
            await loop.run_in_executor(
                None, _mark_to_market, self._session_factory, symbol, last
            )
            mp = book.get(symbol)
            if mp is not None:
                # Backstop: the tick listener normally evaluates every
                # tick in realtime; the sweep re-runs the same check so
                # time exits fire even when the feed goes quiet.
                self._ensure_tick_listener(symbol)
                await self._check_position(symbol, mp, quote, now)
        # Barrier: a tick-listener exit may still be mid-settle. Taking
        # each busy check-lock once means a COMPLETED sweep guarantees
        # every in-flight exit has fully committed — callers (and tests)
        # can treat the sweep as a synchronisation point.
        for lock in [l for l in self._check_locks.values() if l.locked()]:
            async with lock:
                pass

    # -- realtime tick engine ---------------------------------------------

    def _ensure_tick_listener(self, symbol: str) -> None:
        """Start (once) the per-symbol tick listener that evaluates exits
        on EVERY MarketDataBus tick — the Fyers WebSocket publishes into
        the bus, so stop/target/trailing react in realtime instead of on
        the sweep cadence. No-op when no event loop runs (sync tests)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        existing = self._tick_tasks.get(symbol)
        if existing is not None and not existing.done():
            return
        self._tick_tasks[symbol] = asyncio.create_task(
            self._tick_loop(symbol), name=f"trade-ticks-{symbol}"
        )

    async def _tick_loop(self, symbol: str) -> None:
        q = self._md.subscribe(symbol)
        try:
            while not self._stop_event.is_set():
                try:
                    quote = await asyncio.wait_for(q.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    # No tick — check the book so the listener winds down
                    # once the position is gone (exit / external close).
                    async with self._lock:
                        if symbol not in self._book:
                            return
                    continue
                async with self._lock:
                    mp = self._book.get(symbol)
                if mp is None:
                    return
                try:
                    await self._check_position(symbol, mp, quote)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "trade_manager.tick_check_crashed", symbol=symbol
                    )
        except asyncio.CancelledError:
            raise
        finally:
            self._md.unsubscribe(symbol, q)
            # current_task() raises when the coroutine is finalised by GC
            # after the loop closed (test teardown) — treat as None.
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if current is not None and self._tick_tasks.get(symbol) is current:
                self._tick_tasks.pop(symbol, None)

    def _quote_fresh(self, quote: Any, now: Optional[datetime] = None) -> bool:
        """False when a live feed is wired but this tick is older than
        STALE_QUOTE_SECONDS — a frozen feed (WS disconnect) must never
        fire a price-based exit at a stale price (T-25). Offline paper
        mode has no external feed to lose, so it's always fresh."""
        if self._quote_feed is None or not self._quote_feed.has_live_feed():
            return True
        ts = getattr(quote, "timestamp", None)
        if ts is None:
            return False
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        cur = now or datetime.now(timezone.utc)
        max_age = float(getattr(get_settings(), "STALE_QUOTE_SECONDS", 5.0))
        return (cur - ts).total_seconds() <= max_age

    async def _check_position(
        self,
        symbol: str,
        mp: ManagedPosition,
        quote: Any,
        now: Optional[datetime] = None,
    ) -> None:
        """Evaluate ONE position against ONE quote. Shared by the sweep
        and the realtime tick listeners; every rule lives here so both
        paths behave identically.

        Rule order (deliberate):
          1. TIME_EXIT — time-based, allowed even on a stale tick (it
             settles at the last known REAL price).
          2. freshness gate — everything below needs a live tick (T-25).
          3. breakeven lock (tighten-only stop move).
          4. scale-out + trailing (open-ended winners only).
          5. hard stop / target — STOP CHECKED FIRST, so when a gap puts
             both in play at once the protective exit wins (T-13).
          6. consolidation exit, then stall exit.
        """
        now = now or datetime.now(timezone.utc)
        if not self._is_real_quote(quote):
            return
        async with self._check_lock(symbol):
            # Re-validate under the lock: a concurrent tick/sweep may have
            # already exited this position while we waited.
            async with self._lock:
                if self._book.get(symbol) is not mp:
                    return
            await self._check_position_locked(symbol, mp, quote, now)

    async def _check_position_locked(
        self,
        symbol: str,
        mp: ManagedPosition,
        quote: Any,
        now: datetime,
    ) -> None:
        last = float(quote.last_price)
        fresh = self._quote_fresh(quote, now)
        if fresh:
            mp.record_price(last, now)
        # Cooldown after a failed broker exit: don't hammer a broken
        # broker on every tick; the sweep retries after the backoff.
        if mp.next_exit_retry_at is not None and now < mp.next_exit_retry_at:
            return
        # 1. TIME_EXIT first — the hold window closes whether or not
        #    SL/target was hit (speed-trading rule).
        if mp.time_exit_expired(now):
            await self._exit(symbol, reason="TIME_EXIT", exit_price=last)
            return
        # 2. Stale tick → static stop only; wait for the feed (T-25).
        if not fresh:
            return
        st = get_settings()
        # 3. Breakeven lock: at +BREAKEVEN_AT_PCT the stop moves to
        #    entry ± BREAKEVEN_LOCK_PCT (never loosening an existing
        #    tighter stop). One-shot per position.
        if (
            bool(getattr(st, "BREAKEVEN_ENABLED", True))
            and not mp.breakeven_armed
            and mp.entry > 0
            and mp.profit_pct_at(last) >= float(getattr(st, "BREAKEVEN_AT_PCT", 2.0))
        ):
            lock_frac = float(getattr(st, "BREAKEVEN_LOCK_PCT", 0.2)) / 100.0
            if mp.quantity > 0:
                lock = mp.entry * (1.0 + lock_frac)
                if mp.stop_loss is None or lock > mp.stop_loss:
                    mp.stop_loss = lock
            else:
                lock = mp.entry * (1.0 - lock_frac)
                if mp.stop_loss is None or lock < mp.stop_loss:
                    mp.stop_loss = lock
            mp.breakeven_armed = True
            await asyncio.get_running_loop().run_in_executor(
                None, _persist_position_levels,
                self._session_factory, symbol, mp.stop_loss, mp.target,
            )
            log.info(
                "trade_manager.breakeven_armed",
                symbol=symbol, stop_loss=round(float(mp.stop_loss), 4),
            )
        # 4. Scale-out + trailing manage ONLY a position with no explicit
        #    (hard) target — an open-ended winner we let run on a trailing
        #    stop (RISK.md §3). A configured target is never overridden.
        if not mp.has_hard_target:
            if (
                mp.scale_out_enabled
                and not mp.scaled_out
                and not mp.scaling
                and mp.initial_risk > 0
            ):
                rmult = mp.r_multiple_at(last)
                if rmult is not None and rmult >= mp.scale_out_r:
                    await self._scale_out(symbol, mp, last)
                    async with self._lock:
                        if symbol not in self._book:
                            return
            # Ratchet the trailing stop (mutates mp.stop_loss in place).
            mp.apply_trailing(last)
        # 5. Hard stop / target (stop first — T-13).
        reason = mp.exit_reason(last)
        if reason is not None:
            await self._exit(symbol, reason=reason, exit_price=last)
            return
        # 6. Consolidation: small winner pinned in a tight range for the
        #    whole window → take the profit, free the capital (T-05).
        profit = mp.profit_pct_at(last)
        if bool(getattr(st, "CONSOLIDATION_EXIT_ENABLED", True)):
            lo = float(getattr(st, "CONSOLIDATION_MIN_PROFIT_PCT", 1.0))
            hi = float(getattr(st, "CONSOLIDATION_MAX_PROFIT_PCT", 2.5))
            if lo <= profit <= hi:
                rng = mp.range_pct_over(
                    float(getattr(st, "CONSOLIDATION_WINDOW_SECONDS", 120)), now
                )
                if rng is not None and rng <= float(
                    getattr(st, "CONSOLIDATION_RANGE_PCT", 0.5)
                ):
                    await self._exit(
                        symbol, reason="CONSOLIDATION", exit_price=last
                    )
                    return
        # 7. Stall: a bigger winner whose momentum died → capture before
        #    the reversion (T-06).
        if bool(getattr(st, "STALL_EXIT_ENABLED", True)):
            lo = float(getattr(st, "STALL_MIN_PROFIT_PCT", 3.0))
            hi = float(getattr(st, "STALL_MAX_PROFIT_PCT", 6.0))
            if lo <= profit <= hi:
                roc = mp.roc_pct_over(
                    float(getattr(st, "STALL_WINDOW_SECONDS", 90)), now
                )
                if roc is not None and roc < float(getattr(st, "STALL_ROC_PCT", 0.3)):
                    await self._exit(symbol, reason="STALL", exit_price=last)
                    return

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
        loop = asyncio.get_running_loop()
        quote = await self._md.get_quote(symbol)
        async with self._lock:
            mp = self._book.get(symbol)
        fallback_price: Optional[float] = None
        if mp is None:
            loaded = await loop.run_in_executor(
                None, _open_position_as_managed, self._session_factory, symbol
            )
            if loaded is None:
                return None
            mp, fallback_price = loaded
        # A synthetic tick (live feed cold → hash fallback) must NEVER set
        # the exit basis — that books a phantom P&L. Prefer a REAL live
        # price; otherwise settle at the last real mark persisted on the
        # position row (marking skips synthetic ticks, so it stays real),
        # else the real entry. This keeps a manual close / EOD square-off
        # coherent even when the feed is down.
        if quote is not None and self._is_real_quote(quote):
            exit_price = float(quote.last_price)
        else:
            if fallback_price is None:
                fallback_price = await loop.run_in_executor(
                    None, _row_exit_fallback, self._session_factory, symbol
                )
            exit_price = fallback_price if fallback_price is not None else mp.entry
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
        route: bool = True,
    ) -> Optional[dict[str, Any]]:
        # Always drop the symbol from the managed book if present; use
        # the caller-supplied position (DB fallback) when it isn't.
        async with self._lock:
            booked = self._book.pop(symbol, None)
        mp = mp or booked
        if mp is None:
            return None
        settle_price = float(exit_price)
        broker_order_id: Optional[str] = None
        # LIVE positions flatten at the BROKER first — the local settle
        # only happens once the broker confirms. Paper (backend None)
        # settles directly: the bot is the paper broker. `route=False`
        # is the reconciliation path (the broker already closed it).
        backend = await self._exit_backend(mp) if route else None
        if backend is not None:
            routed = await self._execute_exit_order(
                backend, mp,
                qty=abs(int(mp.quantity)),
                price_hint=settle_price,
                reason=reason,
                chase_remainder=True,
            )
            if routed is None:
                # Broker refused / timed out: the position is STILL LIVE
                # at the broker. Put it back under management, apply a
                # retry cooldown, and escalate once past the budget.
                mp.exit_failures += 1
                backoff = min(60.0, 5.0 * (2 ** max(0, mp.exit_failures - 1)))
                mp.next_exit_retry_at = datetime.now(timezone.utc) + timedelta(
                    seconds=backoff
                )
                async with self._lock:
                    self._book.setdefault(symbol, mp)
                max_retries = int(
                    getattr(get_settings(), "EXIT_RETRY_MAX", 3) or 3
                )
                if mp.exit_failures >= max_retries and not mp.escalated:
                    mp.escalated = True
                    await self._escalate_exit_failure(mp, reason)
                log.error(
                    "trade_manager.exit_failed",
                    symbol=symbol, reason=reason,
                    failures=mp.exit_failures, retry_in_s=backoff,
                )
                return None
            filled_qty, avg_price, broker_order_id = routed
            if avg_price is not None and avg_price > 0:
                settle_price = float(avg_price)
            mp.exit_failures = 0
            mp.next_exit_retry_at = None
        pnl = mp.realised_pnl(settle_price)
        r_multiple = mp.r_multiple_at(settle_price)
        # Settle in the DB + in-memory paper book.
        await asyncio.get_running_loop().run_in_executor(
            None, _settle_exit,
            self._session_factory, mp, settle_price, pnl, reason, r_multiple,
            broker_order_id,
        )
        if self._paper is not None:
            try:
                self._paper._positions.pop(symbol, None)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if self._quote_feed is not None:
            self._quote_feed.unwatch(symbol)
        # Wind down the realtime listener (it also self-terminates when
        # it notices the book no longer holds the symbol).
        task = self._tick_tasks.get(symbol)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            self._tick_tasks.pop(symbol, None)
        log.info(
            "trade_manager.exit",
            symbol=symbol, reason=reason, exit_price=round(settle_price, 2),
            pnl=round(pnl, 2), broker_order_id=broker_order_id,
        )
        result = {
            "symbol": symbol,
            "reason": reason,
            "exit_price": settle_price,
            "quantity": mp.quantity,
            "entry": mp.entry,
            "pnl": pnl,
            "r_multiple": r_multiple,
            "signal_id": mp.signal_id,
            "strategy_id": mp.strategy_id,
            "broker_order_id": broker_order_id,
        }
        await event_bus.publish(CHANNEL_TRADE_CLOSED, result)
        return result

    # -- broker-routed exit execution --------------------------------------

    async def _exit_backend(self, mp: ManagedPosition) -> Optional[Any]:
        """The live backend this position's exit must route through, or
        None to settle directly (paper account / no router wired)."""
        if self._exit_backend_fn is None:
            return None
        try:
            return await self._exit_backend_fn(mp.broker_account_id)
        except Exception:  # noqa: BLE001
            log.exception(
                "trade_manager.exit_backend_lookup_failed",
                symbol=mp.symbol, broker_account_id=mp.broker_account_id,
            )
            return None

    async def _execute_exit_order(
        self,
        backend: Any,
        mp: ManagedPosition,
        *,
        qty: int,
        price_hint: float,
        reason: str,
        chase_remainder: bool = True,
    ) -> Optional[tuple[int, Optional[float], Optional[str]]]:
        """Flatten `qty` shares at the broker.

        Strategy (T-01/T-10/T-11/T-12/T-24):
          1. Marketable LIMIT at bid/ask ∓ EXIT_BUFFER_PCT (IOC).
          2. Anything unfilled at EXIT_FILL_TIMEOUT → cancel; when
             `chase_remainder`, chase the remainder with a MARKET order
             (protective exits MUST complete); scale-outs don't chase.
          3. Broker REJECTED → retry at MARKET with backoff, up to
             EXIT_RETRY_MAX total attempts.
          4. Broker call hangs past EXIT_BROKER_TIMEOUT → ONE market
             retry, then give up (caller escalates + retries next cycle).

        Returns (filled_qty, avg_price, broker_order_id) — filled_qty may
        be less than `qty` only when `chase_remainder=False`. None means
        NOTHING was confirmed and the position must stay under management.
        """
        st = get_settings()
        buffer = float(getattr(st, "EXIT_BUFFER_PCT", 0.2)) / 100.0
        broker_timeout = float(getattr(st, "EXIT_BROKER_TIMEOUT_SECONDS", 3.0))
        fill_timeout = float(getattr(st, "EXIT_FILL_TIMEOUT_SECONDS", 2.0))
        max_attempts = max(1, int(getattr(st, "EXIT_RETRY_MAX", 3) or 3))
        exit_side = OrderSide.SELL if mp.quantity > 0 else OrderSide.BUY
        # Marketable limit basis: the touch we transact at, buffered so a
        # moving book still fills (SELL → bid - buffer, BUY → ask + buffer).
        quote = await self._md.get_quote(mp.symbol)
        if exit_side == OrderSide.SELL:
            base = (quote.bid if quote and quote.bid else None) or (
                float(quote.last_price) if quote else price_hint
            )
            limit_price = float(base) * (1.0 - buffer)
        else:
            base = (quote.ask if quote and quote.ask else None) or (
                float(quote.last_price) if quote else price_hint
            )
            limit_price = float(base) * (1.0 + buffer)

        async def _place(order_type: OrderType, lp: Optional[float]) -> Optional[OrderResult]:
            """One place_order call, wrapped in the broker timeout.
            Returns None on timeout (T-24: the order MAY be live — the
            caller decides; we never blind-fire a duplicate here)."""
            try:
                return await asyncio.wait_for(
                    backend.place_order(
                        signal=None,
                        symbol=mp.symbol,
                        side=exit_side,
                        quantity=int(qty),
                        order_type=order_type,
                        limit_price=lp,
                        stop_price=None,
                        product_type=ProductType.INTRADAY,
                        validity="IOC" if order_type == OrderType.LIMIT else "DAY",
                    ),
                    timeout=broker_timeout,
                )
            except asyncio.TimeoutError:
                return None
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception(
                    "trade_manager.exit_place_crashed", symbol=mp.symbol
                )
                return None

        result = await _place(OrderType.LIMIT, limit_price)
        if result is None:
            # Broker silent on the limit attempt → one market retry (T-24).
            log.warning(
                "trade_manager.exit_broker_timeout_market_retry",
                symbol=mp.symbol, reason=reason,
            )
            result = await _place(OrderType.MARKET, None)
            if result is None:
                return None
        # Rejected → market retries with backoff (T-12).
        attempts = 1
        backoff = 1.0
        while result.state == OrderState.REJECTED and attempts < max_attempts:
            await asyncio.sleep(backoff)
            backoff *= 2
            attempts += 1
            log.warning(
                "trade_manager.exit_rejected_retry",
                symbol=mp.symbol, attempt=attempts, error=result.error,
            )
            retried = await _place(OrderType.MARKET, None)
            if retried is None:
                return None
            result = retried
        if result.state == OrderState.REJECTED:
            return None

        filled = int(result.filled_quantity or 0)
        avg = float(result.average_price) if result.average_price else None
        order_id = result.broker_order_id or None
        if result.state == OrderState.FILLED and filled >= int(qty):
            return (filled, avg, order_id)

        # PENDING (live broker acks then fills async) → poll the order
        # up to the fill window; IOC EXPIRED/CANCELLED may carry a
        # partial fill.
        if result.state == OrderState.PENDING and order_id:
            deadline = asyncio.get_running_loop().time() + fill_timeout
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
                try:
                    status = await backend.get_order_status(order_id)
                except Exception:  # noqa: BLE001
                    continue
                if int(getattr(status, "filled_quantity", 0) or 0) > filled:
                    filled = int(status.filled_quantity)
                    if getattr(status, "average_price", None):
                        avg = float(status.average_price)
                if getattr(status, "state", None) == OrderState.FILLED:
                    return (max(filled, int(qty)), avg, order_id)
                if getattr(status, "state", None) in (
                    OrderState.REJECTED,
                    OrderState.CANCELLED,
                    OrderState.EXPIRED,
                ):
                    break
            # Window closed with the order possibly still resting: cancel
            # the remainder before deciding what's left to do.
            try:
                await backend.cancel_order(order_id)
            except Exception:  # noqa: BLE001
                pass

        remainder = int(qty) - filled
        if remainder <= 0:
            return (filled, avg, order_id)
        if not chase_remainder:
            # Scale-out semantics (T-22): don't chase a profit-take; the
            # unfilled slice simply stays managed.
            return (filled, avg, order_id) if filled > 0 else None
        # Protective exit MUST complete: market the remainder (T-11).
        log.warning(
            "trade_manager.exit_partial_market_chase",
            symbol=mp.symbol, filled=filled, remainder=remainder,
        )
        chase = await self._market_chase(backend, mp, exit_side, remainder)
        if chase is None:
            return (filled, avg, order_id) if filled > 0 else None
        chase_qty, chase_avg, chase_id = chase
        total = filled + chase_qty
        if avg is not None and chase_avg is not None and total > 0:
            avg = (avg * filled + chase_avg * chase_qty) / total
        elif chase_avg is not None:
            avg = chase_avg
        return (total, avg, order_id or chase_id)

    async def _market_chase(
        self, backend: Any, mp: ManagedPosition, side: OrderSide, qty: int
    ) -> Optional[tuple[int, Optional[float], Optional[str]]]:
        """MARKET order for an exit remainder. Best-effort short poll for
        the fill; a PENDING market order is counted as filled at the
        broker's eventual price (the order-WS reconcile corrects the trade
        row if it differs)."""
        st = get_settings()
        broker_timeout = float(getattr(st, "EXIT_BROKER_TIMEOUT_SECONDS", 3.0))
        try:
            result = await asyncio.wait_for(
                backend.place_order(
                    signal=None,
                    symbol=mp.symbol,
                    side=side,
                    quantity=int(qty),
                    order_type=OrderType.MARKET,
                    limit_price=None,
                    stop_price=None,
                    product_type=ProductType.INTRADAY,
                    validity="DAY",
                ),
                timeout=broker_timeout,
            )
        except asyncio.TimeoutError:
            return None
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("trade_manager.market_chase_crashed", symbol=mp.symbol)
            return None
        if result.state == OrderState.REJECTED:
            return None
        filled = int(result.filled_quantity or 0)
        avg = float(result.average_price) if result.average_price else None
        order_id = result.broker_order_id or None
        if result.state == OrderState.FILLED and filled > 0:
            return (filled, avg, order_id)
        if order_id:
            # Brief poll; then trust the market order to fill.
            deadline = asyncio.get_running_loop().time() + 1.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
                try:
                    status = await backend.get_order_status(order_id)
                except Exception:  # noqa: BLE001
                    continue
                if getattr(status, "state", None) == OrderState.FILLED:
                    return (
                        int(status.filled_quantity or qty),
                        float(status.average_price)
                        if getattr(status, "average_price", None)
                        else avg,
                        order_id,
                    )
        return (int(qty), avg, order_id)

    async def _escalate_exit_failure(self, mp: ManagedPosition, reason: str) -> None:
        """The exit retry budget is exhausted and the position is STILL
        OPEN at the broker: persist a CRITICAL risk event and publish
        `system.error` so the operator's channels (Telegram / email)
        fire. The position stays under management and keeps retrying on
        a cooldown — it is never silently abandoned (T-12/T-24)."""
        message = (
            f"EXIT FAILED for {mp.symbol} (reason={reason}): broker refused/"
            f"timed out {mp.exit_failures} times; position is still open. "
            "Manual intervention may be required."
        )
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_exit_failure,
            self._session_factory, mp, reason, message,
        )
        await event_bus.publish(
            CHANNEL_SYSTEM_ERROR,
            {
                "source": "trade_manager",
                "code": "EXIT_FAILED",
                "symbol": mp.symbol,
                "reason": reason,
                "quantity": mp.quantity,
                "message": message,
            },
        )

    # -- broker/book reconciliation -----------------------------------------

    @staticmethod
    def _normalize_symbol(raw: str) -> str:
        """Broker symbol → managed-book key: strip the exchange prefix and
        the series suffix (``NSE:RELIANCE-EQ`` → ``RELIANCE``)."""
        s = str(raw or "").upper().strip()
        if ":" in s:
            s = s.split(":", 1)[1]
        if "-" in s:
            s = s.rsplit("-", 1)[0]
        return s

    async def _reconcile_positions(self) -> None:
        """Compare the broker's book with ours (T-15/T-26/T-27).

        A LIVE position the broker shows flat was closed outside the bot
        (manual close in the Fyers app, margin-call square-off, or a fill
        callback we missed): stop managing it, settle it locally as
        CLOSED_EXTERNAL at the last known real price — WITHOUT placing
        any order — and tell the operator. A live position whose broker
        quantity shrank externally is resized down so the remaining
        management (and any later exit) matches reality.
        """
        if self._live_positions_fn is None:
            return
        try:
            broker_positions = await self._live_positions_fn()
        except Exception:  # noqa: BLE001
            log.exception("trade_manager.reconcile_fetch_failed")
            return
        if broker_positions is None:
            return  # feed unavailable this cycle — do NOT assume flat
        net: dict[str, int] = {}
        for p in broker_positions:
            key = self._normalize_symbol(getattr(p, "symbol", ""))
            if key:
                net[key] = net.get(key, 0) + int(getattr(p, "quantity", 0) or 0)
        async with self._lock:
            book = dict(self._book)
        loop = asyncio.get_running_loop()
        for symbol, mp in book.items():
            # Only live positions reconcile against the broker; for paper
            # the bot IS the broker.
            backend = await self._exit_backend(mp)
            if backend is None:
                continue
            broker_qty = net.get(symbol, 0)
            if broker_qty == 0:
                log.warning(
                    "trade_manager.closed_external",
                    symbol=symbol, local_qty=mp.quantity,
                )
                fallback = await loop.run_in_executor(
                    None, _row_exit_fallback, self._session_factory, symbol
                )
                await self._exit(
                    symbol,
                    reason="CLOSED_EXTERNAL",
                    exit_price=fallback if fallback is not None else mp.entry,
                    mp=mp,
                    route=False,  # the broker already closed it
                )
                await event_bus.publish(
                    CHANNEL_SYSTEM_ERROR,
                    {
                        "source": "trade_manager",
                        "code": "CLOSED_EXTERNAL",
                        "symbol": symbol,
                        "message": (
                            f"{symbol}: broker shows the position flat; "
                            "closed outside the bot (manual close / margin "
                            "call). Local state synced."
                        ),
                    },
                )
            elif abs(broker_qty) < abs(mp.quantity):
                log.warning(
                    "trade_manager.resized_external",
                    symbol=symbol, local_qty=mp.quantity, broker_qty=broker_qty,
                )
                mp.quantity = broker_qty
                await loop.run_in_executor(
                    None, _persist_position_quantity,
                    self._session_factory, symbol, broker_qty,
                )

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
        # Re-entrancy guard: a tick burst must not fire two scale-outs
        # while the first order is still at the broker.
        if mp.scaling:
            return None
        mp.scaling = True
        fill_price = float(last)
        try:
            backend = await self._exit_backend(mp)
            if backend is not None:
                # LIVE: the profit-take goes to the broker. A partial fill
                # is NOT chased (T-22) — whatever didn't sell simply stays
                # in the runner; risk only shrinks.
                routed = await self._execute_exit_order(
                    backend, mp,
                    qty=half,
                    price_hint=float(last),
                    reason="SCALE_OUT",
                    chase_remainder=False,
                )
                if routed is None:
                    return None  # nothing confirmed — retry on a later tick
                filled_qty, avg_price, _oid = routed
                if filled_qty < 1:
                    return None
                half = int(filled_qty)
                if avg_price is not None and avg_price > 0:
                    fill_price = float(avg_price)
        finally:
            mp.scaling = False
        closed_signed = half * sign
        last = fill_price
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
        # Remainder runs with AT LEAST a breakeven stop (tighten-only —
        # never loosen a stop the breakeven rule already lifted above
        # entry); the trailing logic then ratchets it up from there.
        mp.quantity -= closed_signed
        mp.scaled_out = True
        if mp.stop_loss is None:
            mp.stop_loss = mp.entry
        elif sign > 0:
            mp.stop_loss = max(mp.stop_loss, mp.entry)
        else:
            mp.stop_loss = min(mp.stop_loss, mp.entry)
        async with self._lock:
            booked = self._book.get(symbol)
            if booked is not None:
                booked.quantity = mp.quantity
                booked.scaled_out = True
                booked.stop_loss = mp.stop_loss
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


def _row_exit_fallback(
    session_factory: Callable[[], Any], symbol: str
) -> Optional[float]:
    """The last REAL price to settle a position at when no live quote is
    available (feed cold). Prefers the persisted `last_price` (mark-to-
    market skips synthetic ticks, so it holds the last real mark) and
    falls back to `average_price` (the real entry). None when the row is
    gone."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        pos = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if pos is None:
            return None
        if pos.last_price is not None and float(pos.last_price) > 0:
            return float(pos.last_price)
        if pos.average_price is not None and float(pos.average_price) > 0:
            return float(pos.average_price)
        return None


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
    broker_order_id: Optional[str] = None,
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
                broker_order_id=broker_order_id,
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


def _persist_position_quantity(
    session_factory: Callable[[], Any], symbol: str, quantity: int
) -> None:
    """Sync the position row's quantity to the broker's (reconciliation
    resize after an external partial close)."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        pos = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if pos is None:
            return
        pos.quantity = int(quantity)
        session.commit()


def _persist_exit_failure(
    session_factory: Callable[[], Any],
    mp: ManagedPosition,
    reason: str,
    message: str,
) -> None:
    """CRITICAL audit trail for an exit the broker kept refusing — the
    position is still open and the operator must know."""
    from app.db.models import AuditLog, RiskEvent

    with session_factory() as session:
        session.add(
            RiskEvent(
                event_type="EXIT_FAILED",
                severity="critical",
                message=message,
                context={
                    "symbol": mp.symbol,
                    "quantity": mp.quantity,
                    "reason": reason,
                    "failures": mp.exit_failures,
                    "signal_id": mp.signal_id,
                    "broker_account_id": mp.broker_account_id,
                },
                halted=False,
            )
        )
        session.add(
            AuditLog(
                actor="system",
                action="trade.exit_failed",
                target=f"symbol:{mp.symbol}",
                after={
                    "reason": reason,
                    "failures": mp.exit_failures,
                    "quantity": mp.quantity,
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
