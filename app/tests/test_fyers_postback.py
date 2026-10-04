"""Fyers order postback: closed without a secret, token-checked, reconciles."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings


def test_postback_closed_then_guarded_then_reconciles(client: TestClient, monkeypatch) -> None:
    body = {"s": "ok", "orders": {"id": "X-404", "status": 2, "symbol": "NSE:SBIN-EQ", "tradedPrice": 800}}
    monkeypatch.setattr(get_settings(), "FYERS_POSTBACK_SECRET", "")
    assert client.post("/api/fyers/postback?token=", json=body).status_code == 503

    monkeypatch.setattr(get_settings(), "FYERS_POSTBACK_SECRET", "s3cret")
    assert client.post("/api/fyers/postback?token=nope", json=body).status_code == 401
    assert client.post("/api/fyers/postback?token=s3cret", content=b"not json").status_code == 400
    r = client.post("/api/fyers/postback?token=s3cret", json=body)
    assert r.status_code == 200 and r.json() == {"ok": True, "matched": False, "order_id": "X-404", "status": "filled"}

    url = client.get("/api/fyers/postback/url").json()
    assert url["configured"] is True and url["url"].endswith("/api/fyers/postback?token=s3cret")
