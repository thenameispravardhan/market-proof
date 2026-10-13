"""Trade-page practice mode: account -1 fills on the paper backend in its own
DB — never the live trades / positions tables, never Fyers."""
from __future__ import annotations

from app.api import practice


def test_practice_order_lands_only_in_the_practice_book(client, monkeypatch, isolated_db):
    practice._backend = None  # fresh backend on this test's loop

    async def fake_quote(*, symbol, db):  # noqa: ARG001
        await practice.backend()._md.publish(symbol, 100.0)
        return {"ok": True, "last_price": 100.0}

    monkeypatch.setattr("app.api.orders.get_quote", fake_quote)
    r = client.post("/api/orders", json={"account_id": -1, "symbol": "NSE:SBIN-EQ", "side": "BUY",
                                         "quantity": 5, "order_type": "MARKET"}).json()
    assert r["ok"] and r["practice"] and r["status"] == "FILLED"
    pos = client.get("/api/positions?practice=1").json()
    assert [(p["symbol"], p["quantity"]) for p in pos] == [("NSE:SBIN-EQ", 5)]
    assert all(p["symbol"] != "NSE:SBIN-EQ" for p in client.get("/api/positions").json())
    assert all(t["symbol"] != "NSE:SBIN-EQ" for t in client.get("/api/trades").json())

    assert client.post("/api/positions/NSE:SBIN-EQ/close?practice=1").json()["ok"]
    trades = client.get("/api/trades?practice=1&status=filled").json()
    assert sorted(t["side"] for t in trades) == ["BUY", "SELL"]
    assert client.get("/api/positions?practice=1").json()[0]["quantity"] == 0
