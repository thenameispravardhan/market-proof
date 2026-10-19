"""Practice trading from the Trade page (the Paper / Fyers toggle).

A virtual account, `PRACTICE_ACCOUNT_ID` (-1): orders on it go to a paper
backend that fills at the REAL Fyers price, and everything it writes (trades,
positions) lives in its own SQLite file with the same schema. So a practice
position can never touch the live book (positions are unique per symbol),
the bot never manages it, and the existing endpoints serve it unchanged by
swapping the session (`?practice=1` / account_id -1).
"""
from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from typing import Any, Optional

from fastapi import HTTPException, Query
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import session as db_session
from app.db.session import Base, _ensure_sqlite_dir

PRACTICE_ACCOUNT_ID = -1
_backend: Any = None
_watcher: Any = None
# Ticket SL / target waiting for a resting practice order to fill: oid -> (symbol, sl, tp).
PENDING_LEVELS: dict[str, tuple[str, Optional[float], Optional[float]]] = {}


@lru_cache(maxsize=1)
def session_factory() -> sessionmaker:
    import app.db.models  # noqa: F401 — register every table on Base

    url = get_settings().effective_database_url()
    if ":memory:" in url or not url.startswith("sqlite:///"):
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    else:
        url = url.rsplit("/", 1)[0] + "/practice.db"
        _ensure_sqlite_dir(url)
        eng = create_engine(url, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False, class_=Session)


def get_db_for(practice: bool = Query(False)) -> Iterator[Session]:
    """`get_db`, or the practice DB when `?practice=1`."""
    db = (session_factory() if practice else db_session.SessionLocal)()
    try:
        yield db
    finally:
        db.close()


def backend() -> Any:
    global _backend
    if _backend is None:
        from app.api.orders import _manager
        from app.execution.paper import PaperBackend

        # ponytail: resting practice orders live in memory (a restart drops
        # them; the pending list then expires their rows at the close)
        # Fills at the touch (BUY at ask, SELL at bid), like a real market order.
        _backend = PaperBackend(market_data=_manager().market_data, session_factory=session_factory(),
                                pending_timeout_s=8 * 3600, fill_at_touch=True)
        _backend.start()
    global _watcher
    if _watcher is None or _watcher.done():
        import asyncio

        _watcher = asyncio.get_running_loop().create_task(_watch_levels(), name="practice-levels")
    return _backend


def live_symbols() -> list[str]:
    """Open practice positions + resting practice orders: kept on the Fyers
    socket so their P&L, SL / target and limit fills run on live ticks even
    with no browser open."""
    from app.db.models import Position

    with session_factory()() as db:
        out = [s for (s,) in db.query(Position.symbol).filter(Position.quantity != 0)]
    if _backend is not None:
        out += [o.symbol for o in _backend.get_pending().values()]
    return [s.upper() for s in out if s and ":" in s]


async def place(*, symbol: str, side: str, quantity: int, order_type: str,
                limit_price: Optional[float], stop_price: Optional[float],
                product_type: str, stop_loss: Optional[float] = None,
                target: Optional[float] = None) -> dict[str, Any]:
    from app.execution.base import OrderSide, OrderState, OrderType, ProductType

    q = await _quote(symbol)
    if not q.get("ok"):
        raise HTTPException(status_code=422, detail=(
            f"No live price for {symbol}: practice orders fill at the real Fyers price, "
            "so Fyers must be connected."))
    r = await backend().place_order(
        signal=None, symbol=symbol, side=OrderSide(side), quantity=int(quantity),
        order_type=OrderType(order_type), limit_price=limit_price, stop_price=stop_price,
        product_type=ProductType(product_type),
    )
    if stop_loss is not None or target is not None:
        if r.state == OrderState.FILLED:
            _write_levels(symbol, stop_loss, target)
        elif r.state == OrderState.PENDING:
            PENDING_LEVELS[r.broker_order_id] = (symbol, stop_loss, target)
    return {"ok": r.state != OrderState.REJECTED, "practice": True, "blocked": False,
            "risk_codes": [], "risk_message": "", "broker_order_id": r.broker_order_id,
            "status": r.state.value, "error": r.error}


async def modify(oid: str, body: dict[str, Any]) -> dict[str, Any]:
    """Move a resting practice order (limit / trigger price) and/or the SL /
    target it arms on fill — the chart's drag-to-modify. Keys absent from
    `body` are left as they are; a null SL / target clears it."""
    from app.db.models import Trade
    from app.execution.base import OrderType

    o = backend()._pending.get(oid)  # noqa: SLF001 — mutate the live resting order in place
    if o is None:
        raise HTTPException(status_code=404, detail="That practice order already filled or was cancelled.")
    px = {k: (float(body[k]) if body.get(k) is not None else None) for k in ("limit_price", "stop_price")}
    for k, v in px.items():
        if v is not None and v <= 0:
            raise HTTPException(status_code=422, detail=f"{k} must be a positive price")
    if px["limit_price"] is not None:
        o.limit_price = px["limit_price"]
    if px["stop_price"] is not None:
        o.stop_price = px["stop_price"]
    if "stop_loss" in body or "target" in body:
        _, sl0, tp0 = PENDING_LEVELS.get(oid, (o.symbol, None, None))
        sl = body.get("stop_loss", sl0)
        tp = body.get("target", tp0)
        sl, tp = (float(sl) if sl is not None else None), (float(tp) if tp is not None else None)
        if sl is None and tp is None:
            PENDING_LEVELS.pop(oid, None)
        else:
            PENDING_LEVELS[oid] = (o.symbol, sl, tp)
    shown = o.stop_price if o.order_type in (OrderType.STOP_LOSS, OrderType.STOP_LOSS_MARKET) else o.limit_price
    with session_factory()() as db:
        for t in db.query(Trade).filter(Trade.broker_order_id == oid, Trade.status == "placed"):
            if shown is not None:
                t.price = shown
        db.commit()
    return {"ok": True, "practice": True, "broker_order_id": oid, "message": "modified", "rows_updated": 1}


def pending_levels(oid: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    _, sl, tp = PENDING_LEVELS.get(oid or "", (None, None, None))
    return sl, tp


async def close(symbol: Optional[str] = None) -> list[dict[str, Any]]:
    """Flatten one practice position (or all) at market."""
    from app.db.models import Position

    with session_factory()() as db:
        q = db.query(Position).filter(Position.quantity != 0)
        rows = [(p.symbol, int(p.quantity), p.product or "INTRADAY") for p in (q.filter(Position.symbol == symbol) if symbol else q)]
    return [await place(symbol=s, side="SELL" if n > 0 else "BUY", quantity=abs(n), order_type="MARKET",
                        limit_price=None, stop_price=None, product_type=prod) for s, n, prod in rows]


async def _quote(symbol: str) -> dict[str, Any]:
    """The real price, through the connected Fyers account (bus, else REST)."""
    from app.api.orders import get_quote

    with db_session.SessionLocal() as db:
        return await get_quote(symbol=symbol, db=db)


def _write_levels(symbol: str, sl: Optional[float], tp: Optional[float]) -> Optional[dict[str, Any]]:
    from app.db.models import Position

    with session_factory()() as db:
        pos = db.query(Position).filter(Position.symbol == symbol, Position.quantity != 0).one_or_none()
        if pos is None:
            return None
        pos.stop_loss, pos.target = sl, tp
        db.commit()
        return {"symbol": symbol, "quantity": pos.quantity, "entry": pos.average_price,
                "stop_loss": sl, "target": tp}


async def set_levels(symbol: str, sl: Optional[float], tp: Optional[float]) -> dict[str, Any]:
    """Edit a practice position's SL / target; same wrong-side refusal as live."""
    from app.db.models import Position

    with session_factory()() as db:
        pos = db.query(Position).filter(Position.symbol == symbol, Position.quantity != 0).one_or_none()
        qty = int(pos.quantity) if pos else 0
    if not qty:
        raise HTTPException(status_code=404, detail=f"no practice position for {symbol!r}")
    q = await _quote(symbol)
    last, long = (float(q["last_price"]) if q.get("ok") else None), qty > 0
    for name, v, below in (("Stop-loss", sl, long), ("Target", tp, not long)):
        if v is None:
            continue
        if v <= 0:
            raise HTTPException(status_code=422, detail=f"{name} must be a positive price")
        if last is not None and (v >= last if below else v <= last):
            raise HTTPException(status_code=422, detail=f"{name} {v:g} is already hit (price {last:g})")
    return _write_levels(symbol, sl, tp)  # type: ignore[return-value]


async def _watch_levels() -> None:
    import asyncio
    import logging

    while True:
        await asyncio.sleep(1.0)
        try:
            await _check_levels()
        except Exception:  # noqa: BLE001 — the watcher must outlive one bad cycle
            logging.getLogger(__name__).exception("practice.levels_check_failed")


async def _check_levels() -> None:
    """Fire practice SL / target at market (levels live on the practice
    position row, so they survive a restart)."""
    from app.db.models import Position, Trade

    with session_factory()() as db:
        for oid, (sym, sl, tp) in list(PENDING_LEVELS.items()):
            st = db.query(Trade.status).filter(Trade.broker_order_id == oid).scalar()
            if st != "placed":
                PENDING_LEVELS.pop(oid, None)
                if st == "filled":
                    _write_levels(sym, sl, tp)
        # A flat row keeps no levels: a later position must not inherit them.
        db.query(Position).filter(Position.quantity == 0, (Position.stop_loss.is_not(None)) | (Position.target.is_not(None)))             .update({"stop_loss": None, "target": None}, synchronize_session=False)
        db.commit()
        armed = [(p.symbol, int(p.quantity), p.stop_loss, p.target) for p in db.query(Position).filter(
            Position.quantity != 0, (Position.stop_loss.is_not(None)) | (Position.target.is_not(None)))]
    from app.api.orders import _fyers_stream, _manager

    for sym, qty, sl, tp in armed:
        stream = _fyers_stream()
        if stream is not None:
            stream.touch_interest(sym)       # keep it on the websocket
        q = await _manager().market_data.get_quote(sym)
        if q is None or (q.extra or {}).get("simulated"):
            continue
        last, long = float(q.last_price), qty > 0
        if (sl is not None and (last <= sl if long else last >= sl)) or (
                tp is not None and (last >= tp if long else last <= tp)):
            _write_levels(sym, None, None)  # disarm first: one exit, never two
            await close(sym)
