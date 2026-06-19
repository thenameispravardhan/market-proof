"""IOC marketable-limit auto entry + pre-entry circuit-breaker gate."""
from __future__ import annotations

import pytest

from app.db.models import BrokerAccount, Signal, Strategy, Trade as TradeRow
from app.execution.base import OrderType
from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.risk import circuit_breakers as cb
from app.risk.engine import RiskEngine


def _setup(db_session, *, action="BUY", confidence=0.9):
    acct = BrokerAccount(name="paper", broker="fyers", paper_mode=True, enabled=True)
    db_session.add(acct)
    db_session.commit()
    db_session.refresh(acct)
    strat = Strategy(name="s", enabled=True, config={"broker_account_id": acct.id})
    db_session.add(strat)
    db_session.commit()
    db_session.refresh(strat)
    rationale = (
        "entry=2500.00 sl=2350.00 target=2950.00 rr=3.00" if action == "BUY"
        else "entry=2500.00 sl=2600.00 target=2200.00 rr=3.00"
    )
    sig = Signal(symbol="RELIANCE", action=action, confidence=confidence,
                 status="pending", strategy_id=strat.id, rationale=rationale)
    db_session.add(sig)
    db_session.commit()
    db_session.refresh(sig)
    return acct, strat, sig


@pytest.mark.asyncio
async def test_auto_entry_uses_ioc_marketable_limit(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0, average_daily_volume_crore=50.0)
    _setup(db_session)
    mgr = Manager(risk_engine=RiskEngine(market_data=md, portfolio_value=10_000_000.0), market_data=md)
    out = await mgr.process_signal(
        db_session.query(Signal).one().id
    )
    assert out["approved"] is True
    # A filled trade was written as a LIMIT order with slippage recorded.
    trade = (
        db_session.query(TradeRow)
        .filter(TradeRow.symbol == "RELIANCE", TradeRow.status == "filled")
        .first()
    )
    assert trade is not None
    assert trade.order_type == OrderType.LIMIT.value
    # Paper fills the marketable limit at the quote => ~zero slippage.
    assert trade.slippage_pct is not None
    assert trade.slippage_pct == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_negative_news_opens_intraday_short(db_session, isolated_db):
    """Full execution path for a SELL: the paper backend opens a SHORT
    (negative-qty) intraday position via the IOC marketable limit."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0, average_daily_volume_crore=50.0)
    _setup(db_session, action="SELL")
    mgr = Manager(risk_engine=RiskEngine(market_data=md, portfolio_value=10_000_000.0), market_data=md)
    out = await mgr.process_signal(db_session.query(Signal).one().id)
    assert out["approved"] is True
    # Filled SELL trade.
    trade = (
        db_session.query(TradeRow)
        .filter(TradeRow.symbol == "RELIANCE", TradeRow.status == "filled")
        .first()
    )
    assert trade is not None and trade.side == "SELL"
    # The paper backend mirrors a SHORT position (negative quantity).
    from app.db.models import Position as PositionRow
    pos = db_session.query(PositionRow).filter_by(symbol="RELIANCE").one()
    assert pos.quantity < 0


@pytest.mark.asyncio
async def test_kill_switch_blocks_auto_entry(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0, average_daily_volume_crore=50.0)
    _setup(db_session)
    cb.trip_manual_kill(db_session)  # engage the kill switch
    mgr = Manager(risk_engine=RiskEngine(market_data=md, portfolio_value=10_000_000.0), market_data=md)
    out = await mgr.process_signal(db_session.query(Signal).one().id)
    assert out["approved"] is False
    assert out["code"].startswith("HALT_")
    # No filled trade.
    assert (
        db_session.query(TradeRow)
        .filter(TradeRow.symbol == "RELIANCE", TradeRow.status == "filled")
        .count()
        == 0
    )
