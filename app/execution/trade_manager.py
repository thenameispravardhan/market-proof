"""Trade management — the post-entry half of the workflow.

The execution Manager opens positions. The TradeManager *closes* them:
it watches the quote feed for every open position and exits when the
price hits the stop-loss or the target, computes realised P&L, and
records everything. It also backs the manual "close" / "square-off
all" controls on the dashboard.

Flow:

  trade.executed (FILLED, BUY)
      -> load the signal's entry/SL/target levels
      -> register a ManagedPosition, ask the QuoteFeed to watch it
  every QUOTE_REFRESH_SECONDS:
      -> for each managed position, read the latest quote
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
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.execution.market_data import MarketDataBus
from app.execution.quote_feed import QuoteFeed
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

    def exit_reason(self, last: float) -> Optional[str]:
        """Return 'STOP' / 'TARGET' if `last` triggers an exit, else None."""
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
    ) -> None:
        symbol = symbol.upper().strip()
        mp = ManagedPosition(
            symbol=symbol,
            quantity=int(quantity),
            entry=float(entry),
            stop_loss=float(stop_loss) if stop_loss is not None else None,
            target=float(target) if target is not None else None,
            signal_id=signal_id,
            strategy_id=strategy_id,
            broker_account_id=broker_account_id,
        )
        async with self._lock:
            self._book[symbol] = mp
        if self._quote_feed is not None:
            self._quote_feed.watch(symbol, entry)
        log.info(
            "trade_manager.registered",
            symbol=symbol, quantity=quantity, entry=entry,
            stop_loss=stop_loss, target=target,
        )

    def managed_positions(self) -> list[ManagedPosition]:
        return list(self._book.values())

    # -- main loop -------------------------------------------------------

    async def _run(self) -> None:
        self._sub_queue = event_bus.subscribe(CHANNEL_TRADE_EXECUTED)
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
        await self.register(
            symbol=symbol,
            quantity=signed_qty,
            entry=float(entry),
            stop_loss=stop_loss,
            target=target,
            signal_id=signal_id if isinstance(signal_id, int) else None,
            strategy_id=payload.get("strategy_id"),
            broker_account_id=payload.get("account_id"),
        )

    async def _sweep(self) -> None:
        async with self._lock:
            items = list(self._book.items())
        for symbol, mp in items:
            quote = await self._md.get_quote(symbol)
            if quote is None:
                continue
            last = float(quote.last_price)
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
        # Settle in the DB + in-memory paper book.
        await asyncio.get_running_loop().run_in_executor(
            None, _settle_exit,
            self._session_factory, mp, exit_price, pnl, reason,
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
            "signal_id": mp.signal_id,
            "strategy_id": mp.strategy_id,
        }
        await event_bus.publish(CHANNEL_TRADE_CLOSED, result)
        return result


# -- DB helpers (sync, run in executor) ----------------------------------


def _open_position_symbols(session_factory: Callable[[], Any]) -> list[str]:
    """Symbols of every open (non-zero quantity) position row."""
    from app.db.models import Position as PositionRow

    with session_factory() as session:
        rows = session.query(PositionRow).filter(PositionRow.quantity != 0).all()
        return [r.symbol for r in rows]


def _open_position_as_managed(
    session_factory: Callable[[], Any], symbol: str
) -> Optional[tuple[ManagedPosition, float]]:
    """Reconstruct a ManagedPosition from a DB position row so it can be
    closed even when it isn't in the in-memory managed book.

    Returns (managed_position, fallback_exit_price) or None when there
    is no open position for the symbol.
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
        mp = ManagedPosition(
            symbol=row.symbol,
            quantity=int(row.quantity),
            entry=float(row.average_price),
            stop_loss=None,
            target=None,
            signal_id=None,
            strategy_id=row.strategy_id,
            broker_account_id=None,
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
                },
            )
        )
        session.commit()
