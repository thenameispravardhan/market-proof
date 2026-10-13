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
        _backend = PaperBackend(market_data=_manager().market_data,
                                session_factory=session_factory(), pending_timeout_s=8 * 3600)
        _backend.start()
    return _backend


async def place(*, symbol: str, side: str, quantity: int, order_type: str,
                limit_price: Optional[float], stop_price: Optional[float],
                product_type: str) -> dict[str, Any]:
    from app.api.orders import get_quote
    from app.execution.base import OrderSide, OrderState, OrderType, ProductType

    with db_session.SessionLocal() as db:  # the quote comes through the real Fyers account
        q = await get_quote(symbol=symbol, db=db)
    if not q.get("ok"):
        raise HTTPException(status_code=422, detail=(
            f"No live price for {symbol}: practice orders fill at the real Fyers price, "
            "so Fyers must be connected."))
    r = await backend().place_order(
        signal=None, symbol=symbol, side=OrderSide(side), quantity=int(quantity),
        order_type=OrderType(order_type), limit_price=limit_price, stop_price=stop_price,
        product_type=ProductType(product_type),
    )
    return {"ok": r.state != OrderState.REJECTED, "practice": True, "blocked": False,
            "risk_codes": [], "risk_message": "", "broker_order_id": r.broker_order_id,
            "status": r.state.value, "error": r.error}


async def close(symbol: Optional[str] = None) -> list[dict[str, Any]]:
    """Flatten one practice position (or all) at market."""
    from app.db.models import Position

    with session_factory()() as db:
        q = db.query(Position).filter(Position.quantity != 0)
        rows = [(p.symbol, int(p.quantity)) for p in (q.filter(Position.symbol == symbol) if symbol else q)]
    return [await place(symbol=s, side="SELL" if n > 0 else "BUY", quantity=abs(n), order_type="MARKET",
                        limit_price=None, stop_price=None, product_type="INTRADAY") for s, n in rows]
