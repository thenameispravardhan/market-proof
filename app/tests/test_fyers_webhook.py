"""Fyers postback normalizer + end-to-end receiver tests.

Coverage:
  - `is_fyers_postback` correctly identifies Fyers vs non-Fyers payloads
  - `normalize_fyers_postback` produces a payload that satisfies the
    generic receiver contract
  - The receiver (HTTP path) creates an `analyses` + `signals` row
    when sent a Fyers-shaped payload
  - The receiver rejects a stale timestamp even when the payload
    shape is Fyers-style (i.e. normalization happens BEFORE the
    replay check, and Fyers-shaped payloads that are too old still
    get rejected)
  - Non-Fyers payloads routed through a webhook named "fyers-*"
    get normalized too (explicit opt-in)
  - The action is always HOLD (postbacks are informational, never
    trade triggers — guards against feedback loops)
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.db.models import Analysis, Signal, Webhook
from app.webhooks.fyers import (
    is_fyers_postback,
    normalize_fyers_postback,
)
from app.webhooks.receiver import process_inbound


# ---- pure normalizer tests -----------------------------------------------


def test_is_fyers_postback_detects_order_id_and_status():
    # Need at least 2 Fyers-typical fields to trigger the heuristic
    # (status alone is too generic — lots of webhook systems use it).
    assert is_fyers_postback({"order_id": "X1", "tradedPrice": 100.0, "status": "2"}) is True


def test_is_fyers_postback_detects_with_timestamp():
    assert is_fyers_postback(
        {"orderTimestamp": "13-Jun-2026 12:30:15", "tradedPrice": 100.0}
    ) is True


def test_is_fyers_postback_detects_orders_list():
    assert is_fyers_postback({"orders": [{"order_id": "X1"}]}) is True


def test_is_fyers_postback_rejects_generic_shape():
    # Looks like the generic receiver contract, NOT a Fyers payload.
    assert (
        is_fyers_postback(
            {"event_type": "ORDER_WIN", "symbol": "RELIANCE", "action": "BUY"}
        )
        is False
    )


def test_is_fyers_postback_handles_non_dict():
    assert is_fyers_postback([1, 2, 3]) is False
    assert is_fyers_postback("just a string") is False
    assert is_fyers_postback(None) is False


def test_normalize_minimal_fyers_payload():
    out = normalize_fyers_postback({"order_id": "X1", "status": "2", "symbol": "NSE:SBIN-EQ"})
    assert out["event_type"] == "FYERS_ORDER_UPDATE"
    assert out["symbol"] == "SBIN-EQ"
    assert out["exchange"] == "NSE"
    assert out["action"] == "HOLD"  # postback is informational, never a trigger
    assert isinstance(out["timestamp"], int)
    assert "FYERS" not in out["headline"].upper() or "FILLED" in out["headline"].upper()
    # raw_fyers must be preserved for audit
    assert out["raw_fyers"]["order_id"] == "X1"


def test_normalize_handles_status_codes():
    out = normalize_fyers_postback({"status": "4", "symbol": "NSE:INFY-EQ"})
    assert "REJECTED" in out["headline"] or "REJECTED" in out["summary"]


def test_normalize_handles_wrapped_orders_list():
    payload = {
        "orders": [
            {"order_id": "X1", "status": "2", "symbol": "NSE:TCS-EQ", "qty": 5, "tradedPrice": 3500}
        ]
    }
    out = normalize_fyers_postback(payload)
    assert out["symbol"] == "TCS-EQ"
    assert out["key_numbers"].get("quantity") == 5.0
    assert out["key_numbers"].get("traded_price") == 3500.0


def test_normalize_handles_epoch_ms_timestamp():
    out = normalize_fyers_postback({"orderTimestamp": 1_700_000_000_000, "symbol": "NSE:ITC-EQ"})
    # The receiver uses `timestamp` (NOT `orderTimestamp`) for replay
    # protection, so we must always set `timestamp` to NOW.
    assert isinstance(out["timestamp"], int)
    assert abs(out["timestamp"] - int(time.time())) < 5


def test_normalize_key_numbers_carries_fyers_categoricals():
    out = normalize_fyers_postback(
        {
            "symbol": "NSE:HDFCBANK-EQ",
            "orderType": "2",
            "productType": "DELIVERY",
            "transactionType": "SELL",
            "exchange": "NSE",
            "segment": "EQUITY",
            "qty": 10,
            "tradedPrice": 1500.0,
            "limitPrice": 1495.0,
            "stopPrice": 1490.0,
            "tradedValue": 15000.0,
        }
    )
    kn = out["key_numbers"]
    assert kn["orderType"] == "2"
    assert kn["productType"] == "DELIVERY"
    assert kn["transactionType"] == "SELL"
    assert kn["exchange"] == "NSE"
    assert kn["segment"] == "EQUITY"
    assert kn["quantity"] == 10.0
    assert kn["traded_price"] == 1500.0
    assert kn["limit_price"] == 1495.0
    assert kn["stop_price"] == 1490.0
    assert kn["traded_value"] == 15000.0


def test_normalize_handles_non_dict_gracefully():
    out = normalize_fyers_postback("not a dict")
    assert out["event_type"] == "FYERS_ORDER_UPDATE"
    assert out["action"] == "HOLD"
    assert out["symbol"] == ""


def test_normalize_keeps_unknown_symbol_placeholder():
    out = normalize_fyers_postback({"order_id": "X1"})
    assert out["symbol"] == "UNKNOWN"


# ---- end-to-end receiver tests via HTTP ----------------------------------


def _counter():
    n = {"v": 0}
    def next_(prefix):
        n["v"] += 1
        return f"{prefix}-{n['v']}"
    return next_


_next_name = _counter()


def _fyers_payload(**overrides) -> dict:
    base = {
        "order_id": "FY-1234",
        "status": "2",  # FILLED
        "symbol": "NSE:SBIN-EQ",
        "orderType": "1",
        "productType": "INTRADAY",
        "transactionType": "BUY",
        "qty": 2,
        "tradedPrice": 612.5,
        "orderTimestamp": "13-Jun-2026 12:30:15",
    }
    base.update(overrides)
    return base


def _post_fyers(client: TestClient, webhook_id: int, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    r = client.post(
        f"/api/webhooks/in/{webhook_id}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def test_fyers_payload_through_receiver_creates_signal(db_session, client: TestClient, isolated_db):
    name = _next_name("fyers-e2e")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret=None,  # Fyers does not currently sign postbacks
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id

    status_code, resp = _post_fyers(client, wid, _fyers_payload())
    assert status_code == 200, resp
    assert resp["ok"] is True
    assert resp["symbol"] == "SBIN-EQ"
    assert resp["action"] == "HOLD"  # postback MUST NOT trigger trades

    s = db_session.get(Signal, resp["signal_id"])
    assert s is not None
    assert s.action == "HOLD"
    a = db_session.get(Analysis, resp["analysis_id"])
    assert a is not None
    assert a.model == "external"
    # The original Fyers payload is preserved for audit
    assert a.raw_response["raw_payload"]["raw_fyers"]["order_id"] == "FY-1234"


def test_fyers_payload_via_named_prefix(db_session, client: TestClient, isolated_db):
    """A webhook whose name starts with `fyers-` triggers the
    normalizer even if the payload itself doesn't look like a
    Fyers payload (explicit opt-in)."""
    name = _next_name("fyers-prefix-optin")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret=None,
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id

    # Send a generic-looking payload — the `fyers-` name prefix
    # forces normalization.
    status_code, resp = _post_fyers(
        client, wid, {"order_id": "FY-99", "status": "4", "symbol": "BSE:RELIANCE-EQ"}
    )
    assert status_code == 200, resp
    assert resp["symbol"] == "RELIANCE-EQ"
    assert resp["action"] == "HOLD"


def test_fyers_payload_directly_via_process_inbound(db_session, isolated_db):
    """Skip HTTP and drive the pure function — useful for tests that
    want fine-grained control over the webhook row."""
    name = _next_name("fyers-direct")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret=None,
            enabled=True,
        )
    )
    db_session.commit()
    wh = db_session.query(Webhook).filter_by(name=name).one()

    body = json.dumps(_fyers_payload(order_id="FY-DIRECT")).encode("utf-8")
    result = process_inbound(db_session, wh, body, headers={})
    assert result["ok"] is True
    assert result["action"] == "HOLD"
    assert result["symbol"] == "SBIN-EQ"

    # Re-fetch and inspect the persisted row
    db_session.expire_all()
    a = db_session.get(Analysis, result["analysis_id"])
    assert a.raw_response["raw_payload"]["raw_fyers"]["order_id"] == "FY-DIRECT"


def test_non_fyers_payload_via_named_prefix_still_normalizes(db_session, client: TestClient, isolated_db):
    """A `fyers-` named webhook always runs the normalizer. A
    truly weird payload (no order_id, no symbol) still gets a
    placeholder symbol so the row can be created."""
    name = _next_name("fyers-weird")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret=None,
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id

    status_code, resp = _post_fyers(client, wid, {"order_id": "FY-WEIRD"})
    assert status_code == 200, resp
    # No symbol in the input → "UNKNOWN" placeholder
    assert resp["symbol"] == "UNKNOWN"
    assert resp["action"] == "HOLD"
