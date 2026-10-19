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


def test_practice_limit_order_drags_and_arms_its_levels(client, monkeypatch, isolated_db):
    """The chart drags a resting paper order: its price moves, and the SL / TP
    pulled out of its tag show on /pending and arm when it fills."""
    practice._backend = None
    practice.PENDING_LEVELS.clear()

    async def fake_quote(*, symbol, db):  # noqa: ARG001
        await practice.backend()._md.publish(symbol, 100.0)
        return {"ok": True, "last_price": 100.0}

    monkeypatch.setattr("app.api.orders.get_quote", fake_quote)
    r = client.post("/api/orders", json={"account_id": -1, "symbol": "NSE:SBIN-EQ", "side": "BUY",
                                         "quantity": 5, "order_type": "LIMIT", "limit_price": 95}).json()
    oid = r["broker_order_id"]
    assert r["status"] == "PENDING"
    m = client.post("/api/orders/modify", json={"account_id": -1, "broker_order_id": oid,
                                                "limit_price": 96, "stop_loss": 94, "target": 99})
    assert m.status_code == 200 and m.json()["ok"]
    o = client.get("/api/orders/pending?account_id=-1").json()["orders"][0]
    assert (o["price"], o["stop_loss"], o["target"]) == (96.0, 94.0, 99.0)
    assert practice.backend()._pending[oid].limit_price == 96.0
    client.post("/api/orders/modify", json={"account_id": -1, "broker_order_id": oid, "target": None})
    assert client.get("/api/orders/pending?account_id=-1").json()["orders"][0]["target"] is None
    assert oid not in practice.PENDING_LEVELS or practice.PENDING_LEVELS[oid][2] is None


def test_resting_order_survives_a_restart_and_prices_snap_to_the_tick(client, monkeypatch, isolated_db):
    """2026-10-19: a deploy emptied the paper engine; the SL-M row stayed
    "placed" and 21 cancels returned ok:false. Also 121.53 is not an NSE price."""
    practice._backend = None

    async def fake_quote(*, symbol, db):  # noqa: ARG001
        await practice.backend()._md.publish(symbol, 100.0)
        return {"ok": True, "last_price": 100.0}

    monkeypatch.setattr("app.api.orders.get_quote", fake_quote)
    from app.services.instrument_master import get_master
    monkeypatch.setattr(get_master(), "get", lambda s: None)      # unknown instrument -> NSE's 0.05
    r = client.post("/api/orders", json={"account_id": -1, "symbol": "NSE:SBIN-EQ", "side": "BUY",
                                         "quantity": 5, "order_type": "LIMIT", "limit_price": 95.03}).json()
    oid = r["broker_order_id"]
    mine = lambda: next(o for o in client.get("/api/orders/pending?account_id=-1").json()["orders"] if o["broker_order_id"] == oid)  # noqa: E731
    assert mine()["price"] == 95.05
    practice._backend = None                                   # "restart"
    m = client.post("/api/orders/modify", json={"account_id": -1, "broker_order_id": oid, "limit_price": 94.02})
    assert m.status_code == 200                                # back on the book, not a ghost
    assert mine()["price"] == 94.0
    assert client.post("/api/orders/cancel", json={"account_id": -1, "broker_order_id": oid}).json()["ok"]
    assert oid not in [o["broker_order_id"] for o in client.get("/api/orders/pending?account_id=-1").json()["orders"]]

    # a row the engine never held (an SL-L lost in a restart) still cancels
    from app.db.models import Trade
    with practice.session_factory()() as db:
        db.add(Trade(symbol="NSE:SBIN-EQ", side="BUY", quantity=1, price=99.0, order_type="STOP_LOSS",
                     status="placed", broker_order_id="PAPER-ghost"))
        db.commit()
    assert client.post("/api/orders/cancel", json={"account_id": -1, "broker_order_id": "PAPER-ghost"}).json()["ok"]


def test_an_index_cannot_be_ordered(client, isolated_db):
    """NIFTY 50 / India VIX are indices: no BUY / SELL, paper or real."""
    for sym in ("NSE:NIFTY50-INDEX", "NSE:INDIAVIX-INDEX"):
        r = client.post("/api/orders", json={"account_id": -1, "symbol": sym, "side": "BUY", "quantity": 1, "order_type": "MARKET"})
        assert r.status_code == 422 and "index" in r.json()["detail"]
