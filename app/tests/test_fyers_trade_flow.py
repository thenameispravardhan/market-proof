"""Fyers + Trade-page workflow regressions.

Each test pins one bug from the Fyers integration / Trade page deep dive:
fills read off the wrong field, a refused cancel hiding a live order or a
fill, a timed-out order reported as placed, position averages for shorts,
duplicate order rows swallowing broker updates, expired tokens shown as
connected. The Fyers transport is never opened — stub backends only.
"""
from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.db.models import BrokerAccount, Position, Trade
from app.execution.base import (
    OrderResult,
    OrderState,
    OrderStatus,
    OrderType,
    ProductType,
    apply_fill,
)


@pytest.fixture()
def real_account(db_session, isolated_db) -> BrokerAccount:
    acc = BrokerAccount(
        name="Fyers Live", broker="fyers", app_id="APP123", secret_key="sek",  # noqa: S106
        access_token="AT123", paper_mode=False, enabled=True,
    )
    db_session.add(acc)
    db_session.commit()
    return db_session.query(BrokerAccount).filter_by(name="Fyers Live").one()


class StubBackend:
    """Places with a configurable result; cancel and status are scripted."""

    name = "stub"

    def __init__(self, *, place: Optional[OrderResult] = None, cancel_ok: bool = True,
                 status: Optional[OrderStatus] = None) -> None:
        self.place = place
        self.cancel_ok = cancel_ok
        self.status = status
        self.placed: list[dict[str, Any]] = []

    async def place_order(self, *, signal, symbol, side, quantity, order_type=OrderType.MARKET,
                          limit_price=None, stop_price=None, product_type=ProductType.INTRADAY) -> OrderResult:
        self.placed.append({"order_type": order_type, "limit_price": limit_price, "stop_price": stop_price})
        if self.place is not None:
            return self.place
        return OrderResult(broker_order_id="ORD-1", state=OrderState.PENDING, symbol=symbol,
                           side=side, quantity=quantity, order_type=order_type)

    async def cancel_order(self, broker_order_id: str) -> bool:
        return self.cancel_ok

    async def get_order_status(self, broker_order_id: str) -> Optional[OrderStatus]:
        return self.status

    async def get_positions(self) -> list:
        return []


def _install(account_id: int, backend: StubBackend) -> StubBackend:
    from app.main import app

    app.state.execution_manager.register_backend(account_id, backend)
    return backend


def _place(client: TestClient, account_id: int, **over: Any):
    body = {"account_id": account_id, "symbol": "NSE:SBIN-EQ", "side": "BUY",
            "quantity": 5, "order_type": "MARKET"}
    body.update(over)
    return client.post("/api/orders", json=body)


# ---------------------------------------------------------------------------
# Cancel: a broker "no" is checked against the order's real state
# ---------------------------------------------------------------------------


def _status(state: OrderState, *, filled: int = 0, price: float = 0.0, oid: str = "ORD-1") -> OrderStatus:
    code = {OrderState.FILLED: 2, OrderState.CANCELLED: 1, OrderState.REJECTED: 5,
            OrderState.PENDING: 6, OrderState.EXPIRED: 7}[state]
    raw = {"id": oid, "symbol": "NSE:SBIN-EQ", "status": code, "qty": 5,
           "filledQty": filled, "tradedPrice": price}
    return OrderStatus(broker_order_id=oid, state=state, filled_quantity=filled,
                       average_price=price or None, raw=raw)


def test_refused_cancel_on_a_still_working_order_leaves_it_pending(
    client: TestClient, db_session, real_account
):
    """A timeout / 5xx on cancel answers False too. The row used to be
    flipped to "cancelled" anyway, hiding an order that was still live."""
    _install(real_account.id, StubBackend())
    assert _place(client, real_account.id).status_code == 200
    _install(real_account.id, StubBackend(cancel_ok=False, status=_status(OrderState.PENDING)))

    r = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["reason"] == "broker_refused"
    assert body["rows_updated"] == 0
    assert "may still be working" in body["message"]
    db_session.expire_all()
    assert db_session.query(Trade).filter_by(broker_order_id="ORD-1").one().status == "placed"
    pending = client.get("/api/orders/pending").json()["orders"]
    assert [o["broker_order_id"] for o in pending] == ["ORD-1"]


def test_refused_cancel_when_status_is_unreachable_leaves_it_pending(
    client: TestClient, db_session, real_account
):
    _install(real_account.id, StubBackend())
    _place(client, real_account.id)
    _install(real_account.id, StubBackend(cancel_ok=False, status=None))

    body = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"}).json()
    assert body["reason"] == "broker_refused"
    db_session.expire_all()
    assert db_session.query(Trade).filter_by(broker_order_id="ORD-1").one().status == "placed"


def test_refused_cancel_on_an_order_already_cancelled_at_the_broker_clears_it(
    client: TestClient, db_session, real_account
):
    _install(real_account.id, StubBackend())
    _place(client, real_account.id)
    _install(real_account.id, StubBackend(cancel_ok=False, status=_status(OrderState.CANCELLED)))

    body = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"}).json()
    assert body["reason"] == "already_gone" and body["rows_updated"] == 1
    db_session.expire_all()
    assert db_session.query(Trade).filter_by(broker_order_id="ORD-1").one().status == "cancelled"
    assert client.get("/api/orders/pending").json()["count"] == 0


def test_refused_cancel_on_a_filled_order_records_the_fill_not_a_cancel(
    client: TestClient, db_session, real_account
):
    """The order filled before the cancel landed. It used to be marked
    "cancelled" — and the postback then skipped the fill as settled, so a
    real position never reached the positions table."""
    _install(real_account.id, StubBackend())
    _place(client, real_account.id)
    _install(real_account.id, StubBackend(
        cancel_ok=False, status=_status(OrderState.FILLED, filled=5, price=812.4)))

    body = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"}).json()
    assert body["reason"] == "already_filled"
    db_session.expire_all()
    trade = db_session.query(Trade).filter_by(broker_order_id="ORD-1").one()
    assert trade.status == "filled"
    pos = db_session.query(Position).filter_by(symbol="NSE:SBIN-EQ").one()
    assert pos.quantity == 5 and pos.average_price == pytest.approx(812.4)


def test_refused_cancel_on_a_partly_filled_cancelled_order_keeps_the_filled_shares(
    client: TestClient, db_session, real_account
):
    _install(real_account.id, StubBackend())
    _place(client, real_account.id)
    _install(real_account.id, StubBackend(
        cancel_ok=False, status=_status(OrderState.CANCELLED, filled=2, price=811.0)))

    body = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"}).json()
    assert body["reason"] == "already_gone"
    db_session.expire_all()
    trade = db_session.query(Trade).filter_by(broker_order_id="ORD-1").one()
    assert trade.status == "cancelled" and trade.filled_qty == 2
    assert db_session.query(Position).filter_by(symbol="NSE:SBIN-EQ").one().quantity == 2


def test_cancel_never_rewrites_a_filled_row(client: TestClient, db_session, real_account):
    _install(real_account.id, StubBackend())
    _place(client, real_account.id)
    db_session.query(Trade).filter_by(broker_order_id="ORD-1").update({"status": "filled"})
    db_session.commit()

    client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"})
    db_session.expire_all()
    assert db_session.query(Trade).filter_by(broker_order_id="ORD-1").one().status == "filled"


# ---------------------------------------------------------------------------
# Place: a timed-out order is "unconfirmed", never "placed"
# ---------------------------------------------------------------------------


def test_order_without_broker_confirmation_is_reported_unconfirmed(
    client: TestClient, db_session, real_account
):
    """A timeout on POST /orders/sync came back as a green "PENDING" ticket
    and an id-less "placed" row nobody could cancel — inviting a second
    click and a duplicate real-money order."""
    from app.execution.base import OrderSide

    timed_out = OrderResult(broker_order_id="", state=OrderState.PENDING, symbol="NSE:SBIN-EQ",
                            side=OrderSide.BUY, quantity=5, order_type=OrderType.MARKET,
                            error="transport error: ReadTimeout")
    _install(real_account.id, StubBackend(place=timed_out))

    r = _place(client, real_account.id)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["status"] == "UNCONFIRMED"
    assert "before placing it again" in body["error"]
    db_session.expire_all()
    row = db_session.query(Trade).filter_by(symbol="NSE:SBIN-EQ").one()
    assert row.status == "unconfirmed"
    assert client.get("/api/orders/pending").json()["count"] == 0


@pytest.mark.parametrize("alias", ["SL", "SL-L"])
def test_stop_limit_aliases_are_accepted(client: TestClient, real_account, alias: str):
    stub = _install(real_account.id, StubBackend())
    r = _place(client, real_account.id, order_type=alias, side="SELL", limit_price=799.0, stop_price=800.0)
    assert r.status_code == 200, r.text
    assert stub.placed[-1]["order_type"] == OrderType.STOP_LOSS


def test_stop_limit_alias_still_requires_both_prices(client: TestClient, real_account):
    _install(real_account.id, StubBackend())
    r = _place(client, real_account.id, order_type="SL", stop_price=800.0)
    assert r.status_code == 422
    assert "limit_price" in r.json()["detail"]


def test_pending_list_carries_the_order_product(client: TestClient, real_account):
    _install(real_account.id, StubBackend())
    _place(client, real_account.id, product_type="CNC")
    (row,) = client.get("/api/orders/pending").json()["orders"]
    assert row["product"] == "DELIVERY"


# ---------------------------------------------------------------------------
# Position averages
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "qty, avg, fill, price, expected",
    [
        (0, 0.0, 10, 100.0, (10, 100.0)),            # open long
        (10, 100.0, 10, 110.0, (20, 105.0)),          # add to long
        (10, 100.0, -4, 120.0, (6, 100.0)),           # reduce long: cost unchanged
        (10, 100.0, -10, 120.0, (0, 100.0)),          # flat
        (5, 100.0, -10, 110.0, (-5, 110.0)),          # flip long -> short at the fill
        (-10, 100.0, -10, 90.0, (-20, 95.0)),         # add to short (was never averaged)
        (-10, 100.0, 5, 90.0, (-5, 100.0)),           # cover part of a short (was moved to 110)
        (-5, 100.0, 10, 90.0, (5, 90.0)),             # flip short -> long
        (10, 100.0, 5, None, (15, 100.0)),            # no price: keep the cost
    ],
)
def test_apply_fill(qty, avg, fill, price, expected):
    new_qty, new_avg = apply_fill(qty, avg, fill, price)
    assert new_qty == expected[0]
    assert new_avg == pytest.approx(expected[1])


@pytest.mark.asyncio
async def test_reconciled_cover_of_a_short_keeps_its_entry_price(db_session, isolated_db):
    from app.execution.order_reconcile import reconcile_order_update

    db_session.add(Position(symbol="NSE:SBIN-EQ", quantity=-10, average_price=100.0,
                            last_price=100.0, unrealized_pnl=0.0))
    db_session.add(Trade(symbol="NSE:SBIN-EQ", side="BUY", quantity=4, price=0.0,
                         order_type="MARKET", status="placed", broker_order_id="COVER-1"))
    db_session.commit()

    await reconcile_order_update(db_session, {"id": "COVER-1", "status": 2, "filledQty": 4,
                                              "tradedPrice": 90.0, "symbol": "NSE:SBIN-EQ"})
    db_session.expire_all()
    pos = db_session.query(Position).filter_by(symbol="NSE:SBIN-EQ").one()
    assert pos.quantity == -6
    assert pos.average_price == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Reconcile with duplicate order rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconcile_survives_duplicate_rows_for_one_order(db_session, isolated_db):
    """`one_or_none()` raised MultipleResultsFound on a duplicate row, so the
    fill was dropped and both rows sat "placed" forever."""
    from app.execution.order_reconcile import reconcile_order_update

    for _ in range(2):
        db_session.add(Trade(symbol="NSE:SBIN-EQ", side="BUY", quantity=3, price=0.0,
                             order_type="MARKET", status="placed", broker_order_id="DUP-1"))
    db_session.commit()

    res = await reconcile_order_update(db_session, {"id": "DUP-1", "status": 2, "filledQty": 3,
                                                    "tradedPrice": 50.0, "symbol": "NSE:SBIN-EQ"})
    assert res["ok"] and res["status"] == "filled"
    db_session.expire_all()
    assert {t.status for t in db_session.query(Trade).filter_by(broker_order_id="DUP-1")} == {"filled"}
    assert db_session.query(Position).filter_by(symbol="NSE:SBIN-EQ").one().quantity == 3


# ---------------------------------------------------------------------------
# Token expiry + OAuth callback
# ---------------------------------------------------------------------------


def _jwt(exp: float) -> str:
    def seg(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'HS256'})}.{seg({'exp': int(exp), 'sub': 'access_token'})}.sig"


def test_token_expiry_is_read_from_the_jwt():
    from app.execution.fyers_auth import token_expires_at, token_is_expired

    past, future = time.time() - 60, time.time() + 3600
    assert token_expires_at(_jwt(future)) == int(future)
    assert token_is_expired(_jwt(past)) is True
    assert token_is_expired(_jwt(future)) is False
    # Not a JWT / garbage: unknown, never "expired".
    for tok in ("AT123", "a.b.c", "", None):
        assert token_is_expired(tok) is False


def _fyers_env(monkeypatch, app_id: str = "APP123") -> None:
    from app import config as app_config

    monkeypatch.setenv("FYERS_APP_ID", app_id)
    monkeypatch.setenv("FYERS_SECRET_KEY", "FAKE-SECRET")
    app_config.reset_settings_cache()


def test_status_reports_an_expired_token_as_not_authorized(client: TestClient, db_session, isolated_db, monkeypatch):
    """The DB only knew a token existed, so a dead token kept the banner on
    "connected" while every order and quote 401'd."""
    from app import config as app_config

    _fyers_env(monkeypatch)
    try:
        db_session.add(BrokerAccount(name="fy", broker="fyers", app_id="APP123",
                                     access_token=_jwt(time.time() - 60), paper_mode=False, enabled=True))
        db_session.commit()
        body = client.get("/api/fyers/status").json()
        assert body["token_expired"] is True
        assert body["has_token"] is False
        assert body["authorized"] is False and body["connected"] is False
        assert "expired" in body["reason"]

        db_session.query(BrokerAccount).filter_by(name="fy").update({"access_token": _jwt(time.time() + 3600)})
        db_session.commit()
        body = client.get("/api/fyers/status").json()
        assert body["token_expired"] is False and body["authorized"] is True
    finally:
        app_config.reset_settings_cache()


def test_callback_with_duplicate_app_id_rows_stores_token_on_the_real_account(
    client: TestClient, db_session, isolated_db, monkeypatch
):
    """Two rows with the configured app_id made `scalar_one_or_none()` raise
    after the one-shot state was spent: a 500 and a restarted login."""
    from app import config as app_config
    from app.api import fyers_callback as cb
    from app.execution.fyers_auth import TokenResponse, register_state

    _fyers_env(monkeypatch, "APP-DUP")
    try:
        db_session.add(BrokerAccount(name="paper-copy", broker="fyers", app_id="APP-DUP", paper_mode=True))
        db_session.add(BrokerAccount(name="real", broker="fyers", app_id="APP-DUP", paper_mode=False))
        db_session.commit()

        async def fake_exchange(**_: Any) -> TokenResponse:
            return TokenResponse(access_token="NEW-TOKEN", raw={})

        monkeypatch.setattr(cb, "exchange_code_for_token", fake_exchange)
        register_state("dup-state")
        r = client.get("/api/fyers/callback", params={"auth_code": "C", "state": "dup-state"},
                       headers={"accept": "application/json"})
        assert r.status_code == 200, r.text
        db_session.expire_all()
        assert db_session.query(BrokerAccount).filter_by(name="real").one().access_token == "NEW-TOKEN"
        assert db_session.query(BrokerAccount).filter_by(name="paper-copy").one().access_token is None
    finally:
        app_config.reset_settings_cache()
