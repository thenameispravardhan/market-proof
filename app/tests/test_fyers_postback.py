"""Fyers order postback: the payload is only a nudge — truth comes from Fyers."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api import market
from app.db import session as db_session
from app.db.models import Trade


def test_postback_reconciles_from_fyers_not_from_the_payload(client: TestClient, monkeypatch) -> None:
    with db_session.SessionLocal() as db:
        db.add(Trade(symbol="SBIN-EQ", side="BUY", quantity=1, price=800.0, status="placed", broker_order_id="OID-1"))
        db.commit()
    calls: list[str] = []

    class _Backend:
        async def get_order_status(self, oid: str):
            calls.append(oid)
            return SimpleNamespace(raw={"id": oid, "status": 2, "tradedPrice": 801.5, "symbol": "NSE:SBIN-EQ"}, error=None)

    monkeypatch.setattr(market, "_fyers_backend", lambda: _Backend())
    # Unknown ids never reach Fyers.
    r = client.post("/api/fyers/postback", json={"orders": {"id": "NOT-OURS", "status": 2}})
    assert r.json() == {"ok": True, "matched": False, "order_id": "NOT-OURS"} and calls == []
    # A forged "cancelled at ₹1" for our order is ignored: Fyers says filled at 801.5.
    r = client.post("/api/fyers/postback", json={"s": "ok", "orders": {"id": "OID-1", "status": 1, "tradedPrice": 1}})
    assert r.status_code == 200 and r.json()["status"] == "filled" and calls == ["OID-1"]
    with db_session.SessionLocal() as db:
        t = db.query(Trade).filter(Trade.broker_order_id == "OID-1").one()
        assert (t.status, t.price) == ("filled", 801.5)
    assert client.post("/api/fyers/postback", content=b"nope").status_code == 400
