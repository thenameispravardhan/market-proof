"""Entry state machine — owns the full lifecycle of ONE entry attempt.

The execution Manager used to place entries with a linear script: build
an IOC marketable-limit, call the backend, persist whatever came back.
That script had no answer for the messy middle — a price that ran away
between signal and order (chasing), two signals for the same symbol
racing each other (double entry), a fill confirmation that arrived on
the order WebSocket but never on REST (or vice versa), a partial fill,
or a market that crashed through the stop inside the fill window.

`EntryManager.execute()` replaces that script with an explicit state
machine. Each approved signal runs as its own asyncio task and moves
through:

    INITIALIZED       signal approved by the risk engine; attempt created
    AWAITING_QUOTE    fetch the freshest bid/ask from the market-data bus
    DRIFT_CHECK       live vs signal price; adverse move beyond
                      ENTRY_MAX_DRIFT_PCT -> RETRACEMENT_WATCH (no order)
    ORDER_ROUTING     IOC marketable-limit to the backend, tagged with a
                      client_order_id (symbol mutex held from here on)
    FILL_MONITORING   wait for the order-WS fill event (fast path)
    RECONCILING       WS silent -> poll REST /orders (slow path)
    PARTIAL_HANDLING  filled < intended -> keep the fill, cancel the rest
    ACTIVE_POSITION   hand over to the TradeManager with the EXACT qty
    EXPIRED           zero fill inside the window -> cancel, NO retry
    REJECTED          broker refused (margin / circuit / suspended), or
                      the symbol mutex stayed contended

Safety features baked into the machine (RISK.md §5):

  * Symbol-level mutex — a second signal for the same symbol while the
    first is routing waits SYMBOL_LOCK_WAIT_SECONDS, then rejects with
    SYMBOL_LOCKED. Prevents double entry when NSE and BSE publish the
    same news. RETRACEMENT_WATCH is passive and holds NO lock, so a
    genuinely new signal for the symbol can still trade (E-13).
  * Dynamic risk on partial fills — the position is handed over with
    the FILLED quantity and the ORIGINAL stop. Rupee risk scales down
    with the quantity automatically; no stop recalculation needed.
  * Dual confirmation — the fill is confirmed by the order WebSocket
    (`trades.filled` on the event bus, published by the shared
    reconciler) OR a REST /orders poll at the ORDER_FILL_TIMEOUT mark,
    whichever lands first. Duplicates are deduped downstream by
    broker_order_id (see `order_reconcile.reconcile_order_update`).
  * Live stop-trigger check on fill — if the market is already through
    the stop when the fill confirms, exit immediately at market
    (FLASH_CRASH_EXIT) instead of handing a dead position to the
    TradeManager.
  * Slippage breach — a fill through the limit price (exchange glitch)
    is flattened immediately at market (SLIPPAGE_BREACH) and flagged
    critical.
  * Broker timeout — if place_order itself doesn't answer within
    BROKER_ORDER_TIMEOUT_SECONDS the attempt ends as BROKER_TIMEOUT and
    is NEVER retried: a blind retry is how double orders happen.

The machine is deliberately DB-free: it returns an `EntryOutcome` and
the Manager owns persistence + event publishing, mirroring how the
TradeManager / QuoteFeed split their responsibilities. The one seam is
`on_routed`, an async callback fired as soon as the broker acks the
order id so the caller can persist the PENDING trade row — without it
the WS reconciler can't match the fill event to a row.

`RetracementMonitor` (same module — it's the other half of the drift
gate) passively watches drift-blocked signals: if the price pulls back
inside the band within RETRACEMENT_WINDOW_SECONDS it re-arms the signal
via `reenter_cb`; when the window closes it expires it via `expire_cb`.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Optional

from app.config import Settings, get_settings
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderType,
    ProductType,
)
from app.execution.market_data import MarketDataBus, Quote
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)


# The shared reconciler (order WebSocket + postback webhook both funnel
# through it) publishes this channel on a CONFIRMED FULL FILL with
# {"broker_order_id": ..., "price": ...}. FILL_MONITORING's fast path
# listens here.
CHANNEL_TRADES_FILLED = "trades.filled"


# ---- States & outcome codes ----------------------------------------------


class EntryState(str, Enum):
    INITIALIZED = "INITIALIZED"
    AWAITING_QUOTE = "AWAITING_QUOTE"
    DRIFT_CHECK = "DRIFT_CHECK"
    RETRACEMENT_WATCH = "RETRACEMENT_WATCH"
    ORDER_ROUTING = "ORDER_ROUTING"
    FILL_MONITORING = "FILL_MONITORING"
    RECONCILING = "RECONCILING"
    PARTIAL_HANDLING = "PARTIAL_HANDLING"
    ACTIVE_POSITION = "ACTIVE_POSITION"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


# Terminal outcome codes the Manager branches on.
OUTCOME_FULL_FILL = "ENTRY_FULL_FILL"
OUTCOME_PARTIAL_FILL = "ENTRY_PARTIAL_FILL"
OUTCOME_EXCESSIVE_DRIFT = "EXCESSIVE_DRIFT"
OUTCOME_SYMBOL_LOCKED = "SYMBOL_LOCKED"
OUTCOME_EXPIRED = "ENTRY_EXPIRED"
OUTCOME_REJECTED = "ENTRY_REJECTED"
OUTCOME_BROKER_TIMEOUT = "BROKER_TIMEOUT"
OUTCOME_SLIPPAGE_BREACH = "SLIPPAGE_BREACH"
OUTCOME_FLASH_CRASH_EXIT = "FLASH_CRASH_EXIT"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def adverse_drift_pct(side: OrderSide | str, anchor: float, live: float) -> float:
    """Signed % the live price has moved AGAINST the entry since the
    signal was priced. Positive = worse (chasing); negative = the stock
    pulled back and we'd enter at a better price than planned (E-02).
    """
    if anchor is None or anchor <= 0 or live is None or live <= 0:
        return 0.0
    s = side.value if isinstance(side, OrderSide) else str(side).upper()
    if s == "BUY":
        return (float(live) - float(anchor)) / float(anchor) * 100.0
    return (float(anchor) - float(live)) / float(anchor) * 100.0


def price_for_side(quote: Optional[Quote], side: OrderSide) -> Optional[float]:
    """The price we'd actually transact at: ask for a BUY, bid for a
    SELL, falling back to last trade when the book side is unknown."""
    if quote is None:
        return None
    px = quote.ask if side == OrderSide.BUY else quote.bid
    if px is None or px <= 0:
        px = quote.last_price
    return float(px) if px and px > 0 else None


# ---- Attempt / outcome value types ----------------------------------------


@dataclass
class EntryAttempt:
    """Mutable record of one pass through the state machine. Carries a
    `client_order_id` so log lines / dedup across WS + REST confirmations
    can always be tied back to this exact attempt."""

    symbol: str
    side: OrderSide
    quantity: int
    signal_entry: float                    # drift anchor (analysis-time price)
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    signal_id: Optional[int] = None
    client_order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    state: EntryState = EntryState.INITIALIZED
    history: list[str] = field(default_factory=lambda: [EntryState.INITIALIZED.value])
    live_price: Optional[float] = None     # freshest transactable price
    drift_pct: Optional[float] = None
    limit_price: Optional[float] = None
    broker_order_id: Optional[str] = None
    filled_quantity: int = 0
    fill_price: Optional[float] = None
    fill_source: Optional[str] = None      # "sync" | "ws" | "rest"
    started_at: datetime = field(default_factory=utcnow)

    def transition(self, state: EntryState) -> None:
        self.state = state
        self.history.append(state.value)
        log.info(
            "entry.state",
            symbol=self.symbol,
            state=state.value,
            client_order_id=self.client_order_id,
            signal_id=self.signal_id,
            broker_order_id=self.broker_order_id,
        )


@dataclass
class EntryOutcome:
    """What `execute()` hands back to the Manager. `result` is the final
    authoritative OrderResult (fill qty / price corrected from whichever
    confirmation path landed); `exit_result` is only set for the two
    emergency flatten paths (SLIPPAGE_BREACH / FLASH_CRASH_EXIT)."""

    code: str
    attempt: EntryAttempt
    result: Optional[OrderResult] = None
    exit_result: Optional[OrderResult] = None
    message: str = ""

    @property
    def filled(self) -> bool:
        """True when a position (full or partial) came out of the attempt
        and is still open (i.e. NOT emergency-flattened)."""
        return self.code in (OUTCOME_FULL_FILL, OUTCOME_PARTIAL_FILL)


# ---- The entry manager -----------------------------------------------------


class EntryManager:
    """Runs entry attempts. One instance per process; per-symbol asyncio
    locks serialise attempts on the same symbol."""

    def __init__(
        self,
        *,
        market_data: MarketDataBus,
        settings_provider: Optional[Callable[[], Settings]] = None,
    ) -> None:
        self._md = market_data
        self._settings = settings_provider or get_settings
        self._locks: dict[str, asyncio.Lock] = {}

    # -- introspection (tests / diagnostics) ------------------------------

    def lock_held(self, symbol: str) -> bool:
        lock = self._locks.get(symbol.upper().strip())
        return lock is not None and lock.locked()

    def _lock_for(self, symbol: str) -> asyncio.Lock:
        key = symbol.upper().strip()
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # -- the state machine -------------------------------------------------

    async def execute(
        self,
        *,
        backend: Any,
        symbol: str,
        side: OrderSide,
        quantity: int,
        signal_entry: float,
        fallback_price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        target: Optional[float] = None,
        signal: Any = None,
        signal_id: Optional[int] = None,
        product_type: ProductType = ProductType.INTRADAY,
        on_routed: Optional[Callable[[OrderResult], Awaitable[None]]] = None,
    ) -> EntryOutcome:
        """Run one entry attempt end-to-end and return its outcome.

        `signal_entry` is the drift anchor — the price the signal was
        born at (the analysis levels), NOT the freshly seeded quote.
        `fallback_price` is the routing basis when the bus has no quote
        (offline tests). `on_routed` fires right after the broker acks
        the order id, so the caller can persist the PENDING trade row
        before fill monitoring starts (the WS reconciler needs the row
        to exist to match the fill event).
        """
        attempt = EntryAttempt(
            symbol=symbol.upper().strip(),
            side=side,
            quantity=int(quantity),
            signal_entry=float(signal_entry),
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            target=float(target) if target is not None else None,
            signal_id=signal_id,
        )
        settings = self._settings()
        lock_wait = float(getattr(settings, "SYMBOL_LOCK_WAIT_SECONDS", 0.5))

        # Symbol-level mutex: prevents double entry on overlapping signals
        # (NSE + BSE publishing the same news). We wait a short beat in
        # case the first attempt resolves fast, then reject.
        lock = self._lock_for(attempt.symbol)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=lock_wait)
        except asyncio.TimeoutError:
            attempt.transition(EntryState.REJECTED)
            log.warning(
                "entry.symbol_locked",
                symbol=attempt.symbol, signal_id=signal_id,
                waited_s=lock_wait,
            )
            return EntryOutcome(
                code=OUTCOME_SYMBOL_LOCKED,
                attempt=attempt,
                message=(
                    f"another entry for {attempt.symbol} is in flight; "
                    f"waited {lock_wait:.1f}s for the symbol lock"
                ),
            )
        try:
            return await self._run_locked(
                backend=backend,
                attempt=attempt,
                fallback_price=fallback_price,
                signal=signal,
                product_type=product_type,
                on_routed=on_routed,
                settings=settings,
            )
        finally:
            lock.release()

    async def _run_locked(
        self,
        *,
        backend: Any,
        attempt: EntryAttempt,
        fallback_price: Optional[float],
        signal: Any,
        product_type: ProductType,
        on_routed: Optional[Callable[[OrderResult], Awaitable[None]]],
        settings: Settings,
    ) -> EntryOutcome:
        side = attempt.side

        # -- AWAITING_QUOTE: the absolute latest transactable price -------
        attempt.transition(EntryState.AWAITING_QUOTE)
        quote = await self._md.get_quote(attempt.symbol)
        live = price_for_side(quote, side)
        if live is None or live <= 0:
            live = float(fallback_price) if fallback_price and fallback_price > 0 else attempt.signal_entry
        attempt.live_price = float(live)

        # -- DRIFT_CHECK: anti-chase gate ----------------------------------
        # Against a SIMULATED quote (offline paper mode) the analysis
        # price and the hashed sim price live in different universes, so
        # drift is meaningless — anchor to the live price (drift 0).
        attempt.transition(EntryState.DRIFT_CHECK)
        simulated = bool(quote is not None and (quote.extra or {}).get("simulated"))
        anchor = attempt.signal_entry if (attempt.signal_entry > 0 and not simulated) else live
        max_drift = float(getattr(settings, "ENTRY_MAX_DRIFT_PCT", 1.5))
        drift = adverse_drift_pct(side, anchor, live)
        attempt.drift_pct = drift
        if drift > max_drift:
            attempt.transition(EntryState.RETRACEMENT_WATCH)
            log.warning(
                "entry.excessive_drift",
                symbol=attempt.symbol, signal_id=attempt.signal_id,
                anchor=round(anchor, 2), live=round(live, 2),
                drift_pct=round(drift, 3), max_drift_pct=max_drift,
            )
            return EntryOutcome(
                code=OUTCOME_EXCESSIVE_DRIFT,
                attempt=attempt,
                message=(
                    f"live {live:.2f} drifted {drift:.2f}% from signal "
                    f"{anchor:.2f} (max {max_drift:.2f}%)"
                ),
            )

        # -- ORDER_ROUTING: IOC marketable-limit ---------------------------
        # Cap the fill within a small buffer of the LIVE price so a news
        # spike can't fill us far away; IOC so we never rest in a fast
        # market.
        attempt.transition(EntryState.ORDER_ROUTING)
        buffer = float(getattr(settings, "ENTRY_BUFFER_PCT", 0.2)) / 100.0
        limit_price = live * (1.0 + buffer) if side == OrderSide.BUY else live * (1.0 - buffer)
        attempt.limit_price = limit_price
        broker_timeout = float(getattr(settings, "BROKER_ORDER_TIMEOUT_SECONDS", 3.0))

        # Subscribe to the fill channel BEFORE routing so a lightning-fast
        # WS confirmation can't slip between place_order and monitoring.
        fill_queue = event_bus.subscribe(CHANNEL_TRADES_FILLED)
        try:
            try:
                result: OrderResult = await asyncio.wait_for(
                    backend.place_order(
                        signal=signal,
                        symbol=attempt.symbol,
                        side=side,
                        quantity=attempt.quantity,
                        order_type=OrderType.LIMIT,
                        limit_price=limit_price,
                        stop_price=None,
                        product_type=product_type,
                        validity="IOC",
                    ),
                    timeout=broker_timeout,
                )
            except asyncio.TimeoutError:
                # The order MAY have reached the broker. Retrying blind is
                # how double orders happen — never retry; the operator (or
                # a later reconcile of the broker order book) resolves it.
                attempt.transition(EntryState.REJECTED)
                log.error(
                    "entry.broker_timeout",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                    timeout_s=broker_timeout,
                )
                return EntryOutcome(
                    code=OUTCOME_BROKER_TIMEOUT,
                    attempt=attempt,
                    message=(
                        f"broker gave no response in {broker_timeout:.1f}s; "
                        "NOT retried (duplicate-order risk)"
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt.transition(EntryState.REJECTED)
                log.exception(
                    "entry.place_order_crashed",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                )
                return EntryOutcome(
                    code=OUTCOME_REJECTED, attempt=attempt, message=str(e)
                )

            attempt.broker_order_id = result.broker_order_id or None

            # Broker refused outright (margin, circuit, suspended, ...).
            if result.state == OrderState.REJECTED:
                attempt.transition(EntryState.REJECTED)
                log.warning(
                    "entry.broker_rejected",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                    error=result.error,
                )
                return EntryOutcome(
                    code=OUTCOME_REJECTED,
                    attempt=attempt,
                    result=result,
                    message=result.error or "broker rejected the order",
                )
            # IOC came back dead on arrival (paper's "not marketable").
            if result.state in (OrderState.EXPIRED, OrderState.CANCELLED):
                attempt.transition(EntryState.EXPIRED)
                return EntryOutcome(
                    code=OUTCOME_EXPIRED,
                    attempt=attempt,
                    result=result,
                    message=result.error or "IOC cancelled with zero fill",
                )
            # Synchronous fill (paper backend fills marketable IOC inline).
            if result.state == OrderState.FILLED:
                attempt.filled_quantity = int(result.filled_quantity or attempt.quantity)
                attempt.fill_price = (
                    float(result.average_price) if result.average_price is not None else None
                )
                attempt.fill_source = "sync"
                return await self._post_fill(
                    backend=backend, attempt=attempt, routed=result,
                    signal=signal, product_type=product_type, settings=settings,
                )

            # PENDING without an order id: we can neither watch the WS nor
            # poll REST. Same double-order calculus as the broker timeout.
            if not attempt.broker_order_id:
                attempt.transition(EntryState.REJECTED)
                return EntryOutcome(
                    code=OUTCOME_BROKER_TIMEOUT,
                    attempt=attempt,
                    result=result,
                    message=result.error or "broker returned no order id; NOT retried",
                )

            # Persist the PENDING row before monitoring so the WS
            # reconciler can match the fill event to it.
            if on_routed is not None:
                try:
                    await on_routed(result)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "entry.on_routed_failed",
                        symbol=attempt.symbol, signal_id=attempt.signal_id,
                    )

            # -- FILL_MONITORING / RECONCILING -----------------------------
            attempt.transition(EntryState.FILL_MONITORING)
            filled_qty, avg_price, source = await self._monitor_fill(
                backend=backend, attempt=attempt,
                fill_queue=fill_queue, settings=settings,
            )
            if filled_qty <= 0:
                # Zero fill inside the window: cancel and expire. Do NOT
                # retry — the market has moved past us (E-07).
                try:
                    await backend.cancel_order(attempt.broker_order_id)
                except Exception:  # noqa: BLE001
                    log.warning(
                        "entry.expire_cancel_failed",
                        symbol=attempt.symbol,
                        broker_order_id=attempt.broker_order_id,
                    )
                attempt.transition(EntryState.EXPIRED)
                log.info(
                    "entry.expired_zero_fill",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                    broker_order_id=attempt.broker_order_id,
                )
                return EntryOutcome(
                    code=OUTCOME_EXPIRED,
                    attempt=attempt,
                    result=replace(result, state=OrderState.EXPIRED),
                    message="zero fill inside the IOC window",
                )
            attempt.filled_quantity = int(filled_qty)
            attempt.fill_price = float(avg_price) if avg_price else None
            attempt.fill_source = source
            return await self._post_fill(
                backend=backend, attempt=attempt, routed=result,
                signal=signal, product_type=product_type, settings=settings,
            )
        finally:
            event_bus.unsubscribe(CHANNEL_TRADES_FILLED, fill_queue)

    # -- fill monitoring ----------------------------------------------------

    async def _monitor_fill(
        self,
        *,
        backend: Any,
        attempt: EntryAttempt,
        fill_queue: asyncio.Queue,
        settings: Settings,
    ) -> tuple[int, Optional[float], str]:
        """Dual-confirmation fill watch. Returns (filled_qty, avg_price,
        source). Fast path: the `trades.filled` event from the order WS
        (published by the shared reconciler — a full fill by definition).
        Slow path: REST /orders polls at the ORDER_FILL_TIMEOUT mark and
        again at the total ENTRY_FILL_TIMEOUT deadline."""
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        rest_poll_at = t0 + float(getattr(settings, "ORDER_FILL_TIMEOUT_SECONDS", 1.5))
        deadline = t0 + float(getattr(settings, "ENTRY_FILL_TIMEOUT_SECONDS", 2.0))
        rest_polled = False
        best_qty = 0
        best_price: Optional[float] = None

        while True:
            now = loop.time()
            if now >= deadline:
                break
            # Wake at whichever comes first: the REST-poll mark or the
            # total deadline; WS events interrupt the wait immediately.
            wake_at = deadline if rest_polled else min(rest_poll_at, deadline)
            try:
                evt = await asyncio.wait_for(
                    fill_queue.get(), timeout=max(0.005, wake_at - now)
                )
            except asyncio.TimeoutError:
                evt = None
            if evt is not None:
                payload = getattr(evt, "payload", None) or {}
                if str(payload.get("broker_order_id") or "") == attempt.broker_order_id:
                    price = payload.get("price")
                    log.info(
                        "entry.fill_confirmed_ws",
                        symbol=attempt.symbol,
                        broker_order_id=attempt.broker_order_id,
                        client_order_id=attempt.client_order_id,
                    )
                    return (
                        attempt.quantity,
                        float(price) if price else None,
                        "ws",
                    )
                continue  # someone else's fill — keep listening
            if not rest_polled and loop.time() >= rest_poll_at:
                rest_polled = True
                attempt.transition(EntryState.RECONCILING)
                qty, price, terminal = await self._rest_reconcile(backend, attempt)
                if qty > best_qty:
                    best_qty, best_price = qty, price
                if terminal:
                    return (best_qty, best_price, "rest")
                attempt.transition(EntryState.FILL_MONITORING)

        # Total window closed — one final REST reconcile settles it.
        attempt.transition(EntryState.RECONCILING)
        qty, price, _terminal = await self._rest_reconcile(backend, attempt)
        if qty > best_qty:
            best_qty, best_price = qty, price
        return (best_qty, best_price, "rest")

    async def _rest_reconcile(
        self, backend: Any, attempt: EntryAttempt
    ) -> tuple[int, Optional[float], bool]:
        """One REST /orders status check. Returns (filled_qty, avg_price,
        terminal) — terminal=True when the order can't change any more."""
        try:
            status = await backend.get_order_status(attempt.broker_order_id)
        except Exception:  # noqa: BLE001
            log.warning(
                "entry.rest_reconcile_failed",
                symbol=attempt.symbol,
                broker_order_id=attempt.broker_order_id,
            )
            return (0, None, False)
        qty = int(getattr(status, "filled_quantity", 0) or 0)
        price = getattr(status, "average_price", None)
        state = getattr(status, "state", OrderState.PENDING)
        if state == OrderState.FILLED and qty <= 0:
            # Broker says traded but omitted the quantity — trust the state.
            qty = attempt.quantity
        terminal = state in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )
        return (qty, float(price) if price else None, terminal)

    # -- post-fill safety checks ---------------------------------------------

    async def _post_fill(
        self,
        *,
        backend: Any,
        attempt: EntryAttempt,
        routed: OrderResult,
        signal: Any,
        product_type: ProductType,
        settings: Settings,
    ) -> EntryOutcome:
        """PARTIAL_HANDLING + the two emergency exits, then handover."""
        partial = 0 < attempt.filled_quantity < attempt.quantity
        if partial:
            attempt.transition(EntryState.PARTIAL_HANDLING)
            # Cancel the unfilled remainder (IOC should have done this at
            # the exchange; belt-and-braces for brokers that leave it).
            # The stop price stays put: risk scales down with quantity.
            try:
                await backend.cancel_order(attempt.broker_order_id or "")
            except Exception:  # noqa: BLE001
                log.warning(
                    "entry.partial_cancel_failed",
                    symbol=attempt.symbol,
                    broker_order_id=attempt.broker_order_id,
                )
            log.info(
                "entry.partial_fill",
                symbol=attempt.symbol, signal_id=attempt.signal_id,
                intended=attempt.quantity, filled=attempt.filled_quantity,
            )

        final = replace(
            routed,
            state=OrderState.FILLED,
            quantity=attempt.filled_quantity,
            filled_quantity=attempt.filled_quantity,
            average_price=(
                attempt.fill_price
                if attempt.fill_price is not None
                else routed.average_price
            ),
        )
        fill_price = final.average_price

        # 1. SLIPPAGE_BREACH — a fill through the limit price can only be
        #    an exchange/broker glitch. Nuclear option: flatten at market
        #    immediately and flag critical.
        if fill_price is not None and attempt.limit_price is not None:
            eps = abs(attempt.limit_price) * 1e-6
            breached = (
                fill_price > attempt.limit_price + eps
                if attempt.side == OrderSide.BUY
                else fill_price < attempt.limit_price - eps
            )
            if breached:
                log.error(
                    "entry.slippage_breach",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                    fill=round(float(fill_price), 4),
                    limit=round(float(attempt.limit_price), 4),
                )
                exit_result = await self._market_exit(
                    backend=backend, attempt=attempt,
                    signal=signal, product_type=product_type,
                )
                return EntryOutcome(
                    code=OUTCOME_SLIPPAGE_BREACH,
                    attempt=attempt,
                    result=final,
                    exit_result=exit_result,
                    message=(
                        f"filled at {fill_price:.2f} through the "
                        f"{attempt.limit_price:.2f} limit; position flattened"
                    ),
                )

        # 2. FLASH_CRASH_EXIT — the market crashed through the stop while
        #    we were filling. Exit now; don't hand a dead position over.
        if attempt.stop_loss is not None:
            quote = await self._md.get_quote(attempt.symbol)
            last = float(quote.last_price) if quote and quote.last_price else None
            crashed = last is not None and (
                last <= attempt.stop_loss
                if attempt.side == OrderSide.BUY
                else last >= attempt.stop_loss
            )
            if crashed:
                log.error(
                    "entry.flash_crash_exit",
                    symbol=attempt.symbol, signal_id=attempt.signal_id,
                    last=round(last, 4), stop_loss=round(attempt.stop_loss, 4),
                )
                exit_result = await self._market_exit(
                    backend=backend, attempt=attempt,
                    signal=signal, product_type=product_type,
                )
                return EntryOutcome(
                    code=OUTCOME_FLASH_CRASH_EXIT,
                    attempt=attempt,
                    result=final,
                    exit_result=exit_result,
                    message=(
                        f"price {last:.2f} already through the stop "
                        f"{attempt.stop_loss:.2f} at fill time; exited at market"
                    ),
                )

        attempt.transition(EntryState.ACTIVE_POSITION)
        code = OUTCOME_PARTIAL_FILL if partial else OUTCOME_FULL_FILL
        log.info(
            "entry.fill",
            code=code,
            symbol=attempt.symbol, signal_id=attempt.signal_id,
            quantity=attempt.filled_quantity,
            price=attempt.fill_price,
            source=attempt.fill_source,
            client_order_id=attempt.client_order_id,
        )
        return EntryOutcome(code=code, attempt=attempt, result=final)

    async def _market_exit(
        self,
        *,
        backend: Any,
        attempt: EntryAttempt,
        signal: Any,
        product_type: ProductType,
    ) -> Optional[OrderResult]:
        """Flatten the just-filled quantity at market (emergency paths)."""
        exit_side = OrderSide.SELL if attempt.side == OrderSide.BUY else OrderSide.BUY
        try:
            return await backend.place_order(
                signal=signal,
                symbol=attempt.symbol,
                side=exit_side,
                quantity=attempt.filled_quantity,
                order_type=OrderType.MARKET,
                limit_price=None,
                stop_price=None,
                product_type=product_type,
                validity="DAY",
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "entry.emergency_exit_failed",
                symbol=attempt.symbol, signal_id=attempt.signal_id,
                quantity=attempt.filled_quantity,
            )
            return None


# ---- Retracement monitor ----------------------------------------------------


@dataclass
class RetracementWatch:
    """One drift-blocked signal being watched for a pullback."""

    signal_id: int
    symbol: str
    side: str                       # "BUY" | "SELL"
    signal_entry: float             # the original drift anchor
    deadline: datetime
    created_at: datetime = field(default_factory=utcnow)


ReenterCb = Callable[[RetracementWatch], Awaitable[None]]
ExpireCb = Callable[[RetracementWatch], Awaitable[None]]


class RetracementMonitor:
    """Passively watches drift-blocked signals for a pullback into the
    entry band. Holds NO symbol lock (a fresh signal for the same symbol
    trades normally, E-13). Each watch is its own asyncio task; it fires
    `reenter_cb` at most once, or `expire_cb` when the window closes.

    Re-arming preserves the ORIGINAL deadline: if the re-attempt drift-
    blocks again, the Manager re-watches with the same deadline, so a
    signal can never ping-pong past its retracement window (E-05).
    """

    def __init__(
        self,
        *,
        market_data: MarketDataBus,
        reenter_cb: Optional[ReenterCb] = None,
        expire_cb: Optional[ExpireCb] = None,
        settings_provider: Optional[Callable[[], Settings]] = None,
    ) -> None:
        self._md = market_data
        self._reenter = reenter_cb
        self._expire = expire_cb
        self._settings = settings_provider or get_settings
        self._watches: dict[int, RetracementWatch] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    # -- watch management --------------------------------------------------

    def watch(
        self,
        *,
        signal_id: int,
        symbol: str,
        side: str,
        signal_entry: float,
        deadline: Optional[datetime] = None,
    ) -> bool:
        """Start watching a drift-blocked signal. Returns False when the
        signal is already being watched (dedupe). Pass the previous
        watch's `deadline` on a re-arm-then-drift-again cycle so the
        original window keeps counting down."""
        if signal_id in self._watches:
            return False
        settings = self._settings()
        window = float(getattr(settings, "RETRACEMENT_WINDOW_SECONDS", 120))
        w = RetracementWatch(
            signal_id=int(signal_id),
            symbol=symbol.upper().strip(),
            side=str(side).upper(),
            signal_entry=float(signal_entry),
            deadline=deadline or (utcnow() + timedelta(seconds=window)),
        )
        self._watches[w.signal_id] = w
        self._tasks[w.signal_id] = asyncio.create_task(
            self._watch_loop(w), name=f"retracement-{w.signal_id}"
        )
        log.info(
            "retracement.watch",
            signal_id=w.signal_id, symbol=w.symbol,
            signal_entry=w.signal_entry,
            deadline=w.deadline.isoformat(),
        )
        return True

    def watching(self, signal_id: Optional[int] = None) -> bool:
        if signal_id is None:
            return bool(self._watches)
        return int(signal_id) in self._watches

    def watch_count(self) -> int:
        return len(self._watches)

    def stop(self) -> None:
        """Cancel every watch task (process shutdown)."""
        for task in list(self._tasks.values()):
            task.cancel()
        self._tasks.clear()
        self._watches.clear()

    # -- the watch loop ------------------------------------------------------

    async def _watch_loop(self, w: RetracementWatch) -> None:
        settings = self._settings()
        poll = float(getattr(settings, "RETRACEMENT_POLL_SECONDS", 1.0))
        max_drift = float(getattr(settings, "ENTRY_MAX_DRIFT_PCT", 1.5))
        side = OrderSide.BUY if w.side == "BUY" else OrderSide.SELL
        try:
            while utcnow() < w.deadline:
                quote = await self._md.get_quote(w.symbol)
                live = price_for_side(quote, side)
                if live is not None and w.signal_entry > 0:
                    drift = adverse_drift_pct(side, w.signal_entry, live)
                    if drift <= max_drift:
                        self._watches.pop(w.signal_id, None)
                        log.info(
                            "retracement.rearmed",
                            signal_id=w.signal_id, symbol=w.symbol,
                            live=round(live, 2), drift_pct=round(drift, 3),
                        )
                        if self._reenter is not None:
                            await self._reenter(w)
                        return
                await asyncio.sleep(poll)
            # Window closed without a pullback: the move is real — let it go.
            self._watches.pop(w.signal_id, None)
            log.info(
                "retracement.expired",
                signal_id=w.signal_id, symbol=w.symbol,
            )
            if self._expire is not None:
                await self._expire(w)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception(
                "retracement.watch_crashed",
                signal_id=w.signal_id, symbol=w.symbol,
            )
        finally:
            # Guarded cleanup: the reenter callback may have re-registered
            # this signal (drifted again → new watch, ORIGINAL deadline)
            # while we were still unwinding — never evict the replacement.
            # current_task() raises when the coroutine is being finalised
            # by GC after the loop closed (test teardown) — treat as None.
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if self._watches.get(w.signal_id) is w:
                self._watches.pop(w.signal_id, None)
            if current is not None and self._tasks.get(w.signal_id) is current:
                self._tasks.pop(w.signal_id, None)
