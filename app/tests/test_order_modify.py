"""POST /api/orders/modify — modify a pending order — and the Fyers v3
modify call behind it (PATCH /orders/sync).

Safety rules under test: no size increase through a modify (a bigger
order must go through the risk engine as a new order), nothing while the
kill switch / loss halt is engaged (cancel stays allowed), the same price
rules as place_order, and every local `trades` row + an audit row per row
updated on success. Fyers is never contacted (stub backends, a recorder
in place of `FyersClient._request`, or an httpx MockTransport).
"""
from __future__ import annotations

from typing import Any, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from app.db.models import AuditLog, BrokerAccount, Trade
from app.execution.base import OrderState, OrderStatus, OrderType
from app.execution.fyers_live import (
    FyersAPIError,
    FyersBlockedError,
    FyersClient,
    FyersLiveBackend,
)
from app.risk import circuit_breakers


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real outbound request fails the test (MockTransport clients pass)."""
    attempts: list[str] = []
    real_send = httpx.AsyncClient.send

    async def _guarded(self, request, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if isinstance(getattr(self, "_transport", None), httpx.MockTransport):
            return await real_send(self, request, *args, **kwargs)
        attempts.append(str(request.url))
        raise RuntimeError(f"network disabled in tests: {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", _guarded)
    yield
    assert not attempts, f"test tried to reach the network: {attempts}"


@pytest.fixture()
def real_account(db_session, isolated_db) -> BrokerAccount:
    acc = BrokerAccount(
        name="Fyers Live",
        broker="fyers",
        app_id="APP123",
        secret_key="sek",  # noqa: S106 (test only)
        access_token="AT123",
        paper_mode=False,
        enabled=True,
    )
    db_session.add(acc)
    db_session.commit()
    return acc


class _ModifyStub:
    """A live-backend stand-in that records modify calls."""

    name = "stub"

    def __init__(
        self,
        *,
        ok: bool = True,
        message: str = "Successfully modified order",
        raises: Optional[Exception] = None,
        status_qty: Optional[int] = None,
    ) -> None:
        self.ok, self.message, self.raises, self.status_qty = ok, message, raises, status_qty
        self.calls: list[dict[str, Any]] = []
        self.cancelled: list[str] = []

    async def modify_order(self, broker_order_id, *, quantity=None, limit_price=None,  # noqa: ANN001
                           stop_price=None, order_type=None):
        self.calls.append({
            "id": broker_order_id, "quantity": quantity, "limit_price": limit_price,
            "stop_price": stop_price, "order_type": order_type,
        })
        if self.raises is not None:
            raise self.raises
        return self.ok, self.message

    async def cancel_order(self, broker_order_id: str) -> bool:
        self.cancelled.append(broker_order_id)
        return True

    async def get_order_status(self, broker_order_id: str) -> Optional[OrderStatus]:
        if self.status_qty is None:
            return None
        return OrderStatus(
            broker_order_id=broker_order_id, state=OrderState.PENDING,
            raw={"id": broker_order_id, "qty": self.status_qty, "status": 6},
        )


def _install(account_id: int, backend) -> None:
    from app.main import app

    app.state.execution_manager.register_backend(account_id, backend)


def _trade(db_session, account_id: Optional[int], *, oid: str = "ORD-1", qty: int = 10,
           price: float = 612.5, order_type: str = "LIMIT") -> None:
    db_session.add(Trade(
        broker_account_id=account_id, broker_order_id=oid, symbol="NSE:SBIN-EQ", side="BUY",
        quantity=qty, price=price, order_type=order_type, status="placed",
    ))
    db_session.commit()


def _rows(db_session, oid: str = "ORD-1") -> list[Trade]:
    db_session.expire_all()
    return db_session.query(Trade).filter_by(broker_order_id=oid).order_by(Trade.id).all()


def _audits(db_session) -> list[AuditLog]:
    db_session.expire_all()
    return db_session.query(AuditLog).filter_by(action="order.manual_modified").order_by(AuditLog.id).all()


def _modify(client: TestClient, account_id: int, **fields):
    return client.post("/api/orders/modify", json={"account_id": account_id, "broker_order_id": "ORD-1", **fields})


# ---------------------------------------------------------------------------
# Happy path: rows + audit
# ---------------------------------------------------------------------------


def test_modify_updates_every_row_and_audits_each(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id)
    _trade(db_session, None)  # duplicate row for the same order (see cancel_order)

    r = _modify(client, real_account.id, quantity=6, limit_price=611.0)
    assert r.status_code == 200, r.text
    assert r.json() == {
        "ok": True, "broker_order_id": "ORD-1",
        "message": "Successfully modified order", "rows_updated": 2,
    }
    assert stub.calls == [{
        "id": "ORD-1", "quantity": 6, "limit_price": 611.0, "stop_price": None, "order_type": None,
    }]
    rows = _rows(db_session)
    assert [(t.quantity, t.price, t.order_type, t.status) for t in rows] == [
        (6, 611.0, "LIMIT", "placed"), (6, 611.0, "LIMIT", "placed"),
    ]
    audits = _audits(db_session)
    assert len(audits) == 2
    for a in audits:
        assert a.actor == "ui_trade_page"
        assert a.target == f"account:{real_account.id}"
        assert a.before == {"broker_order_id": "ORD-1", "quantity": 10, "price": 612.5, "order_type": "LIMIT"}
        assert a.after["quantity"] == 6 and a.after["price"] == 611.0
        assert a.after["broker_message"] == "Successfully modified order"


def test_modify_order_type_change_updates_the_row(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id)

    r = _modify(client, real_account.id, order_type="sl-m", stop_price=600)
    assert r.status_code == 200, r.text
    assert stub.calls[0]["order_type"] is OrderType.STOP_LOSS_MARKET
    # like the place path, an SL-M row carries its trigger as the price
    assert [(t.order_type, t.price, t.quantity) for t in _rows(db_session)] == [("SL-M", 600.0, 10)]

    r = _modify(client, real_account.id, order_type="MARKET")
    assert r.status_code == 200, r.text
    assert [(t.order_type, t.price) for t in _rows(db_session)] == [("MARKET", 0.0)]
    assert len(_audits(db_session)) == 2


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------


def test_modify_refuses_a_quantity_increase(client: TestClient, db_session, isolated_db, real_account) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id, qty=10)

    r = _modify(client, real_account.id, quantity=11, limit_price=611.0)
    assert r.status_code == 422
    assert "new order" in r.json()["detail"] and "risk engine" in r.json()["detail"]
    assert stub.calls == []
    assert [(t.quantity, t.price) for t in _rows(db_session)] == [(10, 612.5)]
    assert _audits(db_session) == []

    # the same size is fine (not an increase)
    assert _modify(client, real_account.id, quantity=10).status_code == 200


def test_modify_blocked_while_kill_switch_engaged_but_cancel_still_works(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id)
    circuit_breakers.trip_manual_kill(db_session)

    r = _modify(client, real_account.id, limit_price=600.0)
    assert r.status_code == 409
    assert "halted" in r.json()["detail"] and "manual_kill_switch" in r.json()["detail"]
    assert stub.calls == []
    assert [t.price for t in _rows(db_session)] == [612.5]

    r = client.post("/api/orders/cancel", json={"account_id": real_account.id, "broker_order_id": "ORD-1"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert stub.cancelled == ["ORD-1"]


def test_modify_allowed_again_after_resume(client: TestClient, db_session, isolated_db, real_account) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id)
    circuit_breakers.trip_manual_kill(db_session)
    assert _modify(client, real_account.id, limit_price=600.0).status_code == 409
    circuit_breakers.clear_halt(db_session)
    assert _modify(client, real_account.id, limit_price=600.0).status_code == 200


def test_unknown_order_size_comes_from_the_broker(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    """No local rows (placed in the Fyers app): the broker's order status
    gives the current size, so increases are still refused."""
    stub = _ModifyStub(status_qty=5)
    _install(real_account.id, stub)

    assert _modify(client, real_account.id, quantity=6).status_code == 422
    assert stub.calls == []
    r = _modify(client, real_account.id, quantity=4)
    assert r.status_code == 200 and r.json()["rows_updated"] == 0
    # still audited — one row, nothing local before
    (audit,) = _audits(db_session)
    assert audit.before is None
    assert audit.after["broker_order_id"] == "ORD-1" and audit.after["quantity"] == 4


def test_unknown_order_without_status_allows_price_changes_only(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    stub = _ModifyStub(status_qty=None)
    _install(real_account.id, stub)

    r = _modify(client, real_account.id, quantity=1)
    assert r.status_code == 422 and "only price" in r.json()["detail"]
    assert stub.calls == []
    r = _modify(client, real_account.id, limit_price=611.5, stop_price=611.0)
    assert r.status_code == 200 and r.json()["ok"] is True


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields, needle",
    [
        ({}, "nothing to modify"),
        ({"quantity": 0}, "quantity must be > 0"),
        ({"quantity": -3}, "quantity must be > 0"),
        ({"quantity": 2.5}, "whole number"),
        ({"quantity": "lots"}, "whole number"),
        ({"quantity": True}, "whole number"),
        ({"limit_price": 0}, "must be > 0"),
        ({"stop_price": "abc"}, "must be > 0"),
        ({"order_type": "LIMIT"}, "limit_price required"),
        ({"order_type": "STOP_LOSS", "limit_price": 600}, "stop_price required"),
        ({"order_type": "SL-M"}, "stop_price required"),
        ({"order_type": "ICEBERG"}, "order_type must be"),
    ],
)
def test_modify_validation(
    client: TestClient, db_session, isolated_db, real_account, fields: dict, needle: str
) -> None:
    stub = _ModifyStub()
    _install(real_account.id, stub)
    _trade(db_session, real_account.id)
    r = _modify(client, real_account.id, **fields)
    assert r.status_code == 422, r.text
    assert needle in r.json()["detail"]
    assert stub.calls == []


def test_modify_requires_the_order_id(client: TestClient, isolated_db, real_account) -> None:
    r = client.post("/api/orders/modify", json={"account_id": real_account.id, "quantity": 1})
    assert r.status_code == 422
    r = client.post("/api/orders/modify", json={"account_id": real_account.id, "broker_order_id": " ", "quantity": 1})
    assert r.status_code == 422


def test_modify_rejects_paper_and_unknown_accounts(client: TestClient, db_session, isolated_db) -> None:
    paper = BrokerAccount(name="Paper Modify", broker="fyers", paper_mode=True, enabled=True)
    db_session.add(paper)
    db_session.commit()
    r = client.post("/api/orders/modify", json={"account_id": paper.id, "broker_order_id": "X", "limit_price": 1})
    assert r.status_code == 400 and "paper account" in r.json()["detail"]
    r = client.post("/api/orders/modify", json={"account_id": 99999, "broker_order_id": "X", "limit_price": 1})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Broker outcomes
# ---------------------------------------------------------------------------


def test_modify_broker_refusal_leaves_rows_alone(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    _install(real_account.id, _ModifyStub(ok=False, message="order is not in a modifiable state"))
    _trade(db_session, real_account.id)
    r = _modify(client, real_account.id, quantity=5)
    assert r.status_code == 200
    assert r.json() == {
        "ok": False, "broker_order_id": "ORD-1",
        "message": "order is not in a modifiable state", "rows_updated": 0,
    }
    assert [t.quantity for t in _rows(db_session)] == [10]
    assert _audits(db_session) == []


def test_modify_blocked_edge_is_503_other_errors_502(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    _trade(db_session, real_account.id)
    _install(real_account.id, _ModifyStub(raises=FyersBlockedError("blocked", status_code=403)))
    r = _modify(client, real_account.id, limit_price=600.0)
    assert r.status_code == 503 and "blocking" in r.json()["detail"]

    _install(real_account.id, _ModifyStub(raises=RuntimeError("socket closed")))
    r = _modify(client, real_account.id, limit_price=600.0)
    assert r.status_code == 502 and "socket closed" in r.json()["detail"]
    assert [t.price for t in _rows(db_session)] == [612.5]
    assert _audits(db_session) == []


def test_modify_needs_a_modify_capable_backend(client: TestClient, db_session, isolated_db, real_account) -> None:
    class _NoModify:
        async def cancel_order(self, broker_order_id: str) -> bool:
            return True

    _install(real_account.id, _NoModify())
    _trade(db_session, real_account.id)
    r = _modify(client, real_account.id, limit_price=600.0)
    assert r.status_code == 400 and r.json()["detail"] == "this broker does not support modify"


# ---------------------------------------------------------------------------
# FyersLiveBackend.modify_order → PATCH /orders/sync
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[tuple[str, str, Any]] = []

    async def __call__(self, method, path, *, params=None, json_body=None, base_url=None):  # noqa: ANN001
        self.calls.append((method, path, json_body))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


_OK = {"s": "ok", "code": 1102, "message": "Successfully modified order", "id": "ORD-1"}


def _backend(monkeypatch, response: Any = _OK):
    client = FyersClient(app_id="APP-100", access_token="TOK")
    rec = _Recorder(response)
    monkeypatch.setattr(client, "_request", rec)
    return FyersLiveBackend(app_id="APP-100", access_token="TOK", broker_account_id=1, client=client), rec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, body",
    [
        # price-only: just the fields given
        ({"limit_price": 611.0}, {"id": "ORD-1", "limitPrice": 611.0}),
        ({"quantity": 4, "stop_price": 600}, {"id": "ORD-1", "qty": 4, "stopPrice": 600.0}),
        # a type change uses place_order's codes and zeroes unused prices
        ({"quantity": 5, "limit_price": 611.0, "order_type": OrderType.LIMIT},
         {"id": "ORD-1", "qty": 5, "type": 1, "limitPrice": 611.0, "stopPrice": 0.0}),
        ({"order_type": OrderType.MARKET, "limit_price": 611.0},
         {"id": "ORD-1", "type": 2, "limitPrice": 0.0, "stopPrice": 0.0}),
        ({"order_type": "SL-M", "stop_price": 600.0},
         {"id": "ORD-1", "type": 3, "limitPrice": 0.0, "stopPrice": 600.0}),
        ({"order_type": OrderType.STOP_LOSS, "limit_price": 601.0, "stop_price": 600.0},
         {"id": "ORD-1", "type": 4, "limitPrice": 601.0, "stopPrice": 600.0}),
    ],
)
async def test_backend_modify_payloads(monkeypatch, kwargs: dict, body: dict) -> None:
    be, rec = _backend(monkeypatch)
    assert await be.modify_order("ORD-1", **kwargs) == (True, "Successfully modified order")
    assert rec.calls == [("PATCH", "/orders/sync", body)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs, needle",
    [
        ({}, "nothing to modify"),
        ({"quantity": 0}, "quantity must be > 0"),
        ({"order_type": OrderType.LIMIT}, "limit price"),
        ({"order_type": OrderType.STOP_LOSS, "limit_price": 601.0}, "stop/trigger price"),
        ({"order_type": "ICEBERG"}, "unknown order_type"),
    ],
)
async def test_backend_modify_refuses_locally(monkeypatch, kwargs: dict, needle: str) -> None:
    be, rec = _backend(monkeypatch)
    ok, msg = await be.modify_order("ORD-1", **kwargs)
    assert ok is False and needle in msg
    assert rec.calls == []


@pytest.mark.asyncio
async def test_backend_modify_broker_answers(monkeypatch) -> None:
    be, _ = _backend(monkeypatch, FyersAPIError("fyers 400: invalid price", status_code=400))
    assert await be.modify_order("ORD-1", limit_price=1.0) == (False, "fyers 400: invalid price")

    be, _ = _backend(monkeypatch, {"s": "error", "code": -50, "message": "not modifiable"})
    assert await be.modify_order("ORD-1", limit_price=1.0) == (False, "not modifiable")

    be, _ = _backend(monkeypatch, FyersBlockedError("blocked", status_code=403))
    with pytest.raises(FyersBlockedError):
        await be.modify_order("ORD-1", limit_price=1.0)


def test_modify_endpoint_end_to_end_on_the_wire(
    client: TestClient, db_session, isolated_db, real_account
) -> None:
    """The real FyersLiveBackend behind the endpoint sends a PATCH with a
    JSON body to /api/v3/orders/sync (httpx MockTransport, no network)."""
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=_OK)

    fy = FyersClient(app_id="APP123", access_token="AT123", transport=httpx.MockTransport(handler))
    _install(real_account.id, FyersLiveBackend(
        app_id="APP123", access_token="AT123", broker_account_id=real_account.id, client=fy,
    ))
    _trade(db_session, real_account.id)
    r = _modify(client, real_account.id, quantity=3, order_type="LIMIT", limit_price=610.0)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True and r.json()["message"] == "Successfully modified order"
    assert [(q.method, q.url.path) for q in seen] == [("PATCH", "/api/v3/orders/sync")]
    import json

    assert json.loads(seen[0].content) == {
        "id": "ORD-1", "qty": 3, "type": 1, "limitPrice": 610.0, "stopPrice": 0.0,
    }
    assert [(t.quantity, t.price) for t in _rows(db_session)] == [(3, 610.0)]
