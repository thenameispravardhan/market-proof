"""Trade-page practice mode: account -1 fills on the paper backend in its own
DB — never the live trades / positions tables, never Fyers."""
from __future__ import annotations

import pytest

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


@pytest.mark.asyncio
async def test_practice_fills_at_touch_and_fires_its_stop(monkeypatch, isolated_db):
    from app.db.models import Position

    practice._backend = None
    px = {"last": 100.0, "bid": 99.9, "ask": 100.1}

    async def fake_quote(symbol):
        await practice.backend()._md.publish(symbol, px["last"], bid=px["bid"], ask=px["ask"])
        return {"ok": True, "last_price": px["last"]}

    monkeypatch.setattr(practice, "_quote", fake_quote)
    r = await practice.place(symbol="NSE:INFY-EQ", side="BUY", quantity=10, order_type="MARKET",
                             limit_price=None, stop_price=None, product_type="INTRADAY",
                             stop_loss=98.0, target=105.0)
    assert r["status"] == "FILLED"
    with practice.session_factory()() as db:
        pos = db.query(Position).filter_by(symbol="NSE:INFY-EQ").one()
        assert pos.average_price == 100.1 and (pos.stop_loss, pos.target) == (98.0, 105.0)  # BUY at ask
    px.update(last=97.5, bid=97.4, ask=97.6)
    await practice.backend()._md.publish("NSE:INFY-EQ", 97.5, bid=97.4, ask=97.6)   # the websocket tick
    await practice._check_levels()
    with practice.session_factory()() as db:
        pos = db.query(Position).filter_by(symbol="NSE:INFY-EQ").one()
        assert pos.quantity == 0 and pos.stop_loss is None      # stopped out, disarmed
    practice._watcher.cancel()
