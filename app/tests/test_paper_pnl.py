"""Paper fills: position averages and realised P&L for every direction.

The paper book used to re-average only BUYs, so covering a short moved its
cost (and a full cover divided by zero), adding to a short or flipping a
long kept the old cost, and realised P&L was booked only when a long went
flat. It also started empty after a restart, so a SELL against a position
the DB still held opened a phantom short.
"""
from __future__ import annotations

import pytest

from app.db.models import Position as PositionRow, Trade as TradeRow
from app.execution.base import OrderSide, OrderState, OrderType
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend


async def _trade(backend, md, side, qty, price, sym="TCS"):
    md.set_quote_sync(sym, last_price=price)
    r = await backend.place_order(signal=None, symbol=sym, side=side, quantity=qty, order_type=OrderType.MARKET)
    assert r.state == OrderState.FILLED
    return r


def _pos(db_session, sym="TCS"):
    db_session.expire_all()
    return db_session.query(PositionRow).filter_by(symbol=sym).one()


def _pnl(db_session, r):
    db_session.expire_all()
    return db_session.query(TradeRow).filter_by(broker_order_id=r.broker_order_id).one().pnl


@pytest.mark.asyncio
async def test_cover_short_in_parts(db_session, isolated_db):
    md = MarketDataBus()
    b = PaperBackend(market_data=md, session_factory=lambda: db_session)
    await _trade(b, md, OrderSide.SELL, 10, 100.0)
    await _trade(b, md, OrderSide.SELL, 10, 110.0)      # add to the short
    p = _pos(db_session)
    assert p.quantity == -20 and p.average_price == pytest.approx(105.0)
    r = await _trade(b, md, OrderSide.BUY, 5, 95.0)      # partial cover
    p = _pos(db_session)
    assert p.quantity == -15 and p.average_price == pytest.approx(105.0)
    assert _pnl(db_session, r) == pytest.approx(50.0)
    r = await _trade(b, md, OrderSide.BUY, 15, 100.0)    # full cover: no ZeroDivisionError
    p = _pos(db_session)
    assert p.quantity == 0
    assert _pnl(db_session, r) == pytest.approx(75.0)


@pytest.mark.asyncio
async def test_long_partial_exit_and_flip(db_session, isolated_db):
    md = MarketDataBus()
    b = PaperBackend(market_data=md, session_factory=lambda: db_session)
    await _trade(b, md, OrderSide.BUY, 10, 100.0)
    r = await _trade(b, md, OrderSide.SELL, 4, 110.0)    # partial exit books P&L
    assert _pnl(db_session, r) == pytest.approx(40.0)
    p = _pos(db_session)
    assert p.quantity == 6 and p.average_price == pytest.approx(100.0)
    assert p.unrealized_pnl == pytest.approx(60.0)
    r = await _trade(b, md, OrderSide.SELL, 10, 120.0)   # flip: close 6, open short 4 @120
    assert _pnl(db_session, r) == pytest.approx(120.0)
    p = _pos(db_session)
    assert p.quantity == -4 and p.average_price == pytest.approx(120.0)


@pytest.mark.asyncio
async def test_restart_reads_the_db_book(db_session, isolated_db):
    db_session.add(PositionRow(symbol="TCS", quantity=10, average_price=100.0, last_price=100.0, product="DELIVERY"))
    db_session.commit()
    md = MarketDataBus()
    b = PaperBackend(market_data=md, session_factory=lambda: db_session)   # fresh: empty memory
    r = await _trade(b, md, OrderSide.SELL, 10, 105.0)
    p = _pos(db_session)
    assert p.quantity == 0              # not a phantom -10 short
    assert p.product == "DELIVERY"
    assert _pnl(db_session, r) == pytest.approx(50.0)


def test_positions_api_carries_the_lot_size(client, db_session, isolated_db, monkeypatch):
    from app.services import instrument_master as im

    class _Inst:
        lot_size = 30

    monkeypatch.setattr(im, "get_master", lambda: type("M", (), {"get": staticmethod(lambda s: _Inst() if s == "NSE:BANKNIFTY25OCT56000CE" else None)})())
    db_session.add_all([
        PositionRow(symbol="NSE:BANKNIFTY25OCT56000CE", quantity=60, average_price=200.0),
        PositionRow(symbol="NSE:SBIN-EQ", quantity=5, average_price=800.0),
    ])
    db_session.commit()
    rows = {r["symbol"]: r for r in client.get("/api/positions").json()}
    assert rows["NSE:BANKNIFTY25OCT56000CE"]["lot_size"] == 30
    assert rows["NSE:SBIN-EQ"]["lot_size"] == 1
