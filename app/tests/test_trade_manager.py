"""Tests for the trade management layer (QuoteFeed + TradeManager).

Covers:
  - QuoteFeed seeds a price and random-walks watched symbols.
  - TradeManager exits a long on a target hit and on a stop hit, with
    the right realised P&L sign, and persists the exit + flattens the
    position.
  - Manual close settles at the latest price.
"""
from __future__ import annotations

import pytest

from app.db.models import Position as PositionRow, Trade as TradeRow
from app.execution.market_data import MarketDataBus
from app.execution.quote_feed import QuoteFeed, _base_price_for
from app.execution.trade_manager import ManagedPosition, TradeManager


# -- QuoteFeed -----------------------------------------------------------


@pytest.mark.asyncio
async def test_quote_feed_seed_publishes_price():
    md = MarketDataBus()
    qf = QuoteFeed(market_data=md, seed=1)
    price = await qf.seed_symbol("RELIANCE")
    assert price > 0
    q = await md.get_quote("RELIANCE")
    assert q is not None
    assert q.last_price == price
    # ADV is populated so the liquidity rule passes in paper mode.
    assert q.average_daily_volume_crore and q.average_daily_volume_crore > 0


def test_base_price_is_deterministic():
    assert _base_price_for("TCS") == _base_price_for("TCS")
    assert _base_price_for("TCS") != _base_price_for("INFY")


@pytest.mark.asyncio
async def test_quote_feed_walks_price():
    md = MarketDataBus()
    qf = QuoteFeed(market_data=md, seed=42, volatility=0.05)
    start = qf.watch("ABC", anchor=100.0)
    assert start == 100.0
    await qf._tick_all()
    q = await md.get_quote("ABC")
    assert q is not None
    # The price moved (vol is non-zero and the seed is fixed).
    assert q.last_price != 100.0


# -- ManagedPosition logic ----------------------------------------------


def test_managed_position_exit_reasons_long():
    mp = ManagedPosition(symbol="X", quantity=10, entry=100.0, stop_loss=95.0, target=115.0)
    assert mp.exit_reason(116.0) == "TARGET"
    assert mp.exit_reason(94.0) == "STOP"
    assert mp.exit_reason(100.0) is None
    assert mp.realised_pnl(115.0) == pytest.approx(150.0)


def test_managed_position_exit_reasons_short():
    mp = ManagedPosition(symbol="X", quantity=-10, entry=100.0, stop_loss=105.0, target=85.0)
    assert mp.exit_reason(84.0) == "TARGET"
    assert mp.exit_reason(106.0) == "STOP"
    # Short profit: price falls below entry → positive pnl.
    assert mp.realised_pnl(90.0) == pytest.approx(100.0)


# -- TradeManager exits --------------------------------------------------


@pytest.mark.asyncio
async def test_trade_manager_exits_on_target(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="RELIANCE", quantity=10, entry=100.0,
        stop_loss=95.0, target=110.0,
    )
    # Seed a winning quote and sweep.
    await md.publish("RELIANCE", 111.0)
    await tm._sweep()
    # Position is no longer managed.
    assert tm.managed_positions() == []
    # A SELL trade with positive pnl was written.
    trades = db_session.query(TradeRow).filter_by(symbol="RELIANCE").all()
    assert len(trades) == 1
    assert trades[0].side == "SELL"
    assert trades[0].pnl == pytest.approx(110.0)  # (111-100)*10
    assert trades[0].status == "filled"


@pytest.mark.asyncio
async def test_trade_manager_exits_on_stop(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="TCS", quantity=5, entry=200.0,
        stop_loss=190.0, target=230.0,
    )
    await md.publish("TCS", 189.0)
    await tm._sweep()
    assert tm.managed_positions() == []
    trades = db_session.query(TradeRow).filter_by(symbol="TCS").all()
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(-55.0)  # (189-200)*5


@pytest.mark.asyncio
async def test_manual_close_settles_at_last_price(db_session, isolated_db):
    md = MarketDataBus()
    # Pre-create the position row so we can assert it gets flattened.
    db_session.add(
        PositionRow(symbol="INFY", quantity=8, average_price=100.0, last_price=100.0)
    )
    db_session.commit()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="INFY", quantity=8, entry=100.0, stop_loss=95.0, target=120.0,
    )
    await md.publish("INFY", 105.0)
    result = await tm.close_position("INFY", reason="MANUAL")
    assert result is not None
    assert result["reason"] == "MANUAL"
    assert result["pnl"] == pytest.approx(40.0)  # (105-100)*8
    db_session.expire_all()
    pos = db_session.query(PositionRow).filter_by(symbol="INFY").one()
    assert pos.quantity == 0


@pytest.mark.asyncio
async def test_close_unknown_symbol_returns_none(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    assert await tm.close_position("NOPE") is None


@pytest.mark.asyncio
async def test_close_unmanaged_position_from_db(db_session, isolated_db):
    """Bug fix: a position that isn't in the in-memory managed book
    (e.g. opened before the trade manager started, or seeded) must
    still be closeable from the dashboard."""
    md = MarketDataBus()
    db_session.add(
        PositionRow(symbol="WIPRO", quantity=12, average_price=240.0, last_price=255.0)
    )
    db_session.commit()
    tm = TradeManager(market_data=md)
    # Not registered in the managed book — close must still work via the
    # DB fallback, settling at the row's last_price.
    assert tm.managed_positions() == []
    result = await tm.close_position("WIPRO", reason="MANUAL")
    assert result is not None
    assert result["pnl"] == pytest.approx((255.0 - 240.0) * 12)
    db_session.expire_all()
    pos = db_session.query(PositionRow).filter_by(symbol="WIPRO").one()
    assert pos.quantity == 0


@pytest.mark.asyncio
async def test_close_all_flattens_unmanaged_positions(db_session, isolated_db):
    md = MarketDataBus()
    db_session.add_all([
        PositionRow(symbol="A", quantity=5, average_price=100.0, last_price=110.0),
        PositionRow(symbol="B", quantity=3, average_price=50.0, last_price=45.0),
    ])
    db_session.commit()
    tm = TradeManager(market_data=md)
    results = await tm.close_all()
    assert len(results) == 2
    db_session.expire_all()
    assert all(p.quantity == 0 for p in db_session.query(PositionRow).all())


# -- Integration: quote feed seeds a fill for a BUY ----------------------


@pytest.mark.asyncio
async def test_manager_with_quote_feed_fills_buy(db_session, isolated_db, monkeypatch):
    """The critical gap: with a quote feed attached, a BUY signal that
    carries no price levels still seeds a quote, fills at market, opens
    a position, and persists coherent entry/SL/target on the signal."""
    from app.config import reset_settings_cache
    from app.db.models import BrokerAccount, Signal, Strategy
    from app.execution.manager import Manager
    from app.execution.paper import PaperBackend
    from app.risk.engine import RiskEngine

    # Pin the global settings this test relies on (other tests mutate
    # os.environ via the settings API). monkeypatch auto-reverts.
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("DEFAULT_SL_PCT", "6.0")
    reset_settings_cache()

    # Start from a clean position book so a leaked position from an
    # earlier test can't trip the concurrent-position cap.
    db_session.query(PositionRow).delete()
    db_session.commit()

    md = MarketDataBus()
    qf = QuoteFeed(market_data=md, seed=7)
    # Default SessionLocal (shared in-memory engine) for thread-safe
    # cross-executor writes, matching the exit tests above.
    paper = PaperBackend(market_data=md)
    paper.start()
    try:
        strat = Strategy(name="qf-route", enabled=True, config={})
        db_session.add(strat)
        db_session.commit()
        db_session.refresh(strat)
        acct = BrokerAccount(
            name="qf-acct", broker="fyers", paper_mode=True, enabled=True
        )
        db_session.add(acct)
        db_session.commit()
        db_session.refresh(acct)
        # Generous caps so this focused test exercises the fill path,
        # not the concurrent/sector limits (covered by test_risk_engine).
        # Strategy-level risk overrides so the engine ignores any
        # leaked global risk settings from earlier tests.
        strat.config = {
            "broker_account_id": acct.id,
            "max_concurrent_positions": 999,
            "sector_concentration_pct": 100.0,
            "max_capital_risk_pct": 1.0,
            "max_single_position_pct": 50.0,
        }
        # No price levels in the rationale — the quote feed must supply them.
        sig = Signal(
            symbol="ZEELEARN", action="BUY", confidence=0.9,
            status="pending", strategy_id=strat.id, rationale="Rule matched.",
        )
        db_session.add(sig)
        db_session.commit()
        db_session.refresh(sig)

        risk = RiskEngine(market_data=md, portfolio_value=10_000_000.0)
        # Manager uses the default SessionLocal (same in-memory engine
        # as db_session); its DB helpers open fresh sessions, which is
        # the production path and is thread-safe for the executor.
        mgr = Manager(risk_engine=risk, market_data=md, paper_backend=paper)
        mgr.attach_quote_feed(qf)

        outcome = await mgr.process_signal(sig.id)
        assert outcome is not None
        # Before this work a BUY with no price levels would expire for
        # lack of a quote. Now the quote feed seeds one and it fills.
        assert outcome["approved"] is True
        assert outcome["state"] == "FILLED"
        assert outcome["qty"] >= 1

        # A quote was seeded for the symbol.
        q = await md.get_quote("ZEELEARN")
        assert q is not None and q.last_price > 0

        # A filled BUY trade and an open position were persisted.
        db_session.expire_all()
        trades = db_session.query(TradeRow).filter_by(symbol="ZEELEARN").all()
        assert any(t.status == "filled" and t.side == "BUY" for t in trades)
        pos = db_session.query(PositionRow).filter_by(symbol="ZEELEARN").one_or_none()
        assert pos is not None and pos.quantity >= 1
    finally:
        await paper.stop()
        # monkeypatch reverts the env; refresh the cache so the next
        # test sees the restored values.
        reset_settings_cache()
