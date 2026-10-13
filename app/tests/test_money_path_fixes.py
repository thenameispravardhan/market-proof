"""Regressions for the real-money review fixes (exit oversell, phantom reverse,
reconcile symbol keys, positions-failure-as-flat, paper switch bypass)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.models import Position as PositionRow
from app.execution import order_reconcile
from app.execution.base import OrderResult, OrderState, Position
from app.execution.fyers_live import FyersAPIError, FyersLiveBackend
from app.execution.manager import Manager
from app.execution.market_data import MarketDataBus
from app.tests.test_exit_management import (  # noqa: F401 — autouse fast-exit settings
    ExitStubBackend, _fast_exit_settings, exit_filled, make_account, make_tm, register,
)


def _pending(kw, oid="EXIT-1"):
    return OrderResult(broker_order_id=oid, state=OrderState.PENDING, symbol=kw["symbol"], side=kw["side"],
                       quantity=int(kw["quantity"]), order_type=kw["order_type"])


@pytest.mark.asyncio
async def test_ioc_partial_on_socket_chases_only_the_rest(db_session, isolated_db):
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(_pending)
    backend.queue_place(lambda kw: exit_filled(kw, price=96.0, order_id="EXIT-2"))
    order_reconcile.RECENT_TERMINAL["EXIT-1"] = {"id": "EXIT-1", "status": 1, "filledQty": 400, "tradedPrice": 96.5}
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        await register(tm, entry=100.0, stop=97.0, account_id=make_account(db_session))
        out = await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        assert out is not None
        assert backend.orders[1]["quantity"] == 600      # not another 1000 (naked short)
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_exit_echo_never_reverses_the_position(db_session, isolated_db):
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_filled(kw, price=96.8))
    db_session.add(PositionRow(symbol="RELIANCE", quantity=1000, average_price=100.0, last_price=100.0, unrealized_pnl=0.0))
    db_session.commit()
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        await register(tm, entry=100.0, stop=97.0, account_id=make_account(db_session))
        await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        await order_reconcile.reconcile_order_update(
            db_session, {"id": "EXIT-1", "status": 2, "filledQty": 1000, "tradedPrice": 96.8, "symbol": "NSE:RELIANCE"},
            retry=False)
        db_session.expire_all()
        assert db_session.query(PositionRow).filter_by(symbol="RELIANCE").one().quantity == 0
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_reconcile_matches_full_symbol_book_keys(db_session, isolated_db):
    md = MarketDataBus()
    tm = make_tm(md, backend=ExitStubBackend(),
                 live_positions=lambda: [Position(symbol="NSE:RELIANCE-EQ", quantity=1000, average_price=100.0)])
    try:
        await register(tm, symbol="NSE:RELIANCE-EQ", entry=100.0, stop=97.0, account_id=make_account(db_session))
        await tm._reconcile_positions()
        assert "NSE:RELIANCE-EQ" in tm._book              # still open at the broker: still managed
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_failed_positions_call_is_not_flat():
    class Client:
        async def get_positions(self):
            raise FyersAPIError("fyers 401", status_code=401)
    b = FyersLiveBackend(app_id="X-100", access_token="t", broker_account_id=1, client=Client())
    assert await b.get_positions() == []
    with pytest.raises(FyersAPIError):
        await b.get_positions(strict=True)


def test_paper_switch_beats_a_cached_live_backend():
    live = FyersLiveBackend(app_id="X-100", access_token="t", broker_account_id=7)
    m = Manager(market_data=MarketDataBus(), backends={7: live},
                settings_provider=lambda: SimpleNamespace(is_paper=True, FYERS_APP_ID=""))
    acct = SimpleNamespace(id=7, paper_mode=False, name="live", access_token="t")
    assert m._backend_for(acct) is m.paper_backend


@pytest.mark.asyncio
async def test_exit_over_freeze_limit_is_sliced(db_session, isolated_db):
    sym = "NSE:NIFTY25O1425000CE"
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: exit_filled(kw, price=95.0, order_id="E1"))
    backend.queue_place(lambda kw: exit_filled(kw, price=95.0, order_id="E2"))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync(sym, 96.0, bid=96.0)
        await register(tm, symbol=sym, qty=2000, entry=100.0, stop=97.0, account_id=make_account(db_session))
        assert await tm._exit(sym, reason="STOP", exit_price=96.0) is not None
        assert [o["quantity"] for o in backend.orders] == [1800, 200]
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_backstop_rests_at_broker_and_is_cancelled_on_exit(db_session, isolated_db, monkeypatch):
    import asyncio

    from app import config as app_config
    from app.execution.base import OrderType

    monkeypatch.setenv("BROKER_STOP_ENABLED", "true")
    app_config.reset_settings_cache()
    md = MarketDataBus()
    backend = ExitStubBackend()
    backend.queue_place(lambda kw: _pending(kw, oid="STOP-1"))
    backend.queue_place(lambda kw: exit_filled(kw, price=96.8))
    tm = make_tm(md, backend=backend)
    try:
        md.set_quote_sync("RELIANCE", 96.8, bid=96.8)
        await register(tm, entry=100.0, stop=97.0, account_id=make_account(db_session))
        for _ in range(50):
            if tm._book["RELIANCE"].broker_stop_id:
                break
            await asyncio.sleep(0.01)
        stop = backend.orders[0]
        assert stop["order_type"] == OrderType.STOP_LOSS_MARKET and stop["stop_price"] == pytest.approx(96.0)
        await tm._exit("RELIANCE", reason="STOP", exit_price=96.8)
        assert backend.cancelled[0] == "STOP-1"          # never left resting on a flat position
    finally:
        tm.stop()


@pytest.mark.asyncio
async def test_live_position_exits_at_fyers_even_in_global_paper_mode(isolated_db, monkeypatch):
    """TRADING_MODE=paper must not turn a real Fyers position's exit into a DB-only close."""
    from app.execution import manager as mgr_mod

    acc = SimpleNamespace(id=7, name="live", paper_mode=False, app_id="APP-100", access_token="tok")
    m = Manager(market_data=MarketDataBus(), settings_provider=lambda: SimpleNamespace(is_paper=True, FYERS_APP_ID=""))
    monkeypatch.setattr(mgr_mod, "_load_account", lambda *_: acc)
    assert isinstance(await m.exit_backend_for_account(7), FyersLiveBackend)
    acc.paper_mode = True
    m._backends.clear()
    assert await m.exit_backend_for_account(7) is None


def test_ticket_levels_refused_before_the_order():
    from app.api.orders import _levels_problem

    assert _levels_problem("BUY", 100.0, 95.0, 110.0) is None
    assert _levels_problem("BUY", 100.0, 101.0, None)          # stop above a long entry
    assert _levels_problem("SELL", 100.0, 99.0, None)          # stop below a short entry
    assert _levels_problem("BUY", None, 110.0, 105.0)          # stop beyond target, no price needed


def test_manual_order_lands_on_the_bots_row(isolated_db):
    from app.db import session as dbs
    from app.execution.manager import _position_and_halt

    with dbs.SessionLocal() as db:
        db.add(PositionRow(symbol="SBIN", quantity=10, average_price=800.0, last_price=800.0))
        db.commit()
    qty, _product, _halt, sym = _position_and_halt("NSE:SBIN-EQ")
    assert (qty, sym) == (10, "SBIN")


@pytest.mark.asyncio
async def test_modify_snaps_prices_to_the_tick():
    sent = {}

    class C:
        async def modify_order(self, payload):
            sent.update(payload)
            return {"s": "ok"}

    b = FyersLiveBackend(app_id="A-100", access_token="t", broker_account_id=1, client=C())
    assert (await b.modify_order("1", stop_price=612.37))[0]
    assert sent["stopPrice"] == 612.35


def test_prices_snap_to_the_instruments_own_tick(monkeypatch):
    from app.execution.fyers_live import _on_tick
    from app.services import instrument_master

    monkeypatch.setattr(instrument_master, "_master_singleton",
                        SimpleNamespace(get=lambda s: SimpleNamespace(tick_size=0.0025) if s == "NSE:USDINR26OCTFUT" else None))
    assert _on_tick("NSE:USDINR26OCTFUT", 83.1234) == 83.1225
    assert _on_tick("NSE:SBIN-EQ", 812.37) == 812.35          # not in the master: 0.05 grid
    assert _on_tick("MCX:GOLD26OCTFUT", 71234.5) == 71234.5   # unknown + not NSE/BSE: untouched


def _bot_trade(db_session, oid: str, status: str = "expired", qty: int = 100):
    from app.db.models import Signal, Strategy, Trade

    strat = Strategy(name=f"s{oid}", enabled=True, config={})
    db_session.add(strat)
    db_session.commit()
    sig = Signal(symbol="RELIANCE", action="BUY", confidence=0.9, status="blocked", strategy_id=strat.id)
    db_session.add(sig)
    db_session.commit()
    db_session.add(Trade(signal_id=sig.id, symbol="RELIANCE", side="BUY", quantity=qty, price=100.0,
                         order_type="limit", status=status, broker_order_id=oid))
    db_session.commit()


@pytest.mark.asyncio
async def test_expired_entry_filled_late_is_still_handed_over(db_session, isolated_db):
    """A pending echo must not turn 'expired' back into 'placed' (the fill then lost its stop)."""
    from app.services.event_bus import event_bus

    _bot_trade(db_session, "LATE1")
    q = event_bus.subscribe("trade.executed")
    try:
        await order_reconcile.reconcile_order_update(db_session, {"id": "LATE1", "status": 6}, retry=False)
        await order_reconcile.reconcile_order_update(
            db_session, {"id": "LATE1", "status": 2, "filledQty": 100, "tradedPrice": 101.0}, retry=False)
        handover = []
        while not q.empty():
            p = q.get_nowait().payload
            if p.get("state") == "FILLED":
                handover.append(p)
        assert handover and handover[0]["quantity"] == 100 and handover[0]["entry"] == 101.0
    finally:
        event_bus.unsubscribe("trade.executed", q)


@pytest.mark.asyncio
async def test_expired_entry_part_filled_late_hands_over_the_slice(db_session, isolated_db):
    from app.services.event_bus import event_bus

    _bot_trade(db_session, "LATE2")
    q = event_bus.subscribe("trade.executed")
    try:
        await order_reconcile.reconcile_order_update(
            db_session, {"id": "LATE2", "status": 1, "filledQty": 30, "tradedPrice": 100.5}, retry=False)
        pos = db_session.query(PositionRow).filter_by(symbol="RELIANCE").one()
        assert pos.quantity == 30
        filled = []
        while not q.empty():
            p = q.get_nowait().payload
            if p.get("state") == "FILLED":
                filled.append(p)
        assert filled and filled[0]["quantity"] == 30
    finally:
        event_bus.unsubscribe("trade.executed", q)


def test_real_order_never_lands_on_a_paper_position(isolated_db):
    from app.db import session as dbs
    from app.db.models import Trade
    from app.execution.manager import _position_and_halt

    with dbs.SessionLocal() as db:
        db.add(PositionRow(symbol="SBIN", quantity=10, average_price=800.0, last_price=800.0))
        db.add(Trade(symbol="SBIN", side="BUY", quantity=10, price=800.0, order_type="market",
                     status="filled", broker_order_id="PAPER-1"))
        db.commit()
    with pytest.raises(ValueError, match="PAPER"):
        _position_and_halt("SBIN")
    assert _position_and_halt("NSE:SBIN-EQ")[3] == "NSE:SBIN-EQ"   # no alias onto the paper row
    assert _position_and_halt("BSE:SBIN-EQ")[3] == "BSE:SBIN-EQ"
