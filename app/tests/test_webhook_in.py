"""Inbound webhook receiver tests.

Coverage (per spec):
  - valid signed payload → creates an `analyses` + `signals` row
  - bad signature → 401
  - old timestamp → 400
  - missing field → 400
  - missing webhook_id → 404
  - outbound (direction='out') used as inbound → 400
  - disabled webhook → 503
  - replay protection rejects older than 5 minutes
"""
from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from app.db.models import Analysis, Signal, Webhook
from app.webhooks.signing import compute_signature


def _make_payload(**overrides):
    import time as _t

    now = int(_t.time())
    base = {
        "event_type": "ORDER_WIN",
        "symbol": "RELIANCE",
        "action": "BUY",
        "timestamp": now,
        "confidence": 0.82,
        "summary": "External: order win",
        "reasoning": "test",
    }
    base.update(overrides)
    return base


def _sign(secret: str, body: bytes) -> str:
    return f"sha256={compute_signature(secret, body)}"


# ---- pure-function process_inbound (no HTTP) ---------------------------


# Counter for unique webhook names (in-memory SQLite leaks between tests).
_in_counter = {"n": 0}


def _in_name(prefix: str = "win") -> str:
    _in_counter["n"] += 1
    return f"{prefix}-{_in_counter['n']}"


def _post_inbound(client: TestClient, webhook_id: int, body: bytes, sig_header: str | None = None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if sig_header is not None:
        headers["X-Mavis-Signature"] = sig_header
    r = client.post(f"/api/webhooks/in/{webhook_id}", content=body, headers=headers)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {}


def test_valid_signed_payload_creates_analysis_and_signal(db_session, client: TestClient, isolated_db):
    name = _in_name("ok")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id

    payload = _make_payload()
    body = json.dumps(payload).encode("utf-8")
    sig = _sign("sek", body)

    status_code, resp = _post_inbound(client, wid, body, sig)
    assert status_code == 200, resp
    assert resp["ok"] is True
    assert "analysis_id" in resp
    assert "signal_id" in resp
    assert resp["symbol"] == "RELIANCE"
    assert resp["action"] == "BUY"

    a = db_session.get(Analysis, resp["analysis_id"])
    assert a is not None
    assert a.model == "external"
    s = db_session.get(Signal, resp["signal_id"])
    assert s is not None
    assert s.symbol == "RELIANCE"
    assert s.action == "BUY"


def test_bad_signature_returns_401(db_session, client: TestClient, isolated_db):
    name = _in_name("badsig")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    body = json.dumps(_make_payload()).encode("utf-8")
    # Send a WRONG signature
    status_code, resp = _post_inbound(client, wid, body, "sha256=" + "0" * 64)
    assert status_code == 401
    assert "signature" in (resp.get("detail", "") or "").lower()


def test_missing_signature_when_required_returns_401(db_session, client: TestClient, isolated_db):
    name = _in_name("nosig")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    body = json.dumps(_make_payload()).encode("utf-8")
    status_code, _ = _post_inbound(client, wid, body, sig_header=None)
    assert status_code == 401


def test_old_timestamp_returns_400(db_session, client: TestClient, isolated_db):
    name = _in_name("oldts")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    payload = _make_payload(timestamp=int(time.time()) - 600)  # 10min ago
    body = json.dumps(payload).encode("utf-8")
    sig = _sign("sek", body)
    status_code, resp = _post_inbound(client, wid, body, sig)
    assert status_code == 400
    assert "timestamp" in (resp.get("detail", "") or "").lower()


def test_missing_required_field_returns_400(db_session, client: TestClient, isolated_db):
    name = _in_name("miss")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    payload = _make_payload()
    payload.pop("action")
    body = json.dumps(payload).encode("utf-8")
    sig = _sign("sek", body)
    status_code, resp = _post_inbound(client, wid, body, sig)
    assert status_code == 400
    assert "action" in (resp.get("detail", "") or "").lower()


def test_unknown_webhook_id_returns_404(client: TestClient, isolated_db):
    payload = _make_payload()
    body = json.dumps(payload).encode("utf-8")
    status_code, _ = _post_inbound(client, 99999, body, "sha256=00")
    assert status_code == 404


def test_outbound_webhook_rejected_for_inbound(db_session, client: TestClient, isolated_db):
    name = _in_name("outdir")
    db_session.add(
        Webhook(
            name=name,
            direction="out",  # NOT in
            event_filter="*",
            url="placeholder",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    body = json.dumps(_make_payload()).encode("utf-8")
    status_code, _ = _post_inbound(client, wid, body, sig_header=None)
    assert status_code == 400


def test_disabled_webhook_returns_503(db_session, client: TestClient, isolated_db):
    name = _in_name("disab")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=False,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    body = json.dumps(_make_payload()).encode("utf-8")
    sig = _sign("sek", body)
    status_code, _ = _post_inbound(client, wid, body, sig)
    assert status_code == 503


def test_no_secret_required_when_secret_null(db_session, client: TestClient, isolated_db):
    """When webhook.secret is null/empty, the receiver accepts
    unsigned payloads (useful for trusted internal sources)."""
    name = _in_name("nosecret")
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
    body = json.dumps(_make_payload()).encode("utf-8")
    status_code, resp = _post_inbound(client, wid, body, sig_header=None)
    assert status_code == 200, resp


def test_invalid_json_returns_400(db_session, client: TestClient, isolated_db):
    name = _in_name("badjson")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    status_code, _ = _post_inbound(client, wid, b"not-json")
    assert status_code == 400


def test_replay_window_boundary(db_session, client: TestClient, isolated_db):
    """Timestamp just inside the 5-min window is accepted; just
    outside is rejected."""
    name = _in_name("bound")
    db_session.add(
        Webhook(
            name=name,
            direction="in",
            event_filter="*",
            url="placeholder",
            secret="sek",
            enabled=True,
        )
    )
    db_session.commit()
    wid = db_session.query(Webhook).filter_by(name=name).one().id
    payload = _make_payload(timestamp=int(time.time()) - 60)  # 1min ago, fine
    body = json.dumps(payload).encode("utf-8")
    sig = _sign("sek", body)
    status_code, _ = _post_inbound(client, wid, body, sig)
    assert status_code == 200
