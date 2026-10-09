"""Manual (Trade page) order path: input checks, double-submit guard,
broker rate-limit handling, and a local save failure after the broker
accepted the order. Stub backends only; Fyers is never contacted."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.db.models import BrokerAccount
from app.execution.base import OrderResult, OrderSide, OrderState, OrderType, ProductType
from app.execution.fyers_live import FyersAPIError, FyersLiveBackend


@pytest.fixture()
def real_account(db_session, isolated_db) -> BrokerAccount:
    acc = BrokerAccount(
        name="Fyers Live", broker="fyers", app_id="APP123", secret_key="sek",  # noqa: S106
        access_token="AT123", paper_mode=False, enabled=True,
    )
    db_session.add(acc)
    db_session.commit()
    return db_session.query(BrokerAccount).filter_by(name="Fyers Live").one()


class _Stub:
    name = "stub"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.placed: list[dict] = []

    async def place_order(self, *, signal, symbol, side, quantity, order_type=OrderType.MARKET,
                          limit_price=None, stop_price=None, product_type=ProductType.INTRADAY):
        self.placed.append({"quantity": quantity, "limit_price": limit_price, "stop_price": stop_price})
        if self.delay:
            await asyncio.sleep(self.delay)
        return OrderResult(broker_order_id=f"STUB-{len(self.placed)}", state=OrderState.PENDING,
                           symbol=symbol, side=side, quantity=quantity, order_type=order_type)


def _install(account_id: int, stub: _Stub) -> _Stub:
    from app.main import app

    app.state.execution_manager.register_backend(account_id, stub)
    return stub


def _body(account_id: int, **kw) -> dict:
    return {"account_id": account_id, "symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 10,
            "order_type": "MARKET", "product_type": "INTRADAY", **kw}


@pytest.mark.parametrize("field,value", [
    ("quantity", 10.5), ("quantity", "ten"), ("side", "HOLD"),
])
def test_bad_quantity_or_side_is_refused_before_the_broker(client: TestClient, real_account, field, value):
    stub = _install(real_account.id, _Stub())
    r = client.post("/api/orders", json=_body(real_account.id, **{field: value}))
    assert r.status_code == 422, r.text
    assert stub.placed == []


@pytest.mark.parametrize("price", [0, -5, "abc"])
def test_bad_limit_price_is_refused(client: TestClient, real_account, price):
    stub = _install(real_account.id, _Stub())
    r = client.post("/api/orders", json=_body(real_account.id, order_type="LIMIT", limit_price=price))
    assert r.status_code == 422, r.text
    assert stub.placed == []


def test_string_prices_reach_the_broker_as_numbers_and_stray_prices_drop(client: TestClient, real_account):
    stub = _install(real_account.id, _Stub())
    r = client.post("/api/orders", json=_body(real_account.id, order_type="LIMIT",
                                              limit_price="612.5", stop_price=600))
    assert r.status_code == 200, r.text
    assert stub.placed[-1] == {"quantity": 10, "limit_price": 612.5, "stop_price": None}


def test_string_false_is_not_a_risk_override(client: TestClient, real_account):
    _install(real_account.id, _Stub())
    from app.execution.manager import Manager

    seen: dict = {}
    orig = Manager.place_manual_order

    async def spy(self, **kw):
        seen.update(kw)
        return await orig(self, **kw)

    Manager.place_manual_order = spy
    try:
        r = client.post("/api/orders", json=_body(real_account.id, bypass_risk="false"))
    finally:
        Manager.place_manual_order = orig
    assert r.status_code == 200, r.text
    assert seen["bypass_risk"] is False


def test_identical_order_in_flight_is_refused(client: TestClient, real_account):
    from app.api import orders

    stub = _install(real_account.id, _Stub())
    # The first order is still waiting on the broker.
    key = (real_account.id, "NSE:SBIN-EQ", "BUY", 10, "MARKET", "INTRADAY", None, None)
    orders._IN_FLIGHT.add(key)
    try:
        r = client.post("/api/orders", json=_body(real_account.id))
        assert r.status_code == 409, r.text
        assert stub.placed == []
        # A different order is not held up.
        assert client.post("/api/orders", json=_body(real_account.id, quantity=5)).status_code == 200
    finally:
        orders._IN_FLIGHT.discard(key)
    # Once the first returns, the same order can be placed again on purpose.
    assert client.post("/api/orders", json=_body(real_account.id)).status_code == 200
    assert not orders._IN_FLIGHT


def test_local_save_failure_after_broker_accept_is_not_a_500(client: TestClient, real_account, monkeypatch):
    from app.execution.manager import Manager

    _install(real_account.id, _Stub())

    def boom(self, *a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(Manager, "_persist_manual_trade_executed", boom)
    r = client.post("/api/orders", json=_body(real_account.id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["broker_order_id"] == "STUB-1"
    assert "do not place it again" in body["warning"]


@pytest.mark.asyncio
async def test_fyers_429_on_place_is_rejected_not_unconfirmed():
    class RateLimited:
        async def place_order(self, payload):
            raise FyersAPIError("fyers 429: rate limited", status_code=429, retryable=True)

    be = FyersLiveBackend(app_id="APP", access_token="TOK", broker_account_id=1, client=RateLimited())
    res = await be.place_order(signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY, quantity=1)
    assert res.state == OrderState.REJECTED
    assert "did not place" in res.error


@pytest.mark.asyncio
async def test_fyers_timeout_on_place_stays_unconfirmed():
    class TimedOut:
        async def place_order(self, payload):
            raise FyersAPIError("transport error: ReadTimeout", retryable=True)

    be = FyersLiveBackend(app_id="APP", access_token="TOK", broker_account_id=1, client=TimedOut())
    res = await be.place_order(signal=None, symbol="NSE:SBIN-EQ", side=OrderSide.BUY, quantity=1)
    assert res.state == OrderState.PENDING and res.broker_order_id == ""
