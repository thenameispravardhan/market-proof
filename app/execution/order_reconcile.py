"""Shared Fyers order reconciliation.

A Fyers order update arrives via the **order WebSocket**
(`app.execution.fyers_stream`) or the REST reconcile sweep. Everything
funnels through `reconcile_order_update` so every path behaves the same:

  - Match the existing `trades` row by ``broker_order_id`` (NEVER create a
    signal — that would risk a re-trade feedback loop).
  - Update its status; on a fill, set the traded price + mirror the fill
    into the `positions` table.
  - Publish ``trades.filled`` / ``trade.executed`` for the UI relay and
    notifications.

Idempotent: keyed by ``broker_order_id`` and the resulting status, so a
WebSocket update and a postback for the same fill converge on the same
row rather than double-applying.

The Fyers-specific *parsing* helpers (``_unwrap_fyers``, ``_status_text``,
``_split_symbol``) live in `app.webhooks.fyers`, which is kept a pure,
DB-free module; this module owns the DB + event-bus side effects.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.db.models import AuditLog, Position as PositionRow, Trade as TradeRow
from app.execution.base import apply_fill
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)

# Fyers v3 numeric order-status codes -> text labels (same table as
# fyers_live._state_from_str). The old table had 4/5/6 shifted, so a real
# rejection (5) read as "AMO_MODIFIED" and the trade stayed "placed".
FYERS_STATUS_HINTS: dict[str, str] = {
    "1": "CANCELLED",
    "2": "FILLED",
    "4": "TRANSIT",
    "5": "REJECTED",
    "6": "PENDING",
    "7": "EXPIRED",
}

# Trading DB work runs on its own two threads — never on the event loop (an
# fsync / busy SQLite would freeze ticks and orders) and never queued behind
# Algo Lab backtests or PDF extraction on the default executor.
from concurrent.futures import ThreadPoolExecutor

TRADING_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trading-db")

_tasks: set = set()  # strong refs so background tasks aren't GC'd


def spawn(coro: Any) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# Fyers often confirms / rejects BEFORE the placing request has committed its
# trades row. Such an update is parked here and applied the moment the row
# is saved (apply_parked, called by the order path) — no polling ladder.
# A slow backstop retry covers rows written by other paths.
PARKED: dict[str, list[tuple[dict[str, Any], str]]] = {}
BACKSTOP_DELAYS = (0.5, 1.5, 3.0, 5.0)

# Ticket SL / target to arm once THIS order fills: order_id -> (symbol, sl, tp).
LEVELS_ON_FILL: dict[str, tuple[str, Any, Any]] = {}

# Exit orders waiting for their fill on the order WebSocket: order_id -> future
# resolved with the terminal status text (FILLED / REJECTED / CANCELLED ...).
ORDER_WAITERS: dict[str, "asyncio.Future[dict[str, Any]]"] = {}


# Terminal updates nobody was waiting for yet (the fill can beat the waiter).
RECENT_TERMINAL: dict[str, dict[str, Any]] = {}


def wait_for_order(order_id: str) -> "asyncio.Future[dict[str, Any]]":
    fut = ORDER_WAITERS.get(order_id)
    if fut is None or fut.done():
        fut = asyncio.get_running_loop().create_future()
        ORDER_WAITERS[order_id] = fut
        if order_id in RECENT_TERMINAL:
            fut.set_result(RECENT_TERMINAL.pop(order_id))
            ORDER_WAITERS.pop(order_id, None)
    return fut


async def wait_for_trade(order_id: str) -> bool:
    """True once a trades row with this broker_order_id is committed."""
    from app.db.session import SessionLocal

    def _has() -> bool:
        with SessionLocal() as db:
            return db.query(TradeRow.id).filter(TradeRow.broker_order_id == order_id).first() is not None

    loop = asyncio.get_running_loop()
    for d in BACKSTOP_DELAYS:
        await asyncio.sleep(d)
        if await loop.run_in_executor(TRADING_POOL, _has):
            return True
    return False


async def apply_parked(order_id: str) -> None:
    """Apply updates that arrived before this order's row existed."""
    from app.db.session import SessionLocal

    for payload, source in PARKED.pop(order_id, []):
        with SessionLocal() as db:
            await reconcile_order_update(db, payload, source=source, retry=False)


async def _backstop(order_id: str) -> None:
    if order_id in PARKED and await wait_for_trade(order_id):
        await apply_parked(order_id)
    PARKED.pop(order_id, None)  # never matched: an order placed outside the bot


def _split_symbol(raw: str) -> tuple[str, str]:
    """Split a Fyers-style symbol like "NSE:SBIN-EQ" into
    ("NSE", "SBIN-EQ"). Defaults to ("NSE", raw) when no prefix."""
    if not raw:
        return ("NSE", "")
    s = str(raw).strip()
    if ":" in s:
        exch, _, sym = s.partition(":")
        return (exch.strip().upper(), sym.strip())
    return ("NSE", s)


def _status_text(raw: Any) -> str:
    """Map a Fyers numeric status code to its text label. Falls back
    to the raw value as a string when it doesn't match the known set."""
    if raw is None:
        return "UNKNOWN"
    key = str(raw).strip()
    if key in FYERS_STATUS_HINTS:
        return FYERS_STATUS_HINTS[key]
    if key.isalpha():
        return key.upper()
    return key


def _unwrap_fyers(payload: Mapping[str, Any]) -> dict[str, Any]:
    """If the payload wraps the actual order object under `orders`,
    return the inner order. Otherwise return the payload unchanged.

    Fyers uses BOTH wrapper shapes: the order WebSocket delivers
    ``{"s": "ok", "orders": {<order>}}`` (a single dict), while other
    surfaces wrap a list (``{"orders": [{<order>}, ...]}``)."""
    inner = payload.get("orders")
    if isinstance(inner, list) and inner and isinstance(inner[0], dict):
        inner = inner[0]
    if isinstance(inner, dict):
        # Merge: wrapper-level fields first, then order-level fields
        # (order-level wins on collisions).
        merged: dict[str, Any] = {k: v for k, v in payload.items() if k != "orders"}
        merged.update(inner)
        return merged
    return dict(payload)

# Fyers status text (from `_status_text`) -> our internal trade status.
_STATUS_MAP = {
    "FILLED": "filled",
    "CANCELLED": "cancelled",
    "REJECTED": "rejected",
    "EXPIRED": "cancelled",
}
_TERMINAL = {"filled", "rejected", "cancelled"}


async def reconcile_order_update(
    db: Session, payload: Mapping[str, Any], *, source: str = "fyers", retry: bool = True
) -> dict[str, Any]:
    """Reconcile a single Fyers order update against the trades table.

    Matches by broker_order_id, never creates a signal, deduped (WebSocket +
    postback + polls converge). The DB work runs on TRADING_POOL; events,
    exit waiters and SL/TP arming happen back on the loop, AFTER the commit.
    """
    order = _unwrap_fyers(payload)
    order_id = str(order.get("id") or order.get("order_id") or "").strip()
    status_text = _status_text(order.get("status"))
    _exch, sym = _split_symbol(str(order.get("symbol") or ""))
    if not order_id:
        log.warning("fyers.reconcile.no_order_id", source=source, status=status_text, symbol=sym)
        return {"ok": False, "reason": "no order id in payload"}

    res = await asyncio.get_running_loop().run_in_executor(TRADING_POOL, _reconcile_sync, db, order, order_id, status_text, source)

    if status_text in ("FILLED", "REJECTED", "CANCELLED", "EXPIRED"):
        fut = ORDER_WAITERS.pop(order_id, None)
        if fut is not None and not fut.done():
            fut.set_result(dict(order))
        else:
            RECENT_TERMINAL[order_id] = dict(order)
            while len(RECENT_TERMINAL) > 500:
                RECENT_TERMINAL.pop(next(iter(RECENT_TERMINAL)))

    if not res.get("matched"):
        if retry:
            PARKED.setdefault(order_id, []).append((dict(payload), source))
            if len(PARKED[order_id]) == 1:
                spawn(_backstop(order_id))
        await event_bus.publish("broker", {"order_id": order_id, "status": status_text, "symbol": sym, "source": source})
        return res

    if res.get("changed"):
        await event_bus.publish(
            "trades.filled" if res["status"] == "filled" else "trade.executed",
            {"trade_id": res["trade_id"], "symbol": res["symbol"], "status": res["status"],
             "broker_order_id": order_id, "price": res.get("price"), "source": source},
        )
        if res.get("filled_now") and order_id in LEVELS_ON_FILL:
            symbol, sl, tp = LEVELS_ON_FILL.pop(order_id)
            spawn(_arm_levels(order_id, symbol, sl, tp))
        if res["status"] in ("rejected", "cancelled") and not res.get("filled_now"):
            LEVELS_ON_FILL.pop(order_id, None)
    await event_bus.publish("broker", {"order_id": order_id, "status": status_text, "symbol": sym, "source": source})
    return {k: v for k, v in res.items() if k not in ("changed", "filled_now", "symbol", "price")}


async def _arm_levels(order_id: str, symbol: str, sl: Any, tp: Any) -> None:
    from app.main import app

    tm = getattr(app.state, "trade_manager", None)
    if tm is None:
        return
    try:
        await tm.update_levels(symbol, stop_loss=sl, target=tp)
        log.info("manual_order.levels_armed", order_id=order_id, symbol=symbol, stop_loss=sl, target=tp)
    except Exception:  # noqa: BLE001
        log.exception("manual_order.levels_arm_failed", order_id=order_id)


def _reconcile_sync(db: Session, order: dict[str, Any], order_id: str, status_text: str, source: str) -> dict[str, Any]:
    new_status = _STATUS_MAP.get(status_text, "placed")
    # broker_order_id isn't unique on `trades` (see orders.cancel_order), and
    # `one_or_none()` raised MultipleResultsFound on a duplicate — the update
    # was lost and the order sat "placed" forever. The oldest row carries the
    # fill; the rest are kept in step with its status below.
    rows = (
        db.query(TradeRow)
        .filter(TradeRow.broker_order_id == order_id)
        .order_by(TradeRow.id.asc())
        .all()
    )
    trade = rows[0] if rows else None
    duplicates = rows[1:]
    if trade is None:
        log.info("fyers.reconcile.unmatched_order", source=source, order_id=order_id, status=status_text)
        return {"ok": True, "matched": False, "order_id": order_id, "status": new_status}

    # Partial fills (manual orders only — the auto pipeline mirrors its own
    # fills): apply each new filledQty slice to the position as it arrives.
    filled_now = False
    manual = trade.signal_id is None
    reported = order.get("filledQty")
    try:
        reported = int(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported = None
    already = int(trade.filled_qty or 0)
    target_filled = reported if reported is not None else (int(trade.quantity or 0) if new_status == "filled" else already)
    delta = target_filled - already if manual else 0
    price = order.get("tradedPrice") or order.get("limitPrice")

    if trade.status == new_status and delta <= 0:
        log.info("fyers.reconcile.deduped", source=source, order_id=order_id, status=new_status)
        return {"ok": True, "matched": True, "deduped": True, "trade_id": trade.id, "status": new_status}
    # A terminal row never goes back (no fill downgraded; a late transit /
    # pending echo can't resurrect a rejected or cancelled order).
    if (trade.status == "filled" and new_status != "filled") or (trade.status in _TERMINAL and new_status == "placed" and delta <= 0):
        log.info("fyers.reconcile.kept_fill", source=source, order_id=order_id, ignored_status=new_status)
        return {"ok": True, "matched": True, "deduped": True, "trade_id": trade.id, "status": trade.status}

    try:
        if price is not None and (delta > 0 or new_status == "filled"):
            trade.price = float(price)
    except (TypeError, ValueError):
        pass
    if manual and delta > 0:
        _apply_fill_to_position(db, trade, delta)
        trade.filled_qty = already + delta
        filled_now = True
    elif not manual and new_status == "filled" and trade.status != "filled":
        _apply_fill_to_position(db, trade, int(trade.quantity or 0))
        filled_now = True
    if trade.status != "filled":
        trade.status = new_status
    if new_status == "filled":
        trade.executed_at = datetime.now(timezone.utc)
    for dup in duplicates:
        if dup.status != "filled":
            dup.status = trade.status
    db.add(AuditLog(actor="system", action=f"{source}.{new_status}", target=f"trade:{trade.id}",
                    after={"order_id": order_id, "status": new_status, "price": trade.price, "filled": trade.filled_qty}))
    db.commit()
    log.info("fyers.reconcile.reconciled", source=source, order_id=order_id, trade_id=trade.id, status=trade.status)
    return {"ok": True, "matched": True, "trade_id": trade.id, "status": trade.status, "changed": True,
            "filled_now": filled_now, "symbol": trade.symbol, "price": trade.price}


def _apply_fill_to_position(db: Session, trade: TradeRow, qty: int | None = None) -> None:
    """Mirror a confirmed Fyers fill (or one partial slice of it) into positions."""
    qty = int(trade.quantity or 0) if qty is None else int(qty)
    if qty <= 0:
        return
    signed = qty if (trade.side or "").upper() == "BUY" else -qty
    pos = db.query(PositionRow).filter_by(symbol=trade.symbol).one_or_none()
    if pos is None:
        db.add(
            PositionRow(
                symbol=trade.symbol,
                quantity=signed,
                average_price=float(trade.price or 0.0),
                last_price=float(trade.price or 0.0),
                unrealized_pnl=0.0,
                product=trade.product or "INTRADAY",
            )
        )
    else:
        pos.quantity, pos.average_price = apply_fill(pos.quantity, pos.average_price, signed, trade.price)
        pos.last_price = float(trade.price or 0.0)
