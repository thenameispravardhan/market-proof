"""Tests for the Fyers order postback endpoint.

A Fyers order-update reconciles an existing trade (by broker_order_id)
and must NEVER create a new signal (no re-trade loop). Auth is the
?token= query param matching the configured secret.
"""
from __future__ import annotations

from app.db.models import Position, Signal, Trade


def _seed_open_trade(client, order_id: str = "ORD-123") -> str:
    """Create a placed trade via the DB so the postback can reconcile it."""
    from app.db import session as db_session_mod

    with db_session_mod.SessionLocal() as s:
        t = Trade(
            symbol="SBIN",
            side="BUY",
            quantity=10,
            price=600.0,
            order_type="market",
            status="placed",
            broker_order_id=order_id,
        )
        s.add(t)
        s.commit()
    return order_id


def _set_secret(client, secret: str) -> None:
    from app.db import session as db_session_mod
    from app.db.infra_models import AppSetting
    import json

    with db_session_mod.SessionLocal() as s:
        row = s.get(AppSetting, "FYERS_POSTBACK_SECRET")
        if row is None:
            s.add(AppSetting(key="FYERS_POSTBACK_SECRET", value=json.dumps(secret)))
        else:
            row.value = json.dumps(secret)
        s.commit()


def test_config_endpoint_reports_url(client):
    r = client.post(
        "/api/fyers/postback/config",
        json={"public_base_url": "https://bot.example.com", "regenerate_secret": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["secret"]
    assert body["url"].startswith("https://bot.example.com/api/fyers/postback?token=")
    assert body["configured"] is True


def test_postback_reconciles_filled_order(client):
    order_id = _seed_open_trade(client)
    _set_secret(client, "sek")
    r = client.post(
        "/api/fyers/postback?token=sek",
        json={
            "id": order_id,
            "status": "2",  # FILLED
            "symbol": "NSE:SBIN-EQ",
            "transactionType": "BUY",
            "qty": 10,
            "tradedPrice": 612.5,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is True
    assert body["status"] == "filled"

    from app.db import session as db_session_mod

    with db_session_mod.SessionLocal() as s:
        t = s.query(Trade).filter_by(broker_order_id=order_id).one()
        assert t.status == "filled"
        assert t.price == 612.5
        # Position mirrored.
        pos = s.query(Position).filter_by(symbol="SBIN").one()
        assert pos.quantity == 10
        # CRITICAL: no signal was created from the broker postback.
        assert s.query(Signal).count() == 0


def test_postback_reconciles_dict_wrapped_order(client):
    """Fyers also wraps the order as ``{"s": "ok", "orders": {<order>}}``
    (a single dict — the order-WebSocket shape). The postback receiver
    must unwrap that too, not just the list form."""
    order_id = _seed_open_trade(client, order_id="ORD-DICT-456")
    _set_secret(client, "sek")
    r = client.post(
        "/api/fyers/postback?token=sek",
        json={
            "s": "ok",
            "orders": {
                "id": order_id,
                "status": "2",  # FILLED
                "symbol": "NSE:SBIN-EQ",
                "transactionType": "BUY",
                "qty": 10,
                "tradedPrice": 612.5,
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] is True
    assert body["status"] == "filled"

    from app.db import session as db_session_mod

    with db_session_mod.SessionLocal() as s:
        t = s.query(Trade).filter_by(broker_order_id=order_id).one()
        assert t.status == "filled"
        assert t.price == 612.5


def test_postback_bad_token_rejected(client):
    _set_secret(client, "right")
    r = client.post(
        "/api/fyers/postback?token=wrong",
        json={"id": "X", "status": "2", "symbol": "NSE:SBIN-EQ"},
    )
    assert r.status_code == 401


def test_postback_unmatched_order_is_acknowledged(client):
    _set_secret(client, "sek")
    r = client.post(
        "/api/fyers/postback?token=sek",
        json={"id": "UNKNOWN-999", "status": "2", "symbol": "NSE:TCS-EQ"},
    )
    assert r.status_code == 200
    assert r.json()["matched"] is False
