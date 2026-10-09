"""`/api/orders` — manual order placement for the Trade page.

The auto-pipeline (signals → risk → backend) lives elsewhere; this
endpoint is the operator-driven counterpart. The risk engine is
still consulted (always), but with the soft-warn model from
`place_manual_order` the operator can confirm-bypass on a block.

Endpoints:
  POST /api/orders              place one manual order
  POST /api/orders/cancel       cancel a pending order
  POST /api/orders/modify       modify a pending order (size down,
                                price, type) — never a size increase,
                                refused while trading is halted
  GET  /api/orders/pending      list PENDING trades for the
                                Trade page's pending-orders panel
  GET  /api/orders/quote        last cached quote for a symbol
                                (Trade page polls this; Fyers
                                postback feeds the cache)

The Trade page is REAL-MONEY ONLY. Accounts in paper_mode are
deliberately hidden from the broker picker.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
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
from app.risk import circuit_breakers

log = get_logger(__name__)

router = APIRouter(prefix="/api/orders", tags=["orders"])

# A bus-cached quote older than this is treated as stale and refreshed
# from the live source. The Trade page polls the quote every ~3s; the
# endpoint checks the in-process bus BEFORE the live fetch, so without a
# freshness gate the very first seed would be served for the rest of the
# session — freezing the ticket price at a single (soon wrong) value.
_BUS_QUOTE_MAX_AGE_S = 6.0

# Manual orders currently being sent, keyed on every field that defines the
# order (see `place_order`). Process-local: the app runs a single worker.
_IN_FLIGHT: set[tuple[Any, ...]] = set()


async def _fyers_quote(db: Session, sym: str) -> Optional[dict[str, Any]]:
    """Live quote via the connected real Fyers account — the sole price
    source for every symbol (equity, index, future, option). Returns None
    when no Fyers account is connected or the broker call fails/returns empty.

    Market-data path: keyed on a connected (token-bearing) account, NOT the
    `enabled` trading switch — the Trade-page price must keep updating even
    when Fyers trading is toggled off. Order placement is gated separately
    (`_require_real_account`)."""
    acc = (
        db.execute(
            select(BrokerAccount).where(
                BrokerAccount.broker == "fyers",
                BrokerAccount.paper_mode == False,  # noqa: E712
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


MANUAL_PRODUCTS = {"INTRADAY", "DELIVERY", "MARGIN"}
_ORDER_TYPE_ALIASES = {"SL": "STOP_LOSS", "SL-L": "STOP_LOSS", "SLL": "STOP_LOSS",
                       "SLM": "SL-M", "SL_M": "SL-M", "STOP_LOSS_MARKET": "SL-M"}
_PRODUCT_ALIASES = {"MIS": "INTRADAY", "CNC": "DELIVERY", "NRML": "MARGIN"}


def _fyers_stream():
    """The realtime Fyers stream manager from the lifespan, or None when
    it isn't running (tests / streaming disabled)."""
    from app.main import app

    return getattr(app.state, "fyers_stream", None)


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
        "order_type":     "MARKET",         # MARKET | LIMIT | STOP_LOSS (alias SL / SL-L) | SL-M
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
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"missing or bad field: {e}")
    # `int(10.5)` silently became 10 — a different order than the one asked for.
    quantity = _opt_int_field(body, "quantity")
    if quantity is None:
        raise HTTPException(status_code=422, detail="missing or bad field: 'quantity'")
    if side not in ("BUY", "SELL"):
        raise HTTPException(status_code=422, detail=f"side must be BUY or SELL, got {side!r}")

    order_type = str(body.get("order_type") or "MARKET").strip().upper()
    # The docstring (and the Fyers / Zerodha vocabulary) says SL / SL-L for a
    # stop-limit, but only "STOP_LOSS" was accepted — "SL" died as a 422
    # "unknown order_type" after skipping the price checks below.
    order_type = _ORDER_TYPE_ALIASES.get(order_type, order_type)
    product_type = str(body.get("product_type", "INTRADAY")).upper()
    # Prices are checked here (a string, 0 or a negative used to reach the
    # broker call), and a price that doesn't apply to the order type is
    # dropped rather than carried into the trade row.
    limit_price = _opt_price_field(body, "limit_price") if order_type in ("LIMIT", "STOP_LOSS") else None
    stop_price = _opt_price_field(body, "stop_price") if order_type in ("STOP_LOSS", "SL-M") else None
    # `bool("false")` is True: only a real true counts as the override.
    bypass_risk = body.get("bypass_risk") is True
    operator = str(body.get("operator", "ui_trade_page"))

    # The BOT trades INTRADAY only. A manual Trade-page order may also be
    # DELIVERY (CNC) or MARGIN (F&O carry-forward); those rows carry their
    # product so the EOD square-off leaves them alone. CO/BO are not supported.
    product_type = _PRODUCT_ALIASES.get(product_type, product_type)
    if product_type not in MANUAL_PRODUCTS:
        raise HTTPException(
            status_code=422,
            detail=f"product_type {product_type!r} not allowed — use one of {sorted(MANUAL_PRODUCTS)}",
        )

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

    lot_problem = await _lot_problem(symbol, quantity)
    if lot_problem:
        raise HTTPException(status_code=422, detail=lot_problem)

    acc = _require_real_account(db, account_id)

    # A double click (or a retry while the first request is still waiting on
    # Fyers) sent the same real-money order twice. While an identical order
    # is in flight, the second one is refused; once it returns, the operator
    # can deliberately place the same order again.
    key = (account_id, symbol, side, quantity, order_type, product_type, limit_price, stop_price)
    if key in _IN_FLIGHT:
        raise HTTPException(
            status_code=409,
            detail="An identical order is already being sent. Wait for its result before placing it again.",
        )
    _IN_FLIGHT.add(key)
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
    finally:
        _IN_FLIGHT.discard(key)

    # 200 with ok=false when risk blocks; the UI shows the reason
    # and the confirm-phrase field. 200 with ok=true on a clean
    # place. 502 only on broker-side failures.
    if result.get("blocked") and not result.get("bypassed_risk"):
        return result
    if result.get("status") in ("REJECTED",):
        return result
    # Optional SL / target from the ticket: armed on the position once THIS
    # order fills (a resting limit may fill much later), never before.
    sl, tp = _opt_price(body.get("stop_loss")), _opt_price(body.get("target"))
    if (sl is not None or tp is not None) and result.get("broker_order_id"):
        from app.execution.order_reconcile import LEVELS_ON_FILL

        # Armed by the reconciler the moment THIS order's fill lands — or now,
        # if the fill was already applied (instant fill / parked update).
        oid = str(result["broker_order_id"])
        LEVELS_ON_FILL[oid] = (symbol.upper(), sl, tp)
        row = db.query(Trade.status).filter(Trade.broker_order_id == oid).first()
        if row is not None and row[0] == "filled":
            from app.execution.order_reconcile import _arm_levels, spawn

            spawn(_arm_levels(oid, *LEVELS_ON_FILL.pop(oid)))
        result["levels_pending"] = {"stop_loss": sl, "target": tp}
    return result


async def _lot_problem(symbol: str, quantity: int) -> Optional[str]:
    """`fno.lot_error`, downloading the F&O scrip master first when the lot is
    unknown only because it hasn't been fetched yet (fresh install)."""
    from app.algo import fno

    problem = fno.lot_error(symbol, quantity)
    if problem and fno.is_derivative(symbol) and fno.contract_lot(symbol) is None:
        try:
            await fno.ensure_master()
        except Exception as e:  # noqa: BLE001 — the refusal below stands
            log.warning("orders.fno_master_refresh_failed", error=str(e)[:200])
        problem = fno.lot_error(symbol, quantity)
    return problem


async def _commit_off_loop(db: Session) -> None:
    """The commit (fsync + SQLite write lock, up to its busy timeout) runs on
    the trading pool so it can't freeze ticks and orders on the event loop."""
    import asyncio

    from app.execution.order_reconcile import TRADING_POOL

    await asyncio.get_running_loop().run_in_executor(TRADING_POOL, db.commit)


def _opt_price(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None




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
    # Update the local trade rows. A broker "no" is NOT proof the order is
    # gone: `cancel_order` also answers False on a timeout / 5xx / 429
    # (the order is still working) and on an order that already FILLED.
    # Marking those "cancelled" hid a live order from the pending list and
    # turned a real fill into a cancel (the postback then skipped it as
    # settled). So on False we ask the broker what the order actually is:
    #   - cancelled / rejected / expired -> mark the rows to match;
    #   - filled -> reconcile the fill (status + position) instead;
    #   - still working / unknown        -> leave the rows alone.
    # Only rows still "placed" are touched — a settled row never flips.
    #
    # `broker_order_id` is NOT unique on `trades` (the persist path can
    # produce duplicates), so every matching row is read, never
    # `scalar_one_or_none()` (MultipleResultsFound -> a plain-text 500).
    # See tests/test_trade_page.py for both contracts.
    reason: str = "cancelled"
    new_status: Optional[str] = "cancelled"
    message: Optional[str] = None
    if not ok:
        reason, new_status, message, fill = await _status_after_refused_cancel(backend, broker_order_id)
        if fill is not None:
            from app.execution.order_reconcile import reconcile_order_update

            try:
                await reconcile_order_update(db, fill, source="manual_cancel")
            except Exception:  # noqa: BLE001
                log.exception("manual_order.cancel_fill_reconcile_failed", broker_order_id=broker_order_id)
    trades = (
        db.execute(
            select(Trade).where(Trade.broker_order_id == broker_order_id)
        )
        .scalars()
        .all()
    )
    updated = 0
    for trade in trades:
        if new_status is None or trade.status != "placed":
            continue
        trade.status = new_status
        updated += 1
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
    if updated:
        await _commit_off_loop(db)
    response: dict[str, Any] = {
        "ok": bool(ok),
        "broker_order_id": broker_order_id,
        "reason": reason,
        "rows_updated": updated,
    }
    if message:
        response["message"] = message
    return response


async def _status_after_refused_cancel(
    backend: Any, broker_order_id: str
) -> tuple[str, Optional[str], str, Optional[dict[str, Any]]]:
    """After the broker refused a cancel, what is the order really?

    Returns `(reason, new_local_status, message, fill)`: `new_local_status`
    None leaves the rows alone; `fill` is the broker's order payload when
    the order had (partly) filled, for the caller to reconcile."""
    if not hasattr(backend, "get_order_status"):
        return ("broker_refused", None, "the broker refused the cancel; the order may still be working", None)
    try:
        st = await backend.get_order_status(broker_order_id)
    except Exception as e:  # noqa: BLE001
        log.warning("manual_order.cancel_status_failed", broker_order_id=broker_order_id, error=str(e))
        st = None
    state = getattr(st, "state", None)
    raw = getattr(st, "raw", None)
    # Only a status that names THIS order counts; an error or empty book is "unknown".
    if isinstance(raw, dict) and str(raw.get("id") or "") == broker_order_id:
        if state == OrderState.FILLED:
            return ("already_filled", None, "the order had already filled — it is in your positions", raw)
        if state in (OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED):
            local = "rejected" if state == OrderState.REJECTED else "cancelled"
            # A cancelled order can carry a partial fill — reconcile it so
            # those shares reach the positions table.
            fill = raw if int(getattr(st, "filled_quantity", 0) or 0) > 0 else None
            return ("already_gone", local, f"the order was already {state.value.lower()} at the broker", fill)
    return (
        "broker_refused",
        None,
        "the broker didn't cancel the order and it may still be working — "
        "check the Orders tab and try again",
        None,
    )


def _opt_int_field(body: dict[str, Any], key: str) -> Optional[int]:
    """An optional whole-number body field: None when absent, 422 when it
    isn't a whole number (10.5 is refused, not truncated)."""
    v = body.get(key)
    if v is None:
        return None
    try:
        f = float(v) if not isinstance(v, bool) else math.nan
    except (TypeError, ValueError):
        f = math.nan
    if not math.isfinite(f) or not f.is_integer():
        raise HTTPException(status_code=422, detail=f"bad field {key}: {v!r} (whole number expected)")
    return int(f)


def _opt_price_field(body: dict[str, Any], key: str) -> Optional[float]:
    """An optional price body field: None when absent, 422 unless > 0."""
    v = body.get(key)
    if v is None:
        return None
    try:
        f = float(v) if not isinstance(v, bool) else math.nan
    except (TypeError, ValueError):
        f = math.nan
    if not math.isfinite(f) or f <= 0:
        raise HTTPException(status_code=422, detail=f"bad field {key}: {v!r} (must be > 0)")
    return f


def _trading_halt_reason(db: Session) -> Optional[str]:
    """Why trading is halted, or None when it isn't. Reads the same
    `RiskState.trading_disabled` flag `/api/risk/kill` sets (also set by
    the daily-loss / monthly-drawdown breakers); cleared only by a
    calendar rollover or `/api/risk/resume`."""
    state = circuit_breakers.get_or_create_state(db)
    if state.trading_disabled:
        return state.disabled_reason or "trading_disabled"
    return None


async def _current_order_qty(trades: list[Trade], backend: Any, broker_order_id: str) -> Optional[int]:
    """The order's current size, for refusing modify-time increases.

    Prefers the local `trades` rows (the smallest positive quantity — the
    rows for one order should agree, and the smallest is the safe bound),
    else the live backend's order status (Fyers' `qty` on the order
    matching this id). None when neither can say."""
    known = [int(t.quantity) for t in trades if (t.quantity or 0) > 0]
    if known:
        return min(known)
    if not hasattr(backend, "get_order_status"):
        return None
    try:
        order_status = await backend.get_order_status(broker_order_id)
    except Exception as e:  # noqa: BLE001
        log.debug("manual_order.modify_status_failed", broker_order_id=broker_order_id, error=str(e))
        return None
    raw = getattr(order_status, "raw", None)
    if not isinstance(raw, dict) or str(raw.get("id") or "") != broker_order_id:
        return None
    try:
        qty = int(float(raw.get("qty")))
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


@router.post("/modify")
async def modify_order(
    body: dict[str, Any] = Body(...),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Modify a pending order in place.

    Body:
      {
        "account_id":      2,
        "broker_order_id": "24061300012345",
        "quantity":        5,          # optional; may only go DOWN
        "limit_price":     611.0,      # optional
        "stop_price":      605.0,      # optional
        "order_type":      "LIMIT"     # optional: LIMIT | MARKET | STOP_LOSS | SL-M
      }

    Safety:
      - 409 while trading is halted (kill switch / loss breaker). Cancel
        stays allowed — only cancelling reduces exposure.
      - A size INCREASE is refused (422): a bigger order must be placed
        new so the risk engine checks it. The current size comes from the
        local `trades` rows, else the broker's order status; when neither
        knows it, only price / type changes are accepted.
      - A new order_type needs its prices, exactly as for place_order.
      - Never changes the product type (intraday-only bot).

    Returns `{ok, broker_order_id, message, rows_updated}` — `message` is
    the broker's. On success every local `trades` row for the order takes
    the new quantity / price / type, with one `order.manual_modified`
    audit row each. 400 when the account's broker can't modify, 503 when
    the Fyers edge blocks the request, 502 on any other broker failure.
    """
    try:
        account_id = int(body["account_id"])
        broker_order_id = str(body["broker_order_id"]).strip()
    except (KeyError, TypeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"missing or bad field: {e}")
    if not broker_order_id:
        raise HTTPException(status_code=422, detail="broker_order_id is required")
    quantity = _opt_int_field(body, "quantity")
    limit_price = _opt_price_field(body, "limit_price")
    stop_price = _opt_price_field(body, "stop_price")
    order_type: Optional[OrderType] = None
    if body.get("order_type") is not None:
        try:
            raw_type = str(body["order_type"]).strip().upper()
            order_type = OrderType(_ORDER_TYPE_ALIASES.get(raw_type, raw_type))
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"order_type must be LIMIT, MARKET, STOP_LOSS or SL-M, got {body['order_type']!r}",
            )
    if quantity is None and limit_price is None and stop_price is None and order_type is None:
        raise HTTPException(
            status_code=422,
            detail="nothing to modify — send quantity, limit_price, stop_price and/or order_type",
        )
    if quantity is not None and quantity <= 0:
        raise HTTPException(status_code=422, detail="quantity must be > 0")
    # Same price rules as place_order (Fyers v3): LIMIT -> limitPrice;
    # STOP_LOSS (SL-L) -> stopPrice + limitPrice; SL-M -> stopPrice.
    if order_type in (OrderType.LIMIT, OrderType.STOP_LOSS) and limit_price is None:
        raise HTTPException(status_code=422, detail="limit_price required for LIMIT / SL-L")
    if order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET) and stop_price is None:
        raise HTTPException(status_code=422, detail="stop_price required for SL-L / SL-M")

    acc = _require_real_account(db, account_id)
    halt = _trading_halt_reason(db)
    if halt:
        raise HTTPException(
            status_code=409,
            detail=(
                f"trading is halted ({halt}) — orders can't be modified while the "
                "kill switch / loss limit is engaged. Cancel still works; resume "
                "trading first to modify."
            ),
        )
    backend = _manager()._manual_backend_for(acc)  # noqa: SLF001 — live backend for the chosen account
    if backend is None:
        raise HTTPException(status_code=400, detail=f"no backend for account {account_id}")
    if not hasattr(backend, "modify_order"):
        raise HTTPException(status_code=400, detail="this broker does not support modify")

    # Not unique (see cancel_order) — read every row for this order. Only
    # rows still "placed" describe a working order: a filled / cancelled /
    # rejected row is history, and a modify must never rewrite its size or
    # price (it also isn't a valid bound for the current size).
    all_rows = list(
        db.execute(select(Trade).where(Trade.broker_order_id == broker_order_id)).scalars().all()
    )
    trades = [t for t in all_rows if t.status == "placed"]
    if quantity is not None:
        filled = max((int(t.filled_qty or 0) for t in trades), default=0)
        if quantity < filled:
            raise HTTPException(
                status_code=422,
                detail=f"quantity can't go below what already filled ({filled})",
            )
        current = await _current_order_qty(trades, backend, broker_order_id)
        if current is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "can't verify this order's current quantity, so only price / "
                    "type changes are allowed — increase size with a new order so "
                    "the risk engine checks it"
                ),
            )
        sym = next((t.symbol for t in trades if t.symbol), None)
        lot_problem = await _lot_problem(sym, quantity) if sym else None
        if lot_problem:
            raise HTTPException(status_code=422, detail=lot_problem)
        if quantity > current:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"quantity can't be raised by a modify ({current} -> {quantity}) — "
                    "increase size with a new order so the risk engine checks it"
                ),
            )

    try:
        ok, message = await backend.modify_order(
            broker_order_id,
            quantity=quantity,
            limit_price=limit_price,
            stop_price=stop_price,
            order_type=order_type,
        )
    except FyersBlockedError as e:
        log.warning(
            "manual_order.modify_blocked",
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
        log.warning("manual_order.modify_failed", broker_order_id=broker_order_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"broker modify failed: {e}")

    rows_updated = 0
    if ok:
        new_type = order_type.value if order_type is not None else None
        for trade in trades:
            before = {
                "broker_order_id": broker_order_id,
                "quantity": trade.quantity,
                "price": trade.price,
                "order_type": trade.order_type,
            }
            if quantity is not None:
                trade.quantity = quantity
            if new_type is not None:
                trade.order_type = new_type
            # `price` mirrors the place path (limit, else the SL-M
            # trigger, 0 for MARKET); a fill later overwrites it.
            eff_type = (new_type or trade.order_type or "").upper()
            if limit_price is not None and eff_type not in ("SL-M", "MARKET"):
                trade.price = limit_price
            elif stop_price is not None and eff_type == "SL-M":
                trade.price = stop_price
            elif new_type == "MARKET":
                trade.price = 0.0
            db.add(AuditLog(
                actor="ui_trade_page",
                action="order.manual_modified",
                target=f"account:{account_id}",
                before=before,
                after={
                    "broker_order_id": broker_order_id,
                    "symbol": trade.symbol,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "order_type": trade.order_type,
                    "limit_price": limit_price,
                    "stop_price": stop_price,
                    "broker_message": message,
                },
            ))
        if not trades:
            # An order placed outside the bot (Fyers app / web), or one whose
            # local rows are already settled: nothing local to update, but the
            # real-money action is still audited.
            db.add(AuditLog(
                actor="ui_trade_page",
                action="order.manual_modified",
                target=f"account:{account_id}",
                before=None,
                after={
                    "broker_order_id": broker_order_id,
                    "quantity": quantity,
                    "limit_price": limit_price,
                    "stop_price": stop_price,
                    "order_type": new_type,
                    "broker_message": message,
                },
            ))
        await _commit_off_loop(db)
        rows_updated = len(trades)
    return {
        "ok": bool(ok),
        "broker_order_id": broker_order_id,
        "message": message,
        "rows_updated": rows_updated,
    }


_IST = timezone(timedelta(hours=5, minutes=30))
# Session close (IST) after which a DAY order can no longer be working.
_CLOSE_IST = {"MCX": (23, 30), "CDS": (17, 0), "BCD": (17, 0)}
_CLOSE_DEFAULT = (15, 30)  # NSE / BSE cash and F&O


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO string with a `Z`: SQLite hands back naive datetimes, and a
    naive string is read as browser-local time (5h30m off in India)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _expired_day_order(trade: Trade, now: datetime) -> bool:
    """True when `trade` was placed before the latest session close at or
    before `now` — a DAY order that the exchange has already expired."""
    created = trade.created_at
    if created is None:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    exch = (trade.symbol or "").split(":", 1)[0].upper() if ":" in (trade.symbol or "") else ""
    hh, mm = _CLOSE_IST.get(exch, _CLOSE_DEFAULT)
    local = now.astimezone(_IST)
    close = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if close > local:
        close -= timedelta(days=1)
    return created < close


@router.get("/pending")
def list_pending_orders(
    account_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List orders that are PENDING on the broker (not yet filled).

    Source: the local `trades` table where `status='placed'`. The
    Trade page's pending-orders panel polls this every few seconds.

    Every order goes out as a DAY order, so one placed before the last
    session close can't still be working; if no expiry update reached us
    (server down at the close, socket gap) the row would sit here forever
    with a Cancel button. Such rows are settled as "cancelled" (the same
    status the reconciler gives a broker EXPIRED) and left out.
    """
    q = select(Trade).where(Trade.status == "placed").order_by(Trade.created_at.desc())
    if account_id is not None:
        q = q.where(Trade.broker_account_id == account_id)
    now = datetime.now(timezone.utc)
    rows = []
    expired = 0
    for r in db.execute(q).scalars():
        if _expired_day_order(r, now):
            r.status = "cancelled"
            expired += 1
            db.add(AuditLog(
                actor="system",
                action="order.expired_eod",
                target=f"trade:{r.id}",
                before={"status": "placed"},
                after={"status": "cancelled", "broker_order_id": r.broker_order_id, "symbol": r.symbol},
            ))
            continue
        if len(rows) < limit:
            rows.append(r)
    if expired:
        db.commit()
        log.info("manual_order.expired_stale_rows", count=expired)
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
                "product": r.product or "INTRADAY",
                "status": r.status,
                "filled_qty": int(r.filled_qty or 0),
                "created_at": _iso_utc(r.created_at),
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
    # WebSocket-first: tell the realtime feed this symbol is being viewed so
    # it subscribes (and keeps it subscribed while the page polls). After the
    # first tick lands, the fresh-bus branch below serves it and we stop
    # hitting REST /data/quotes for the Trade page. Non-blocking.
    stream = _fyers_stream()
    if stream is not None:
        try:
            stream.touch_interest(sym)
        except Exception:  # noqa: BLE001
            pass
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
