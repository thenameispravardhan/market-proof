"""Position management endpoints — manual close + square-off-all.

Live read-only position data is served by `core.py` (`GET /api/positions`).
These endpoints drive the *actions* on the TradeManager that the
dashboard's position controls call:

    POST /api/positions/{symbol}/close   -> close one position at market
    POST /api/positions/close-all        -> square off everything
    GET  /api/positions/managed          -> the live managed book
                                            (entry/SL/target per symbol)

The TradeManager is created in the app lifespan and stashed on
`app.state.trade_manager`. In TESTING mode (no lifespan services) the
endpoints return a clear 503 rather than pretending to trade.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from app.db.models import AuditLog
from app.db.session import SessionLocal
from app.logging_config import get_logger

router = APIRouter(prefix="/api/positions", tags=["positions"])

log = get_logger(__name__)


class LevelsUpdate(BaseModel):
    """New stop-loss / target for an open position. Either may be null
    to clear (disarm) that exit level."""

    stop_loss: Optional[float] = None
    target: Optional[float] = None


def _trade_manager(request: Request) -> Any:
    tm = getattr(request.app.state, "trade_manager", None)
    if tm is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="trade manager is not running (TESTING mode or not started)",
        )
    return tm


@router.get("/managed")
def list_managed(request: Request) -> list[dict[str, Any]]:
    """The live managed book: every open position with its entry,
    stop-loss and target, plus the exit state (trailing / breakeven /
    time exit / failing exit) the TradeManager is acting on."""
    tm = _trade_manager(request)
    return [mp.view() for mp in tm.managed_positions()]


@router.post("/{symbol}/close")
async def close_position(symbol: str, request: Request) -> dict[str, Any]:
    """Close one managed position at the latest price."""
    tm = _trade_manager(request)
    result = await tm.close_position(symbol, reason="MANUAL")
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no managed position for symbol {symbol!r}",
        )
    _audit("positions.close", symbol, result)
    return {"ok": True, "closed": result}


@router.post("/{symbol}/levels")
async def update_levels(
    symbol: str, body: LevelsUpdate, request: Request
) -> dict[str, Any]:
    """Edit the stop-loss / target of an open position so the trade
    manager exits on the new levels. Pass null for a level to clear it."""
    tm = _trade_manager(request)
    problem = await tm.check_levels(
        symbol, stop_loss=body.stop_loss, target=body.target
    )
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    result = await tm.update_levels(
        symbol, stop_loss=body.stop_loss, target=body.target
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no open position for symbol {symbol!r}",
        )
    _audit("positions.update_levels", symbol, result)
    return {"ok": True, "managed": result}


class ConvertBody(BaseModel):
    to: str


@router.post("/{symbol}/convert")
async def convert_position(symbol: str, body: ConvertBody) -> dict[str, Any]:
    """Convert an open position's product at the broker (INTRADAY <-> DELIVERY
    for cash, INTRADAY <-> MARGIN for F&O) and record it, so the EOD
    square-off flattens it or carries it accordingly."""
    from app.api import market
    from app.api.orders import MANUAL_PRODUCTS, _PRODUCT_ALIASES
    from app.db import session as db_session
    from app.db.models import Position as PositionRow

    to = _PRODUCT_ALIASES.get(body.to.upper(), body.to.upper())
    if to not in MANUAL_PRODUCTS:
        raise HTTPException(status_code=422, detail=f"convert to one of {sorted(MANUAL_PRODUCTS)}")
    with db_session.SessionLocal() as db:
        pos = db.query(PositionRow).filter_by(symbol=symbol).one_or_none()
        if pos is None or not pos.quantity:
            raise HTTPException(status_code=404, detail=f"no open position for {symbol!r}")
        frm, qty = pos.product or "INTRADAY", pos.quantity
    if frm == to:
        return {"ok": True, "symbol": symbol, "product": to, "unchanged": True}
    backend = market._fyers_backend()  # noqa: SLF001
    if backend is None or not hasattr(backend, "convert_position"):
        raise HTTPException(status_code=503, detail="connect a Fyers account to convert positions")
    try:
        await backend.convert_position(symbol, qty, abs(qty), frm, to)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Fyers rejected the conversion: {e}")
    with db_session.SessionLocal() as db:
        db.query(PositionRow).filter_by(symbol=symbol).update({"product": to})
        db.commit()
    _audit("positions.convert", symbol, {"from": frm, "to": to, "qty": qty})
    return {"ok": True, "symbol": symbol, "product": to, "from": frm}


@router.post("/close-all")
async def close_all(request: Request) -> dict[str, Any]:
    """Square off every managed position."""
    tm = _trade_manager(request)
    results = await tm.close_all(reason="SQUARE_OFF")
    _audit("positions.close_all", "*", {"count": len(results)})
    return {"ok": True, "closed": results, "count": len(results)}


def _audit(action: str, target: str, after: dict[str, Any]) -> None:
    try:
        with SessionLocal() as s:
            s.add(AuditLog(actor="ui", action=action, target=target, after=after))
            s.commit()
    except Exception:  # noqa: BLE001
        log.exception("positions.audit_failed", action=action, target=target)
