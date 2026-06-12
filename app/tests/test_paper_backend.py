"""Tests for the in-process paper-trading simulator (PaperBackend).

Coverage:

  - MARKET order fills at the last cached quote.
  - LIMIT order fills when the next tick crosses the limit price.
  - STOP_LOSS / SL-M orders trigger on the right price side.
  - get_positions() marks-to-market with the latest quote.
  - cancel_order() prevents the pending order from filling.
  - Order without a known quote stays PENDING then EXPIRES.
  - SELL with no position opens a short.
  - Trade rows are persisted to the DB (status / side / symbol).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Position as PositionRow, Signal, Trade as TradeRow
from app.execution.base import (
    OrderSide,
    OrderState,
    OrderType,
)
from app.execution.market_data import MarketDataBus
from app.execution.paper import PaperBackend


# -- Helpers --------------------------------------------------------------


def _persist_signal(db_session, *, symbol: str, action: str) -> Signal:
    s = Signal(symbol=symbol, action=action, status="pending", confidence=0.9)
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


# -- Tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_market_order_fills_at_last_quote(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.00)
    sig = _persist_signal(db_session, symbol="RELIANCE", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session)
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
        )
        assert result.state == OrderState.FILLED
        assert result.filled_quantity == 10
        assert result.average_price == 2500.00
        # The position should reflect the buy.
        positions = await backend.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "RELIANCE"
        assert positions[0].quantity == 10
        assert positions[0].average_price == 2500.00
        # DB row written and updated to filled.
        rows = db_session.execute(select(TradeRow)).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "filled"
        assert rows[0].broker_order_id == result.broker_order_id
        # Position mirrored to DB.
        pos = db_session.execute(select(PositionRow)).scalar_one()
        assert pos.symbol == "RELIANCE"
        assert pos.quantity == 10
        assert pos.average_price == 2500.00
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_limit_buy_fills_when_tick_crosses_below_limit(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("TCS", last_price=4000.0)
    sig = _persist_signal(db_session, symbol="TCS", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session,
                            pending_timeout_s=5.0)
    backend.start()
    try:
        # BUY LIMIT @ 3950 — fills when price drops to 3950 or below.
        result = await backend.place_order(
            signal=sig,
            symbol="TCS",
            side=OrderSide.BUY,
            quantity=5,
            order_type=OrderType.LIMIT,
            limit_price=3950.0,
        )
        assert result.state == OrderState.PENDING
        # Push a tick that crosses the limit.
        await md.tick("TCS", 3940.0)
        # Give the consumer a moment to drain.
        for _ in range(20):
            statuses = await backend.get_order_status(result.broker_order_id)
            if statuses.state == OrderState.FILLED:
                break
            await asyncio.sleep(0.05)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.FILLED
        # Filled at the limit price.
        assert status.average_price == 3950.0
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_limit_sell_fills_when_tick_crosses_above_limit(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("INFY", last_price=1500.0)
    sig = _persist_signal(db_session, symbol="INFY", action="SELL")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session,
                            pending_timeout_s=5.0)
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="INFY",
            side=OrderSide.SELL,
            quantity=3,
            order_type=OrderType.LIMIT,
            limit_price=1520.0,
        )
        assert result.state == OrderState.PENDING
        await md.tick("INFY", 1530.0)
        for _ in range(20):
            status = await backend.get_order_status(result.broker_order_id)
            if status.state == OrderState.FILLED:
                break
            await asyncio.sleep(0.05)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.FILLED
        assert status.average_price == 1520.0
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_stop_loss_triggers_on_correct_side(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("HDFCBANK", last_price=1600.0)
    sig = _persist_signal(db_session, symbol="HDFCBANK", action="SELL")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session,
                            pending_timeout_s=5.0)
    backend.start()
    try:
        # SELL stop @ 1500 — triggers when last_price <= 1500.
        result = await backend.place_order(
            signal=sig,
            symbol="HDFCBANK",
            side=OrderSide.SELL,
            quantity=4,
            order_type=OrderType.STOP_LOSS_MARKET,
            stop_price=1500.0,
        )
        assert result.state == OrderState.PENDING
        # Tick that does NOT cross: still PENDING.
        await md.tick("HDFCBANK", 1550.0)
        await asyncio.sleep(0.1)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.PENDING
        # Tick that crosses: fills at market.
        await md.tick("HDFCBANK", 1490.0)
        for _ in range(20):
            status = await backend.get_order_status(result.broker_order_id)
            if status.state == OrderState.FILLED:
                break
            await asyncio.sleep(0.05)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.FILLED
        # Filled at the triggering tick's price.
        assert status.average_price == 1490.0
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_get_positions_marks_to_market(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    sig = _persist_signal(db_session, symbol="RELIANCE", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session)
    backend.start()
    try:
        await backend.place_order(
            signal=sig,
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=10,
            order_type=OrderType.MARKET,
        )
        # Tick up 100.
        await md.tick("RELIANCE", 2600.0)
        positions = await backend.get_positions()
        assert len(positions) == 1
        assert positions[0].last_price == 2600.0
        # P&L = (2600 - 2500) * 10 = 1000.
        assert positions[0].unrealized_pnl == 1000.0
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_cancel_order_prevents_fill(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("WIPRO", last_price=500.0)
    sig = _persist_signal(db_session, symbol="WIPRO", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session,
                            pending_timeout_s=5.0)
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="WIPRO",
            side=OrderSide.BUY,
            quantity=2,
            order_type=OrderType.LIMIT,
            limit_price=490.0,
        )
        assert result.state == OrderState.PENDING
        ok = await backend.cancel_order(result.broker_order_id)
        assert ok is True
        # Now tick across; should not fill (it's been cancelled).
        await md.tick("WIPRO", 480.0)
        await asyncio.sleep(0.1)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.CANCELLED
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_market_order_with_no_quote_expires(db_session, isolated_db):
    md = MarketDataBus()
    sig = _persist_signal(db_session, symbol="UNKNOWN", action="BUY")
    backend = PaperBackend(
        market_data=md, session_factory=lambda: db_session,
        pending_timeout_s=0.5,
        fill_on_market_immediate=False,
    )
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="UNKNOWN",
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.MARKET,
        )
        assert result.state == OrderState.PENDING
        # Wait for the timeout.
        await asyncio.sleep(1.0)
        status = await backend.get_order_status(result.broker_order_id)
        assert status.state == OrderState.EXPIRED
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_sell_with_no_position_opens_short(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("TCS", last_price=3500.0)
    sig = _persist_signal(db_session, symbol="TCS", action="SELL")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session)
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="TCS",
            side=OrderSide.SELL,
            quantity=2,
            order_type=OrderType.MARKET,
        )
        assert result.state == OrderState.FILLED
        positions = await backend.get_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "TCS"
        assert positions[0].quantity == -2  # short
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_place_order_rejects_zero_qty(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    sig = _persist_signal(db_session, symbol="RELIANCE", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session)
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=0,
            order_type=OrderType.MARKET,
        )
        assert result.state == OrderState.REJECTED
    finally:
        await backend.stop()


@pytest.mark.asyncio
async def test_trade_persisted_with_correct_side_symbol_status(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("AXISBANK", last_price=1100.0)
    sig = _persist_signal(db_session, symbol="AXISBANK", action="BUY")
    backend = PaperBackend(market_data=md, session_factory=lambda: db_session)
    backend.start()
    try:
        result = await backend.place_order(
            signal=sig,
            symbol="AXISBANK",
            side=OrderSide.BUY,
            quantity=8,
            order_type=OrderType.MARKET,
        )
        # After fill, the trade row should reflect the fill.
        row = db_session.execute(
            select(TradeRow).where(TradeRow.broker_order_id == result.broker_order_id)
        ).scalar_one()
        assert row.symbol == "AXISBANK"
        assert row.side == "BUY"
        assert row.status == "filled"
        assert row.quantity == 8
    finally:
        await backend.stop()
