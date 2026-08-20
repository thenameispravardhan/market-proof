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

from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy.orm import Session

from app.db.models import AuditLog, Position as PositionRow, Trade as TradeRow
from app.logging_config import get_logger
from app.services.event_bus import event_bus

log = get_logger(__name__)

# Fyers numeric order-status codes -> text labels.
FYERS_STATUS_HINTS: dict[str, str] = {
    "1": "CANCELLED",
    "2": "FILLED",
    "4": "REJECTED",
    "5": "AMO_MODIFIED",
    "6": "AMO_CANCELLED",
    "7": "MODIFIED",
    "8": "EXPIRED",
    "10": "TRIGGERED",
    "11": "AMO_FROZEN",
}


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
}


async def reconcile_order_update(
    db: Session, payload: Mapping[str, Any], *, source: str = "fyers"
) -> dict[str, Any]:
    """Reconcile a single Fyers order update against the trades table.

    `payload` is the raw Fyers order object (or a wrapper around it).
    `source` tags where the update came from (e.g. ``fyers_order_ws``)
    for the audit trail and the published event. Historical rows may
    carry ``fyers_postback`` from the since-removed webhook receiver.
    Commits `db` on a match. Returns a result dict describing the outcome.
    """
    order = _unwrap_fyers(payload)
    order_id = str(order.get("id") or order.get("order_id") or "").strip()
    status_text = _status_text(order.get("status"))
    _exch, sym = _split_symbol(str(order.get("symbol") or ""))
    traded_price = order.get("tradedPrice") or order.get("limitPrice")

    if not order_id:
        log.warning("fyers.reconcile.no_order_id", source=source, status=status_text, symbol=sym)
        return {"ok": False, "reason": "no order id in payload"}

    new_status = _STATUS_MAP.get(status_text, "placed")

    trade = (
        db.query(TradeRow).filter(TradeRow.broker_order_id == order_id).one_or_none()
    )
    if trade is None:
        # An order we didn't originate (manual order in the Fyers app, or
        # a stale id). Acknowledge without creating anything — a broker
        # update must NEVER spin up a new signal.
        log.info(
            "fyers.reconcile.unmatched_order",
            source=source, order_id=order_id, status=status_text,
        )
        return {"ok": True, "matched": False, "order_id": order_id, "status": new_status}

    # Dual-confirmation dedup: the same fill can arrive via the order
    # WebSocket, the postback webhook AND the entry state machine's REST
    # poll. The FIRST confirmation wins; a repeat of the same status is
    # acknowledged without re-applying (re-applying a fill would double
    # the position), and a terminal `filled` row is never downgraded by
    # a late cancel/pending echo (an IOC partial's final broker status
    # is "cancelled" even though shares traded).
    if trade.status == new_status:
        log.info(
            "fyers.reconcile.deduped",
            source=source, order_id=order_id, status=new_status,
        )
        return {
            "ok": True, "matched": True, "deduped": True,
            "trade_id": trade.id, "status": new_status,
        }
    if trade.status == "filled" and new_status != "filled":
        log.info(
            "fyers.reconcile.kept_fill",
            source=source, order_id=order_id, ignored_status=new_status,
        )
        return {
            "ok": True, "matched": True, "deduped": True,
            "trade_id": trade.id, "status": trade.status,
        }

    trade.status = new_status
    if new_status == "filled":
        try:
            if traded_price is not None:
                trade.price = float(traded_price)
        except (TypeError, ValueError):
            pass
        trade.executed_at = datetime.now(timezone.utc)
        _apply_fill_to_position(db, trade)

    db.add(
        AuditLog(
            actor="system",
            action=f"{source}.{new_status}",
            target=f"trade:{trade.id}",
            after={"order_id": order_id, "status": new_status, "price": trade.price},
        )
    )
    db.commit()

    await event_bus.publish(
        "trades.filled" if new_status == "filled" else "trade.executed",
        {
            "trade_id": trade.id,
            "symbol": trade.symbol,
            "status": new_status,
            "broker_order_id": order_id,
            "price": trade.price,
            "source": source,
        },
    )
    log.info(
        "fyers.reconcile.reconciled",
        source=source, order_id=order_id, trade_id=trade.id, status=new_status,
    )
    return {"ok": True, "matched": True, "trade_id": trade.id, "status": new_status}


def _apply_fill_to_position(db: Session, trade: TradeRow) -> None:
    """Mirror a confirmed Fyers fill into the positions table."""
    qty = int(trade.quantity or 0)
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
            )
        )
    else:
        new_qty = pos.quantity + signed
        if signed > 0 and new_qty != 0:
            pos.average_price = (
                (pos.average_price * pos.quantity + float(trade.price or 0.0) * signed)
                / new_qty
            )
        pos.quantity = new_qty
        pos.last_price = float(trade.price or 0.0)
