"""Trailing stop, scale-out, EOD square-off, and R-multiple logging."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.models import Position as PositionRow, Trade as TradeRow
from app.execution.market_data import MarketDataBus
from app.execution.trade_manager import ManagedPosition, TradeManager
from app.risk.market_clock import IST


# -- ManagedPosition trailing math (pure) --------------------------------


def test_trailing_arms_and_ratchets_long():
    mp = ManagedPosition(
        symbol="X", quantity=10, entry=100.0, stop_loss=95.0, target=None,
        initial_risk=5.0, scale_out_enabled=True,
        trail_activate_r=1.5, trail_distance_r=0.5,
    )
    # Below +1.5R (price 107.5) the trail stays disarmed.
    mp.apply_trailing(105.0)
    assert mp.trail_active is False
    assert mp.stop_loss == 95.0
    # At +2R (110) the trail arms and the stop ratchets to peak - 0.5R.
    mp.apply_trailing(110.0)
    assert mp.trail_active is True
    assert mp.stop_loss == pytest.approx(110.0 - 0.5 * 5.0)  # 107.5
    # Pullback doesn't loosen the stop.
    mp.apply_trailing(108.0)
    assert mp.stop_loss == pytest.approx(107.5)


def test_trailing_ratchets_short():
    mp = ManagedPosition(
        symbol="X", quantity=-10, entry=100.0, stop_loss=105.0, target=None,
        initial_risk=5.0, scale_out_enabled=True,
        trail_activate_r=1.5, trail_distance_r=0.5,
    )
    mp.apply_trailing(90.0)  # +2R in favour of the short
    assert mp.trail_active is True
    assert mp.stop_loss == pytest.approx(90.0 + 0.5 * 5.0)  # 92.5


def test_scale_out_in_mode_ignores_hard_target():
    mp = ManagedPosition(
        symbol="X", quantity=10, entry=100.0, stop_loss=95.0,
        target=110.0, scale_out_enabled=True,
    )
    # Target is ignored in scale-out mode; only the stop triggers.
    assert mp.exit_reason(120.0) is None
    assert mp.exit_reason(94.0) == "STOP"


# -- TradeManager scale-out ----------------------------------------------


@pytest.mark.asyncio
async def test_scale_out_takes_half_and_trails(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    # entry 100, stop 95 => R=5. scale_out_r default 2 => take half at 110.
    await tm.register(symbol="RELIANCE", quantity=10, entry=100.0,
                      stop_loss=95.0, target=None)
    # Mirror a DB position so the partial-settle can reduce it.
    db_session.add(PositionRow(symbol="RELIANCE", quantity=10,
                               average_price=100.0, last_price=100.0))
    db_session.commit()
    await md.publish("RELIANCE", 111.0)  # +2.2R
    await tm._sweep()
    # Half (5) closed at a profit; 5 still managed with a breakeven-ish
    # (trailed) stop.
    booked = tm.managed_positions()
    assert len(booked) == 1
    assert abs(booked[0].quantity) == 5
    assert booked[0].scaled_out is True
    partials = db_session.query(TradeRow).filter_by(symbol="RELIANCE").all()
    assert len(partials) == 1
    assert partials[0].quantity == 5
    assert partials[0].pnl == pytest.approx((111.0 - 100.0) * 5)
    assert partials[0].r_multiple is not None


@pytest.mark.asyncio
async def test_exit_records_r_multiple(db_session, isolated_db, monkeypatch):
    monkeypatch.setenv("SCALE_OUT_ENABLED", "0")
    from app.config import get_settings
    get_settings.cache_clear()
    try:
        md = MarketDataBus()
        tm = TradeManager(market_data=md)
        # R=5; exit at target 110 => +2R.
        await tm.register(symbol="TCS", quantity=10, entry=100.0,
                          stop_loss=95.0, target=110.0)
        await md.publish("TCS", 110.0)
        await tm._sweep()
        trade = db_session.query(TradeRow).filter_by(symbol="TCS").one()
        assert trade.r_multiple == pytest.approx(2.0)
    finally:
        get_settings.cache_clear()


# -- EOD square-off ------------------------------------------------------


@pytest.mark.asyncio
async def test_square_off_if_due_flattens(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(symbol="INFY", quantity=10, entry=1000.0,
                      stop_loss=950.0, target=None)
    db_session.add(PositionRow(symbol="INFY", quantity=10,
                               average_price=1000.0, last_price=1000.0))
    db_session.commit()
    await md.publish("INFY", 1010.0)
    # 15:20 IST on a Friday is past the 15:10 square-off.
    now = datetime(2026, 6, 19, 15, 20, tzinfo=IST).astimezone(timezone.utc)
    closed = await tm.square_off_if_due(now=now, force=True)
    assert any(r["symbol"] == "INFY" for r in closed)
    assert tm.managed_positions() == []


@pytest.mark.asyncio
async def test_square_off_not_due_midsession(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(symbol="INFY", quantity=10, entry=1000.0,
                      stop_loss=950.0, target=None)
    await md.publish("INFY", 1010.0)
    now = datetime(2026, 6, 19, 11, 0, tzinfo=IST).astimezone(timezone.utc)
    closed = await tm.square_off_if_due(now=now, force=True)
    assert closed == []
    assert len(tm.managed_positions()) == 1
