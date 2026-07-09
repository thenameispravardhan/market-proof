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
async def test_seed_symbol_returns_none_when_live_feed_cannot_price():
    """With a live feed wired, seed_symbol must NOT fabricate a synthetic
    anchor when the real price is unavailable — otherwise a synthetic entry
    gets marked/exited against the real feed (the CEIGALL ₹833→₹365 phantom
    loss). It returns None and publishes no simulated quote."""
    md = MarketDataBus()

    async def live_fn(sym):  # feed can't serve this symbol right now
        return None

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    price = await qf.seed_symbol("CEIGALL")
    assert price is None
    # No synthetic price leaked onto the bus for the fill to hit.
    assert await md.get_quote("CEIGALL") is None


@pytest.mark.asyncio
async def test_seed_symbol_uses_real_price_when_live_feed_serves():
    md = MarketDataBus()

    async def live_fn(sym):
        return 365.05

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    price = await qf.seed_symbol("CEIGALL")
    assert price == 365.05
    q = await md.get_quote("CEIGALL")
    assert q is not None and q.last_price == 365.05
    # Real Fyers tick — not flagged simulated.
    assert not (q.extra or {}).get("simulated")


@pytest.mark.asyncio
async def test_seed_symbol_synthetic_only_without_live_feed():
    """Pure offline paper mode (no live feed) keeps the self-consistent
    synthetic anchor so paper testing still fills."""
    md = MarketDataBus()
    qf = QuoteFeed(market_data=md)  # live_quote_fn=None
    price = await qf.seed_symbol("CEIGALL")
    assert price == _base_price_for("CEIGALL")


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
async def test_trade_manager_exits_on_target(db_session, isolated_db, monkeypatch):
    # Hard-target full-exit path (scale-out disabled). With scale-out on
    # (the default) a target hit instead scales out + trails — covered in
    # test_trade_exits.py.
    monkeypatch.setenv("SCALE_OUT_ENABLED", "0")
    from app.config import get_settings
    get_settings.cache_clear()
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
    get_settings.cache_clear()


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


# -- Edit stop-loss / target ---------------------------------------------


@pytest.mark.asyncio
async def test_update_levels_changes_managed_position(db_session, isolated_db):
    """Editing SL/target updates the in-memory book (so the next sweep
    exits on the new levels) and persists onto the position row."""
    md = MarketDataBus()
    db_session.add(
        PositionRow(symbol="HDFC", quantity=10, average_price=100.0, last_price=100.0)
    )
    db_session.commit()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="HDFC", quantity=10, entry=100.0, stop_loss=95.0, target=120.0,
    )
    result = await tm.update_levels("HDFC", stop_loss=98.0, target=130.0)
    assert result is not None
    assert result["stop_loss"] == pytest.approx(98.0)
    assert result["target"] == pytest.approx(130.0)
    # In-memory book reflects the edit.
    mp = tm.managed_positions()[0]
    assert mp.stop_loss == pytest.approx(98.0)
    assert mp.target == pytest.approx(130.0)
    # Persisted onto the position row (survives a restart).
    db_session.expire_all()
    pos = db_session.query(PositionRow).filter_by(symbol="HDFC").one()
    assert pos.stop_loss == pytest.approx(98.0)
    assert pos.target == pytest.approx(130.0)


@pytest.mark.asyncio
async def test_update_levels_arms_unmanaged_position_from_db(db_session, isolated_db):
    """A position not in the in-memory book (opened before the manager
    started, or seeded) can still have its levels edited — we hydrate it
    from the DB and arm it."""
    md = MarketDataBus()
    db_session.add(
        PositionRow(symbol="ITC", quantity=20, average_price=400.0, last_price=405.0)
    )
    db_session.commit()
    tm = TradeManager(market_data=md)
    assert tm.managed_positions() == []
    result = await tm.update_levels("ITC", stop_loss=390.0, target=440.0)
    assert result is not None
    assert result["stop_loss"] == pytest.approx(390.0)
    # Now armed in the managed book.
    assert any(mp.symbol == "ITC" for mp in tm.managed_positions())
    db_session.expire_all()
    pos = db_session.query(PositionRow).filter_by(symbol="ITC").one()
    assert pos.stop_loss == pytest.approx(390.0)
    assert pos.target == pytest.approx(440.0)


@pytest.mark.asyncio
async def test_update_levels_clears_a_level(db_session, isolated_db):
    """Passing None clears (disarms) a level."""
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="SBIN", quantity=5, entry=500.0, stop_loss=480.0, target=550.0,
    )
    result = await tm.update_levels("SBIN", stop_loss=None, target=560.0)
    assert result is not None
    assert result["stop_loss"] is None
    assert result["target"] == pytest.approx(560.0)
    mp = tm.managed_positions()[0]
    assert mp.stop_loss is None


@pytest.mark.asyncio
async def test_update_levels_unknown_symbol_returns_none(db_session, isolated_db):
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    assert await tm.update_levels("NOPE", stop_loss=1.0, target=2.0) is None


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


# -- Speed-trading: time-based exit --------------------------------------


@pytest.mark.asyncio
async def test_managed_position_time_exit_expired():
    """`time_exit_expired` returns True iff max_hold_seconds has elapsed."""
    from datetime import datetime, timedelta, timezone
    opened = datetime.now(timezone.utc) - timedelta(seconds=120)
    # 60s window, 120s elapsed -> expired.
    mp = ManagedPosition(
        symbol="X", quantity=10, entry=100.0, stop_loss=95.0, target=120.0,
        opened_at=opened, max_hold_seconds=60,
    )
    assert mp.time_exit_expired() is True
    # 200s window, 120s elapsed -> not expired.
    mp.max_hold_seconds = 200
    assert mp.time_exit_expired() is False
    # max_hold_seconds = 0 disables the time exit entirely.
    mp.max_hold_seconds = 0
    assert mp.time_exit_expired() is False


@pytest.mark.asyncio
async def test_trade_manager_exits_on_time_window(db_session, isolated_db):
    """A position held past `max_hold_seconds` is force-closed on the
    next sweep with reason TIME_EXIT, at the latest quote, regardless
    of whether SL/target has been hit. Speed-trading rule: capture the
    20-30 min spike and get out.
    """
    from datetime import datetime, timedelta, timezone
    md = MarketDataBus()
    # Build the manager then tamper with the registered position's
    # opened_at to simulate a position held for 31 minutes (past the
    # 30-min default window). Easier than sleeping in a test.
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="TATAMOTORS", quantity=10, entry=100.0,
        stop_loss=80.0, target=130.0,
        max_hold_seconds=1800,  # 30 min default
    )
    # Backdate opened_at by 31 minutes via the in-memory book.
    async with tm._lock:
        mp = tm._book["TATAMOTORS"]
        mp.opened_at = datetime.now(timezone.utc) - timedelta(seconds=31 * 60)
    # The current price is 105 (well within SL/target) — the only
    # reason to exit is TIME_EXIT.
    await md.publish("TATAMOTORS", 105.0)
    await tm._sweep()

    # Position no longer managed.
    assert tm.managed_positions() == []
    # A SELL trade was written with reason TIME_EXIT and a small
    # realised P&L (105 - 100) * 10 = +50.
    trades = db_session.query(TradeRow).filter_by(symbol="TATAMOTORS").all()
    assert len(trades) == 1
    assert trades[0].side == "SELL"
    assert trades[0].pnl == pytest.approx(50.0)
    assert trades[0].status == "filled"
    # The exit reason propagates into the trade_manager.exit log line
    # (covered by the audit_log row written by _settle_exit).
    from app.db.models import AuditLog
    audit_rows = db_session.query(AuditLog).filter_by(action="trade.closed").all()
    assert any(
        r.after and r.after.get("reason") == "TIME_EXIT"
        for r in audit_rows
    )


@pytest.mark.asyncio
async def test_time_exit_does_not_fire_when_disabled(db_session, isolated_db):
    """max_hold_seconds = 0 disables the time exit entirely — only
    SL/target apply. (Forward-compat for strategies that want the
    manager as a pure SL/target executor.)"""
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="HCL", quantity=5, entry=200.0,
        stop_loss=180.0, target=240.0,
        max_hold_seconds=0,  # disabled
    )
    # Backdate so we'd otherwise hit the time window, but it's disabled.
    from datetime import datetime, timedelta, timezone
    async with tm._lock:
        tm._book["HCL"].opened_at = datetime.now(timezone.utc) - timedelta(seconds=7200)
    await md.publish("HCL", 210.0)  # within SL/target
    await tm._sweep()
    # Still managed — only SL/target would close it.
    assert any(mp.symbol == "HCL" for mp in tm.managed_positions())
    # And no exit trade was written.
    assert db_session.query(TradeRow).filter_by(symbol="HCL").count() == 0


@pytest.mark.asyncio
async def test_register_uses_global_max_hold_default(db_session, isolated_db, monkeypatch):
    """When register() is called without an explicit max_hold_seconds,
    it falls back to the global Settings.MAX_HOLD_SECONDS — that's how
    the speed-trading 20-30 min window propagates to every trade.
    """
    from types import SimpleNamespace
    monkeypatch.setattr(
        "app.execution.trade_manager.get_settings",
        lambda: SimpleNamespace(MAX_HOLD_SECONDS=1234),
    )
    md = MarketDataBus()
    tm = TradeManager(market_data=md)
    await tm.register(
        symbol="AUTO", quantity=1, entry=10.0, stop_loss=9.0, target=12.0,
    )
    mp = tm.managed_positions()[0]
    assert mp.max_hold_seconds == 1234


# -- Phantom-price guard: never exit at a synthetic tick -----------------


@pytest.mark.asyncio
async def test_sweep_holds_position_on_synthetic_quote(db_session, isolated_db):
    """With a live feed wired, a SIMULATED tick (feed cold → hash anchor)
    must NOT exit a real-entry position. Otherwise a real ₹172.98 entry
    gets exited at the synthetic ₹1,641 hash price and books a phantom
    +₹15L gain (the PPLPHARMA bug). The position is held until a real tick.
    """
    md = MarketDataBus()

    async def live_fn(sym):  # feed can't price the symbol right now
        return None

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    tm = TradeManager(market_data=md, quote_feed=qf)
    await tm.register(
        symbol="PPLPHARMA", quantity=1040, entry=172.98,
        stop_loss=150.0, target=200.0, max_hold_seconds=0,
    )
    # Synthetic tick at the hash anchor (1641 >= target 200) — a TARGET
    # exit would fire and book a fake gain if the guard weren't there.
    assert _base_price_for("PPLPHARMA") == 1641.0
    await md.publish("PPLPHARMA", 1641.0, extra={"simulated": True})
    await tm._sweep()
    # Still managed — no phantom exit, no trade row written.
    assert any(mp.symbol == "PPLPHARMA" for mp in tm.managed_positions())
    assert db_session.query(TradeRow).filter_by(symbol="PPLPHARMA").count() == 0


@pytest.mark.asyncio
async def test_sweep_exits_on_real_quote_with_live_feed(db_session, isolated_db):
    """A REAL Fyers tick (source=fyers) still exits normally — the guard
    only suppresses synthetic ticks, not real ones."""
    md = MarketDataBus()

    async def live_fn(sym):
        return None

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    tm = TradeManager(market_data=md, quote_feed=qf)
    await tm.register(
        symbol="RELIANCE", quantity=10, entry=100.0, stop_loss=95.0, target=110.0,
    )
    await md.publish("RELIANCE", 111.0, extra={"source": "fyers"})
    await tm._sweep()
    assert tm.managed_positions() == []
    trades = db_session.query(TradeRow).filter_by(symbol="RELIANCE").all()
    assert len(trades) == 1
    assert trades[0].pnl == pytest.approx(110.0)  # (111-100)*10


@pytest.mark.asyncio
async def test_close_position_uses_last_real_price_when_feed_cold(
    db_session, isolated_db
):
    """A manual close with only a synthetic quote on the bus settles at the
    last REAL mark persisted on the row, never the hash anchor. SOLEX
    ₹1,060 entry closes at the last real ₹1,055 mark, not the synthetic
    ₹654 (which would fabricate a −₹1.38L loss)."""
    md = MarketDataBus()

    async def live_fn(sym):
        return None

    qf = QuoteFeed(market_data=md, live_quote_fn=live_fn)
    db_session.add(
        PositionRow(symbol="SOLEX", quantity=341, average_price=1060.0, last_price=1055.0)
    )
    db_session.commit()
    tm = TradeManager(market_data=md, quote_feed=qf)
    assert _base_price_for("SOLEX") == 654.0
    await md.publish("SOLEX", 654.0, extra={"simulated": True})
    result = await tm.close_position("SOLEX", reason="MANUAL")
    assert result is not None
    # Settled at the last real mark (1055), NOT the synthetic 654.
    assert result["exit_price"] == pytest.approx(1055.0)
    assert result["pnl"] == pytest.approx((1055.0 - 1060.0) * 341)


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
