"""Fyers order updates: postback (payload = nudge, truth from Fyers), status
codes, the update-beats-its-own-row race, and the cancel endpoint."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from app.api import fyers_postback, market
from app.db import session as db_session
from app.db.models import Trade
from app.execution import order_reconcile
from app.execution.fyers_live import FyersClient, _state_from_str
from app.execution.base import OrderState


def _trade(oid: str, status: str = "placed") -> None:
    with db_session.SessionLocal() as db:
        db.add(Trade(symbol="INFY-EQ", side="BUY", quantity=1, price=0.0, status=status, broker_order_id=oid))
        db.commit()


def _status(oid: str) -> str:
    with db_session.SessionLocal() as db:
        return db.query(Trade).filter(Trade.broker_order_id == oid).one().status


class _Backend:
    def __init__(self, code: int, price: float = 0.0) -> None:
        self.code, self.price, self.calls = code, price, []

    async def get_order_status(self, oid: str):
        self.calls.append(oid)
        return SimpleNamespace(raw={"id": oid, "status": self.code, "tradedPrice": self.price, "symbol": "NSE:INFY-EQ"}, error=None)


def test_fyers_v3_status_codes() -> None:
    # 1 cancelled, 2 traded, 4 transit, 5 rejected, 6 pending, 7 expired
    assert [order_reconcile._status_text(c) for c in (1, 2, 4, 5, 6, 7)] == ["CANCELLED", "FILLED", "TRANSIT", "REJECTED", "PENDING", "EXPIRED"]
    assert [_state_from_str(c) for c in ("4", "5", "6", "7")] == [OrderState.PENDING, OrderState.REJECTED, OrderState.PENDING, OrderState.EXPIRED]


def test_postback_trusts_fyers_not_the_payload(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(fyers_postback, "UNMATCHED_RETRY_S", 0)
    _trade("OID-1")
    be = _Backend(code=2, price=801.5)
    monkeypatch.setattr(market, "_fyers_backend", lambda: be)
    assert client.post("/api/fyers/postback", json={"orders": {"id": "NOT-OURS", "status": 2}}).json()["matched"] is False
    assert be.calls == []  # unknown ids never reach Fyers
    r = client.post("/api/fyers/postback", json={"s": "ok", "orders": {"id": "OID-1", "status": 1, "tradedPrice": 1}})
    assert r.json()["status"] == "filled" and be.calls == ["OID-1"]
    assert client.post("/api/fyers/postback", content=b"nope").status_code == 400


def test_postback_rejection_that_beats_its_own_row(client: TestClient, monkeypatch) -> None:
    """Fyers rejected within ms, before the placing request committed the row."""
    monkeypatch.setattr(market, "_fyers_backend", lambda: _Backend(code=5))
    real_sleep = asyncio.sleep

    async def row_lands_while_we_wait(_s: float) -> None:
        _trade("OID-RACE")
        await real_sleep(0)

    monkeypatch.setattr(fyers_postback.asyncio, "sleep", row_lands_while_we_wait)
    r = client.post("/api/fyers/postback", json={"orders": {"id": "OID-RACE", "status": 5}})
    assert r.json()["status"] == "rejected" and _status("OID-RACE") == "rejected"


def test_ws_update_before_row_is_retried_and_no_resurrection(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(order_reconcile, "UNMATCHED_RETRY_S", 0.05)

    async def scenario() -> None:
        with db_session.SessionLocal() as db:
            r = await order_reconcile.reconcile_order_update(db, {"orders": {"id": "OID-WS", "status": 5}}, source="test")
        assert r["matched"] is False
        _trade("OID-WS")  # the placing request commits after the update
        await asyncio.sleep(0.2)
        assert _status("OID-WS") == "rejected"
        with db_session.SessionLocal() as db:  # a late "pending" echo can't bring it back
            await order_reconcile.reconcile_order_update(db, {"id": "OID-WS", "status": 6}, source="test", retry=False)
        assert _status("OID-WS") == "rejected"

    asyncio.run(scenario())


def test_cancel_uses_v3_sync_endpoint() -> None:
    seen: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(method=req.method, path=req.url.path, body=req.content)
        return httpx.Response(200, json={"s": "ok", "code": 1103, "id": "OID-9"})

    async def go() -> None:
        c = FyersClient(app_id="X-100", access_token="t", transport=httpx.MockTransport(handler))
        await c.cancel_order("OID-9")

    asyncio.run(go())
    assert seen["method"] == "DELETE" and seen["path"].endswith("/orders/sync") and b'"OID-9"' in seen["body"]
