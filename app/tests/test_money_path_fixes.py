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
