"""`/api/orders` — manual order placement for the Trade page.

The auto-pipeline (signals → risk → backend) lives elsewhere; this
endpoint is the operator-driven counterpart. The risk engine is
still consulted (always), but with the soft-warn model from
`place_manual_order` the operator can confirm-bypass on a block.

Endpoints:
  POST /api/orders              place one manual order
  POST /api/orders/cancel       cancel a pending order
  GET  /api/orders/pending      list PENDING trades for the
                                Trade page's pending-orders panel
  GET  /api/orders/quote        last cached quote for a symbol
                                (Trade page polls this; Fyers
                                postback feeds the cache)

The Trade page is REAL-MONEY ONLY. Accounts in paper_mode are
deliberately hidden from the broker picker.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.core import get_db  # shared DB dependency  # noqa: F401  (re-export for symmetry)
from app.db.models import (
    AuditLog,
    BrokerAccount,
    Trade,
)
from app.execution.base import OrderSide, OrderState, OrderType, ProductType
from app.execution.fyers_live import FyersBlockedError
from app.execution.market_data import Quote
from app.logging_config import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/orders", tags=["orders"])

# A bus-cached quote older than this is treated as stale and refreshed
# from the live source. The Trade page polls the quote every ~3s; the
# endpoint checks the in-process bus BEFORE the live fetch, so without a
# freshness gate the very first seed would be served for the rest of the
# session — freezing the ticket price at a single (soon wrong) value.
_BUS_QUOTE_MAX_AGE_S = 6.0


async def _fyers_quote(db: Session, sym: str) -> Optional[dict[str, Any]]:
    """Live quote via the connected real Fyers account — the sole price
    source for every symbol (equity, index, future, option). Returns None
    when no Fyers account is connected or the broker call fails/returns empty."""
    acc = (
        db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
                BrokerAccount.enabled == True,  # noqa: E712
                BrokerAccount.access_token.is_not(None),
            ).order_by(BrokerAccount.id.asc())
        )
        .scalars()
        .first()
    )
    if acc is None:
        return None
    backend = _manager()._manual_backend_for(acc)  # noqa: SLF001
    if backend is None or not hasattr(backend, "get_quote"):
        return None
    try:
        quotes = await backend.get_quote([sym])
    except Exception:  # noqa: BLE001
        return None
    if not quotes:
        return None
    q = quotes[0]
    return {
        "last_price": q.last_price,
        "bid": q.bid,
        "ask": q.ask,
        "volume": q.volume,
    }


def _bus_quote_is_fresh(q: Quote) -> bool:
    """True when a bus quote is recent enough to serve as-is.

    A real feed (Fyers websocket / poll) re-publishes every few seconds
    so its entries stay fresh; a one-off seed or a paper-fill publish
    goes stale and falls through to a fresh live Fyers fetch."""
    ts = getattr(q, "timestamp", None)
    if ts is None:
        return False
    try:
        age = (datetime.now(timezone.utc) - ts).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0.0 <= age <= _BUS_QUOTE_MAX_AGE_S


def _is_simulated(q: Quote) -> bool:
    """True for paper-mode synthetic prices. The Trade page must NEVER
    display these — the paper QuoteFeed seeds hash-based fake prices into
    the SHARED bus for every watched symbol, and serving them as a quote
    showed wrong prices for any symbol the operator had paper-traded."""
    return bool(getattr(q, "extra", None) and q.extra.get("simulated"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _manager():
    """Late import: the ExecutionManager is constructed in the
    FastAPI lifespan. We grab the same singleton the rest of the
    app uses via `app.main.app.state.execution_manager`."""
    from app.main import app

    mgr = getattr(app.state, "execution_manager", None)
    if mgr is None:  # tests sometimes run without the lifespan
        from app.execution.manager import Manager
        from app.execution.market_data import MarketDataBus
        from app.risk.engine import RiskEngine

        return Manager(
            market_data=MarketDataBus(),
            risk_engine=RiskEngine(),
        )
    return mgr


def _require_real_account(db: Session, account_id: int) -> BrokerAccount:
    """Return the broker_accounts row, or raise 404/400.

    Hard rule from the operator: the Trade page is REAL-MONEY ONLY.
    We 400 (not 404) for paper accounts so the UI can show a
    clearer "this is a paper account, not available here" message.
    """
    acc = db.get(BrokerAccount, account_id)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"broker_account {account_id} not found")
    if not acc.enabled:
        raise HTTPException(status_code=400, detail=f"broker_account {account_id} is disabled")
    if acc.paper_mode:
        raise HTTPException(
            status_code=400,
            detail=(
                f"broker_account {account_id} is a paper account. "
                "The Trade page is real-money only — pick a live Fyers/Dhan "
                "account from the dropdown."
            ),
        )
    if not acc.access_token:
        raise HTTPException(
            status_code=400,
            detail=(
                f"broker_account {account_id} has no access token. "
                "Complete OAuth in the Accounts page first."
            ),
        )
    return acc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("")
async def place_order(
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Place a manual order.

    Body:
      {
        "account_id":     2,                # broker_accounts.id
        "symbol":         "NSE:SBIN-EQ",    # Fyers-style
        "side":           "BUY",            # BUY | SELL
        "quantity":       10,
        "order_type":     "MARKET",         # MARKET | LIMIT | SL | SL-M
        "limit_price":    612.5,            # required for LIMIT
        "stop_price":     605.0,            # required for SL / SL-M
        "product_type":   "INTRADAY",       # INTRADAY | DELIVERY(CNC) | NORMAL(NRML) | MARGIN
        "bypass_risk":    false,            # true only after the operator types the confirm phrase
        "operator":       "ui_trade_page"
      }

    Returns:
      {
        "ok":              true,
        "blocked":         false,
        "bypassed_risk":   false,
        "risk_codes":      [],
        "risk_message":    "",
        "broker_order_id": "24061300012345",
        "status":          "PENDING" | "FILLED" | "REJECTED",
        "error":           null | "..."
      }
    """
    try:
        account_id = int(body["account_id"])
        symbol = str(body["symbol"]).strip().upper()
        side = str(body["side"]).strip().upper()
        quantity = int(body["quantity"])
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"missing or bad field: {e}")

    order_type = str(body.get("order_type", "MARKET")).upper()
    product_type = str(body.get("product_type", "INTRADAY")).upper()
    limit_price = body.get("limit_price")
    stop_price = body.get("stop_price")
    bypass_risk = bool(body.get("bypass_risk", False))
    operator = str(body.get("operator", "ui_trade_page"))

    if not symbol:
        raise HTTPException(status_code=422, detail="symbol is required")
    if quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be > 0")
    # Fyers v3 price requirements by type:
    #   LIMIT      -> limitPrice
    #   STOP_LOSS  -> SL-L (stop-limit): BOTH stopPrice (trigger) + limitPrice
    #   SL-M       -> stopPrice (trigger) only
    if order_type in ("LIMIT", "STOP_LOSS") and limit_price is None:
        raise HTTPException(status_code=422, detail="limit_price required for LIMIT / SL-L")
    if order_type in ("STOP_LOSS", "SL-M") and stop_price is None:
        raise HTTPException(status_code=422, detail="stop_price required for SL-L / SL-M")

    acc = _require_real_account(db, account_id)

    try:
        result = await _manager().place_manual_order(
            account=acc,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            product_type=product_type,
            bypass_risk=bypass_risk,
            operator=operator,
        )
    except FyersBlockedError as e:
        # CDN-level block. The manager normally converts this to a
        # REJECTED result inside `place_order`, but if it ever
        # bubbles (e.g. quote fetch blocked), surface it as 503 so
        # the operator sees the real cause.
        log.warning(
            "manual_order.place_blocked",
            status_code=e.status_code, reason=e.reason, symbol=symbol,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Fyers edge is blocking the request as a script "
                "(anti-bot / Cloudflare). The order was NOT placed. "
                "Check the server's outbound IP and User-Agent. "
                "See server logs for the full response."
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        # No backend available — the operator hasn't done OAuth yet
        # or the account row is mis-configured.
        raise HTTPException(status_code=400, detail=str(e))

    # 200 with ok=false when risk blocks; the UI shows the reason
    # and the confirm-phrase field. 200 with ok=true on a clean
    # place. 502 only on broker-side failures.
    if result.get("blocked") and not result.get("bypassed_risk"):
        return result
    if result.get("status") in ("REJECTED",):
        return result
    return result


@router.post("/cancel")
async def cancel_order(
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Cancel a pending order by broker_order_id.

    Body: `{ "account_id": 2, "broker_order_id": "24061300012345" }`
    """
    try:
        account_id = int(body["account_id"])
        broker_order_id = str(body["broker_order_id"]).strip()
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"missing or bad field: {e}")
    if not broker_order_id:
        raise HTTPException(status_code=422, detail="broker_order_id is required")
    acc = _require_real_account(db, account_id)
    # Look up the backend the same way Manager does.
    mgr = _manager()
    backend = mgr._manual_backend_for(acc)  # noqa: SLF001 — live backend for the chosen account
    if backend is None:
        raise HTTPException(status_code=400, detail=f"no backend for account {account_id}")
    try:
        ok = await backend.cancel_order(broker_order_id)
    except FyersBlockedError as e:
        # CDN-level block. Not a "broker said no" — the broker never
        # got the request. Surface a 503 so the operator understands
        # it's an infrastructure / anti-bot issue, not a trade one.
        log.warning(
            "manual_order.cancel_blocked",
            broker_order_id=broker_order_id, status_code=e.status_code,
            reason=e.reason,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Fyers edge is blocking the request as a script "
                "(anti-bot / Cloudflare). The order is untouched on "
                "the broker; retry once the server's IP / User-Agent "
                "is accepted. See server logs for the full response."
            ),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("manual_order.cancel_failed", broker_order_id=broker_order_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"broker cancel failed: {e}")
    # Update the local trade row to CANCELLED. We do this whenever
    # the broker confirms the order is no longer active — either it
    # accepted the cancel (ok=True) or it told us the order is gone
    # / already filled / already cancelled (ok=False but reachable).
    # Leaving the row as "placed" in those cases made the Trade page's
    # pending list loop forever: the UI clicked Cancel, the broker
    # said "already gone", and the row stayed visible with no
    # feedback. See `tests/test_trade_page.py::test_cancel_marks_local
    # _row_when_broker_rejects` for the contract.
    #
    # IMPORTANT: `broker_order_id` is NOT unique on `trades` — the
    # persist path can produce duplicate rows (one from the manual
    # place, one from a webhook re-write, etc.). We must NOT use
    # `scalar_one_or_none()` here; doing so raises
    # `MultipleResultsFound` on any duplicate and turns into a 500
    # with a plain-text body, which the frontend chokes on with
    # "body stream already read" when it tries to parse the error.
    # Iterate the matching rows and update all of them. See
    # `tests/test_trade_page.py::test_cancel_handles_duplicate_broker
    # _order_id_rows` for the regression.
    trades = (
        db.execute(
            select(Trade).where(Trade.broker_order_id == broker_order_id)
        )
        .scalars()
        .all()
    )
    reason: Optional[str] = None
    if ok:
        reason = "cancelled"
    else:
        # Broker rejects normally mean the order is no longer
        # cancellable: filled, rejected, already cancelled, or
        # simply not found. In all of those the order is gone
        # from the active set, so it should drop off the pending
        # list. We keep the local status as "cancelled" and note
        # the reason for the audit log.
        reason = "broker_rejected_already_gone"
    for trade in trades:
        trade.status = "cancelled"
        db.add(AuditLog(
            actor="ui_trade_page",
            action="order.manual_cancelled",
            target=f"account:{account_id}",
            before=None,
            after={
                "broker_order_id": broker_order_id,
                "symbol": trade.symbol,
                "broker_ok": bool(ok),
                "reason": reason,
            },
        ))
    if trades:
        db.commit()
    response: dict[str, Any] = {
        "ok": bool(ok),
        "broker_order_id": broker_order_id,
    }
    if trades:
        response["reason"] = reason
        response["rows_updated"] = len(trades)
    return response


@router.get("/pending")
def list_pending_orders(
    account_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List orders that are PENDING on the broker (not yet filled).

    Source: the local `trades` table where `status='placed'`. The
    Trade page's pending-orders panel polls this every few seconds.
    """
    q = select(Trade).where(Trade.status == "placed").order_by(Trade.created_at.desc())
    if account_id is not None:
        q = q.where(Trade.broker_account_id == account_id)
    rows = db.execute(q.limit(limit)).scalars().all()
    return {
        "ok": True,
        "count": len(rows),
        "orders": [
            {
                "id": r.id,
                "broker_order_id": r.broker_order_id,
                "broker_account_id": r.broker_account_id,
                "symbol": r.symbol,
                "side": r.side,
                "quantity": r.quantity,
                "price": r.price,
                "order_type": r.order_type,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/quote")
async def get_quote(
    symbol: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Live quote for `symbol`.

    Source priority — REAL-TIME first:
      1. Fresh in-process bus cache (a live feed re-publishes constantly).
      2. The connected Fyers account's real-time exchange quote
         (`/data/quotes`) — authoritative for any NSE/BSE equity, index,
         future or option. This is the ONLY live source (no public feed).
      3. A stale (but real) bus quote, else ok:false.
    """
    sym = symbol.upper()
    md = _manager().market_data
    cached = await md.get_quote(sym)
    # Serve the in-process bus cache ONLY when it's fresh and real (never
    # a simulated paper price). See `_bus_quote_is_fresh` / `_is_simulated`.
    if cached is not None and _bus_quote_is_fresh(cached) and not _is_simulated(cached):
        return {
            "ok": True,
            "symbol": cached.symbol,
            "last_price": cached.last_price,
            "bid": cached.bid,
            "ask": cached.ask,
            "volume": cached.volume,
            "source": "bus",
            "as_of": cached.timestamp.isoformat(),
        }

    # 1. Real-time exchange price via Fyers (preferred when connected).
    fy = await _fyers_quote(db, sym)
    if fy is not None and fy.get("last_price"):
        try:
            await md.publish(
                sym,
                float(fy["last_price"]),
                bid=fy.get("bid"),
                ask=fy.get("ask"),
                volume=fy.get("volume"),
            )
        except Exception:  # noqa: BLE001
            pass
        return {
            "ok": True,
            "symbol": sym,
            "last_price": fy["last_price"],
            "bid": fy.get("bid"),
            "ask": fy.get("ask"),
            "volume": fy.get("volume"),
            "source": "fyers",
            "as_of": datetime.now(timezone.utc).isoformat(),
        }

    # 2. A stale-but-real bus quote beats nothing.
    if cached is not None and not _is_simulated(cached):
        return {
            "ok": True,
            "symbol": cached.symbol,
            "last_price": cached.last_price,
            "bid": cached.bid,
            "ask": cached.ask,
            "volume": cached.volume,
            "source": "bus_stale",
            "as_of": cached.timestamp.isoformat(),
        }
    return {
        "ok": False,
        "symbol": sym,
        "reason": "no live quote — connect Fyers for real-time data (the token may have expired)",
    }
