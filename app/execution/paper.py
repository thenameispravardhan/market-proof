"""In-process paper-trading simulator.

The PaperBackend implements the TradingBackend protocol and lives
entirely in-process — no network calls. It uses a MarketDataBus to
trigger pending SL / LIMIT orders.

Semantics:

  MARKET orders
    - Fill at the most recent tick (last_price) for the symbol.
    - If no quote is available, the order stays PENDING for
      `pending_timeout_s` (default 5s) then EXPIRES.
  LIMIT orders
    - BUY LIMIT: fills when last_price <= limit_price.
    - SELL LIMIT: fills when last_price >= limit_price.
    - If the trigger never crosses, the order stays PENDING for
      `pending_timeout_s` then EXPIRES.
  STOP_LOSS / SL-M orders
    - BUY STOP: triggers when last_price >= stop_price.
    - SELL STOP: triggers when last_price <= stop_price.
    - Once triggered, the order converts to a MARKET fill at the
      triggering tick's price (true SL-M semantics) or to a LIMIT
      at stop_price (true STOP_LOSS).
    - If the trigger never crosses, the order stays PENDING for
      `pending_timeout_s` then EXPIRES.

Pending orders are stored in an in-memory dict; the backend's
`_pending_loop` task waits on per-symbol MarketDataBus queues and
checks each pending order on every tick. When an order fills, the
backend updates the in-memory `Position` (per symbol, with a
`mark_to_market` call) and persists a `trades` row with status
`filled`.

Persistence: every place_order writes a `trades` row with status
`placed`; on fill the same row is UPDATEd to `filled` with
executed_at + average_price + pnl. The `positions` table is
mirrored to the in-memory position dict so that PnL updates from
market data show up in `get_positions()` immediately.

The `mode='paper'` column on the trades table is implicit: a
`backend` field on Trade records the source. The T1 `trades` table
doesn't have a `mode` column; the paper backend is identified by
`trades.broker_order_id` being prefixed with `PAPER-`.
"""
from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Position as PositionRow
from app.db.models import Trade as TradeRow
from app.execution.base import (
    OrderResult,
    OrderSide,
    OrderState,
    OrderStatus,
    OrderType,
    Position,
    safe_float,
)
from app.execution.market_data import MarketDataBus, Quote
from app.logging_config import get_logger

log = get_logger(__name__)


def _paper_id() -> str:
    return f"PAPER-{uuid.uuid4().hex[:12]}"


@dataclass
class _PendingOrder:
    """Internal book-keeping for an order that hasn't filled yet."""
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    submitted_at: float = field(default_factory=time.monotonic)
    timeout_s: float = 30.0
    signal_id: Optional[int] = None
    strategy_id: Optional[int] = None
    broker_account_id: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: float) -> bool:
        return (now - self.submitted_at) >= self.timeout_s

    def triggered(self, last_price: float) -> bool:
        """True if the order's trigger has been hit at `last_price`."""
        if self.order_type == OrderType.MARKET:
            return True
        if self.order_type == OrderType.LIMIT:
            if self.limit_price is None:
                return False
            if self.side == OrderSide.BUY:
                return last_price <= self.limit_price
            return last_price >= self.limit_price
        # STOP_LOSS / SL-M
        if self.stop_price is None:
            return False
        if self.side == OrderSide.BUY:
            return last_price >= self.stop_price
        return last_price <= self.stop_price


# -- session factory type --------------------------------------------------


SessionFactory = Callable[[], Session]


def _default_session_factory() -> SessionFactory:
    from app.db.session import SessionLocal
    return SessionLocal


# -- The backend -----------------------------------------------------------


class PaperBackend:
    """In-process paper-trading simulator.

    Construction is cheap. Call `start()` to begin the pending-order
    loop; call `stop()` to cancel it. `start()` is idempotent.
    """

    name: str = "paper"
    broker_account_id: Optional[int] = None  # paper is account-agnostic

    def __init__(
        self,
        *,
        market_data: Optional[MarketDataBus] = None,
        session_factory: Optional[SessionFactory] = None,
        pending_timeout_s: float = 30.0,
        fill_on_market_immediate: bool = True,
    ) -> None:
        self._md = market_data or MarketDataBus()
        self._session_factory = session_factory or _default_session_factory()
        self._pending: dict[str, _PendingOrder] = {}
        self._positions: dict[str, Position] = {}
        # id -> asyncio.Queue so the caller can wait for a specific
        # order to fill. We hand this back in OrderResult.raw.
        self._fill_events: dict[str, asyncio.Event] = {}
        self._subs: dict[str, tuple[asyncio.Queue[Quote], asyncio.Task]] = {}
        self._stop_event: asyncio.Event = asyncio.Event()
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._pending_timeout_s = float(pending_timeout_s)
        # When True (the default), MARKET orders with a known quote
        # fill at submit time. When False, the order waits for the
        # next tick — useful for tests that want to assert PENDING
        # state explicitly.
        self._fill_on_market_immediate = bool(fill_on_market_immediate)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> asyncio.Task[None]:
        if self._loop_task is not None and not self._loop_task.done():
            return self._loop_task
        self._stop_event.clear()
        self._loop_task = asyncio.create_task(self._run(), name="paper-pending")
        return self._loop_task

    async def stop(self) -> None:
        self._stop_event.set()
        # Drop all symbol subscriptions.
        for sym, (q, t) in list(self._subs.items()):
            self._md.unsubscribe(sym, q)
            t.cancel()
        self._subs.clear()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
            self._loop_task = None

    async def _run(self) -> None:
        # Subscribe to every symbol that has a pending order. New
        # symbols added later are picked up on the next iteration
        # of the outer while loop (we re-scan pending orders).
        while not self._stop_event.is_set():
            try:
                # Check expiry on all pending orders.
                now = time.monotonic()
                expired = [
                    oid for oid, o in self._pending.items() if o.is_expired(now)
                ]
                for oid in expired:
                    await self._expire(oid)

                # Make sure we're subscribed to every symbol with a
                # pending order.
                wanted = {o.symbol for o in self._pending.values()}
                for sym in wanted - set(self._subs.keys()):
                    q = self._md.subscribe(sym)
                    task = asyncio.create_task(self._consume(sym, q))
                    self._subs[sym] = (q, task)
                # Drop subscriptions for symbols with no pending orders.
                for sym in list(self._subs.keys()):
                    if sym not in wanted:
                        q, t = self._subs.pop(sym)
                        self._md.unsubscribe(sym, q)
                        t.cancel()

                # Wait a short window then re-check.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001
                log.exception("paper.pending_loop_error")
                # Don't spin on a broken loop.
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def _consume(self, symbol: str, queue: asyncio.Queue[Quote]) -> None:
        """Drain ticks for one symbol; trigger matching pending orders."""
        try:
            while True:
                quote = await queue.get()
                await self._on_tick(symbol, quote)
        except asyncio.CancelledError:
            return

    async def _on_tick(self, symbol: str, quote: Quote) -> None:
        last = float(quote.last_price)
        # Walk a copy so we can mutate _pending while iterating.
        for oid in list(self._pending.keys()):
            o = self._pending[oid]
            if o.symbol != symbol:
                continue
            if not o.triggered(last):
                continue
            # Fill it. SL-M / STOP_LOSS fill at market once triggered;
            # LIMIT fills at limit_price; MARKET fills at last.
            if o.order_type == OrderType.LIMIT and o.limit_price is not None:
                fill_price = float(o.limit_price)
            elif o.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET):
                fill_price = last
            else:
                fill_price = last
            await self._fill(oid, fill_price)

    # -- TradingBackend protocol -----------------------------------------

    async def place_order(
        self,
        *,
        signal: Any,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> OrderResult:
        if int(quantity) <= 0:
            return OrderResult(
                broker_order_id="",
                state=OrderState.REJECTED,
                symbol=symbol,
                side=side,
                quantity=int(quantity),
                order_type=order_type,
                error="quantity must be > 0",
            )
        oid = _paper_id()
        signal_id = getattr(signal, "id", None) if signal is not None else None
        strategy_id = getattr(signal, "strategy_id", None) if signal is not None else None

        # Persist a placed trade row up front.
        await asyncio.get_running_loop().run_in_executor(
            None, _persist_trade_placed,
            self._session_factory,
            signal_id,
            self.broker_account_id,
            symbol,
            side.value,
            int(quantity),
            float(limit_price or stop_price or 0.0),
            order_type.value,
            oid,
        )

        # MARKET orders with a known quote fill immediately; otherwise PENDING.
        if order_type == OrderType.MARKET and self._fill_on_market_immediate:
            quote = await self._md.get_quote(symbol)
            if quote is not None:
                fill_price = float(quote.last_price)
                await self._fill(oid, fill_price, quantity=int(quantity),
                                 side=side, symbol=symbol,
                                 signal_id=signal_id, strategy_id=strategy_id,
                                 order_type=order_type)
                return OrderResult(
                    broker_order_id=oid,
                    state=OrderState.FILLED,
                    symbol=symbol,
                    side=side,
                    quantity=int(quantity),
                    order_type=order_type,
                    filled_quantity=int(quantity),
                    average_price=float(fill_price),
                    limit_price=limit_price,
                    stop_price=stop_price,
                    raw={"mode": "paper", "fill_source": "market_immediate"},
                )

        # Otherwise PENDING.
        pending = _PendingOrder(
            broker_order_id=oid,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            timeout_s=self._pending_timeout_s,
            signal_id=signal_id,
            strategy_id=strategy_id,
            broker_account_id=self.broker_account_id,
            extra={"signal_action": getattr(signal, "action", None) if signal is not None else None},
        )
        self._pending[oid] = pending
        self._fill_events[oid] = asyncio.Event()
        # Subscribe immediately so we don't miss the first tick.
        # (The main loop's "re-check" pass would pick this up, but
        # tests publish ticks right after place_order — a race.)
        if self._loop_task is not None and symbol not in self._subs:
            q = self._md.subscribe(symbol)
            task = asyncio.create_task(self._consume(symbol, q))
            self._subs[symbol] = (q, task)
        log.info(
            "paper.order_placed",
            broker_order_id=oid,
            symbol=symbol,
            side=side.value,
            quantity=quantity,
            order_type=order_type.value,
            limit_price=limit_price,
            stop_price=stop_price,
        )
        return OrderResult(
            broker_order_id=oid,
            state=OrderState.PENDING,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            raw={"mode": "paper"},
        )

    async def cancel_order(self, broker_order_id: str) -> bool:
        o = self._pending.pop(broker_order_id, None)
        ev = self._fill_events.pop(broker_order_id, None)
        if o is None:
            return False
        # Mark the trade row cancelled in DB.
        await asyncio.get_running_loop().run_in_executor(
            None, _update_trade_status,
            self._session_factory, broker_order_id, "cancelled",
        )
        log.info("paper.order_cancelled", broker_order_id=broker_order_id)
        if ev is not None:
            ev.set()
        return True

    async def get_positions(self) -> list[Position]:
        # Mark-to-market with the latest quote for each symbol.
        for pos in self._positions.values():
            quote = await self._md.get_quote(pos.symbol)
            if quote is not None:
                pos.mark_to_market(quote.last_price)
        return list(self._positions.values())

    async def get_order_status(self, broker_order_id: str) -> OrderStatus:
        o = self._pending.get(broker_order_id)
        if o is None:
            # Check the DB for terminal state.
            with self._session_factory() as s:
                row = s.query(TradeRow).filter_by(broker_order_id=broker_order_id).one_or_none()
            if row is None:
                return OrderStatus(
                    broker_order_id=broker_order_id,
                    state=OrderState.REJECTED,
                    error="unknown order",
                )
            state_map = {
                "placed": OrderState.PENDING,
                "filled": OrderState.FILLED,
                "cancelled": OrderState.CANCELLED,
                "rejected": OrderState.REJECTED,
                "expired": OrderState.EXPIRED,
            }
            return OrderStatus(
                broker_order_id=broker_order_id,
                state=state_map.get(row.status, OrderState.PENDING),
                filled_quantity=row.quantity if row.status == "filled" else 0,
                average_price=row.price,
            )
        return OrderStatus(
            broker_order_id=o.broker_order_id,
            state=OrderState.PENDING,
            filled_quantity=0,
        )

    # -- internals -------------------------------------------------------

    async def _fill(
        self,
        broker_order_id: str,
        fill_price: float,
        *,
        quantity: Optional[int] = None,
        side: Optional[OrderSide] = None,
        symbol: Optional[str] = None,
        signal_id: Optional[int] = None,
        strategy_id: Optional[int] = None,
        order_type: Optional[OrderType] = None,
    ) -> None:
        o = self._pending.pop(broker_order_id, None)
        ev = self._fill_events.pop(broker_order_id, None)
        if o is None and quantity is None:
            # Nothing to do.
            return
        sym = symbol or (o.symbol if o else "")
        sd = side or (o.side if o else OrderSide.BUY)
        qty = int(quantity if quantity is not None else (o.quantity if o else 0))
        sid = signal_id if signal_id is not None else (o.signal_id if o else None)
        stid = strategy_id if strategy_id is not None else (o.strategy_id if o else None)
        if qty <= 0:
            return
        # Update position.
        pos = self._positions.get(sym)
        if sd == OrderSide.BUY:
            if pos is None or pos.quantity == 0:
                self._positions[sym] = Position(
                    symbol=sym,
                    quantity=qty,
                    average_price=float(fill_price),
                    strategy_id=stid,
                    broker_account_id=self.broker_account_id,
                )
            else:
                new_qty = pos.quantity + qty
                new_avg = (pos.average_price * pos.quantity + fill_price * qty) / new_qty
                pos.quantity = new_qty
                pos.average_price = new_avg
                pos.updated_at = datetime.now(timezone.utc)
        else:  # SELL — close / reduce
            if pos is None or pos.quantity == 0:
                # Sell with no position: open a short. v1 keeps it
                # simple — record a short with negative qty.
                self._positions[sym] = Position(
                    symbol=sym,
                    quantity=-qty,
                    average_price=float(fill_price),
                    strategy_id=stid,
                    broker_account_id=self.broker_account_id,
                )
            else:
                pos.quantity = pos.quantity - qty
                pos.updated_at = datetime.now(timezone.utc)
        # Re-fetch the (possibly newly created) position so the
        # mirror step below has a valid reference.
        pos = self._positions.get(sym)
        if pos is None:
            # Defensive — should never happen.
            return
        # Compute realised PnL on the closing side.
        realised_pnl: Optional[float] = None
        if pos is not None and sd == OrderSide.SELL and pos.quantity <= 0:
            # Simplified: realised = (fill - avg) * qty when closing a long.
            if pos.average_price is not None and pos.quantity + qty > 0:
                realised_pnl = (fill_price - pos.average_price) * qty
        # Persist the fill on the trade row.
        await asyncio.get_running_loop().run_in_executor(
            None, _update_trade_filled,
            self._session_factory, broker_order_id, float(fill_price),
            realised_pnl,
        )
        # Mirror position to DB.
        await asyncio.get_running_loop().run_in_executor(
            None, _mirror_position,
            self._session_factory, sym, pos.quantity, pos.average_price,
            pos.last_price, pos.unrealized_pnl, stid,
        )
        log.info(
            "paper.order_filled",
            broker_order_id=broker_order_id,
            symbol=sym,
            side=sd.value,
            quantity=qty,
            fill_price=fill_price,
        )
        if ev is not None:
            ev.set()

    async def _expire(self, broker_order_id: str) -> None:
        o = self._pending.pop(broker_order_id, None)
        ev = self._fill_events.pop(broker_order_id, None)
        if o is None:
            return
        await asyncio.get_running_loop().run_in_executor(
            None, _update_trade_status,
            self._session_factory, broker_order_id, "expired",
        )
        log.info("paper.order_expired", broker_order_id=broker_order_id, symbol=o.symbol)
        if ev is not None:
            ev.set()

    # -- public test helpers --------------------------------------------

    def get_pending(self) -> dict[str, _PendingOrder]:
        """Return a copy of the in-memory pending-order book. Used
        by tests to assert state without round-tripping through
        the DB."""
        return dict(self._pending)


# -- DB helpers (sync, run in executor) ----------------------------------


def _persist_trade_placed(
    session_factory: SessionFactory,
    signal_id: Optional[int],
    broker_account_id: Optional[int],
    symbol: str,
    side: str,
    quantity: int,
    price: float,
    order_type: str,
    broker_order_id: str,
) -> None:
    with session_factory() as session:
        row = TradeRow(
            signal_id=signal_id,
            broker_account_id=broker_account_id,
            symbol=symbol,
            side=side,
            quantity=int(quantity),
            price=float(price),
            order_type=order_type,
            status="placed",
            broker_order_id=broker_order_id,
        )
        session.add(row)
        session.commit()


def _update_trade_filled(
    session_factory: SessionFactory,
    broker_order_id: str,
    fill_price: float,
    realised_pnl: Optional[float],
) -> None:
    with session_factory() as session:
        row = session.query(TradeRow).filter_by(broker_order_id=broker_order_id).one_or_none()
        if row is None:
            return
        row.status = "filled"
        row.price = float(fill_price)
        row.pnl = realised_pnl
        row.executed_at = datetime.now(timezone.utc)
        session.commit()


def _update_trade_status(
    session_factory: SessionFactory,
    broker_order_id: str,
    status: str,
) -> None:
    with session_factory() as session:
        row = session.query(TradeRow).filter_by(broker_order_id=broker_order_id).one_or_none()
        if row is None:
            return
        row.status = status
        session.commit()


def _mirror_position(
    session_factory: SessionFactory,
    symbol: str,
    quantity: int,
    average_price: float,
    last_price: Optional[float],
    unrealized_pnl: Optional[float],
    strategy_id: Optional[int],
) -> None:
    """Update the positions table to mirror the in-memory state."""
    with session_factory() as session:
        row = session.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if row is None:
            row = PositionRow(
                symbol=symbol,
                quantity=int(quantity),
                average_price=float(average_price),
                last_price=last_price,
                unrealized_pnl=unrealized_pnl,
                strategy_id=strategy_id,
            )
            session.add(row)
        else:
            row.quantity = int(quantity)
            row.average_price = float(average_price)
            row.last_price = last_price
            row.unrealized_pnl = unrealized_pnl
            row.strategy_id = strategy_id
        session.commit()
