"""GET/PUT /api/settings tests."""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.services.event_bus import event_bus


def test_credentials_get_masks_secrets(client: TestClient) -> None:
    r = client.get("/api/settings/credentials")
    assert r.status_code == 200
    b = r.json()
    assert "fyers_app_id" in b
    assert "fyers_secret_set" in b
    # Raw secret values are NEVER returned.
    assert "fyers_secret_key" not in b
    assert "deepseek_api_key" not in b


def test_credentials_put_hot_applies_without_restart(client: TestClient) -> None:
    """PUT hot-applies to the live process so get_settings() reflects the
    change with no restart (the .env write is skipped under TESTING).
    Secrets are masked on the way back out."""
    import os
    from app.config import get_settings

    keys = ("FYERS_APP_ID", "FYERS_SECRET_KEY", "FYERS_REDIRECT_URI", "DEEPSEEK_API_KEY")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        r = client.put(
            "/api/settings/credentials",
            json={
                "fyers_app_id": "TESTAPP-100",
                "fyers_secret_key": "supersecret123",
                "fyers_redirect_uri": "http://localhost:8000/api/fyers/callback",
            },
        )
        assert r.status_code == 200, r.text
        s = get_settings()
        assert s.FYERS_APP_ID == "TESTAPP-100"
        assert s.FYERS_SECRET_KEY == "supersecret123"
        b = client.get("/api/settings/credentials").json()
        assert b["fyers_app_id"] == "TESTAPP-100"
        assert b["fyers_secret_set"] is True
        assert "supersecret123" not in json.dumps(b)
        assert client.put("/api/settings/credentials", json={}).status_code == 422
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def test_get_settings_defaults(client: TestClient) -> None:
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    g = body["global"]
    assert g["TRADING_MODE"] == "paper"
    # Risk defaults follow RISK.md (0.75% risk, 2.5% daily, 3 concurrent).
    assert g["MAX_CAPITAL_RISK_PCT"] == 0.75
    assert g["DAILY_MAX_LOSS_PCT"] == 2.5
    assert g["MAX_CONCURRENT_POSITIONS"] == 3
    assert g["MAX_SINGLE_POSITION_PCT"] == 20.0
    assert g["MIN_LIQUIDITY_CRORE"] == 5.0
    assert g["MAX_SIGNALS_PER_DAY"] == 20
    assert g["POLL_INTERVAL_SECONDS"] == 5
    assert "sections" in body
    assert "version" in body


def test_put_updates_and_persists(client: TestClient) -> None:
    r = client.put(
        "/api/settings",
        json={
            "global": {
                "TRADING_MODE": "live",
                "MAX_CAPITAL_RISK_PCT": 1.5,
                "DAILY_MAX_LOSS_PCT": 3.0,
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    g = body["global"]
    assert g["TRADING_MODE"] == "live"
    assert g["MAX_CAPITAL_RISK_PCT"] == 1.5
    assert g["DAILY_MAX_LOSS_PCT"] == 3.0
    # Untouched settings keep their previous values
    assert g["MAX_CONCURRENT_POSITIONS"] == 3

    # GET reflects the change
    r2 = client.get("/api/settings")
    assert r2.json()["global"]["TRADING_MODE"] == "live"
    assert r2.json()["global"]["MAX_CAPITAL_RISK_PCT"] == 1.5


def test_put_invalid_range_422(client: TestClient) -> None:
    r = client.put("/api/settings", json={"global": {"MAX_CAPITAL_RISK_PCT": 150.0}})
    assert r.status_code == 422


def test_put_unknown_key_422(client: TestClient) -> None:
    r = client.put("/api/settings", json={"global": {"NOPE": 1}})
    assert r.status_code == 422


def test_put_invalid_trading_mode_422(client: TestClient) -> None:
    r = client.put("/api/settings", json={"global": {"TRADING_MODE": "banana"}})
    assert r.status_code == 422


def test_put_writes_audit_log(client: TestClient) -> None:
    # Fresh state — clear audit log to make assertion deterministic.
    from app.db.session import SessionLocal
    from app.db.models import AuditLog

    with SessionLocal() as s:
        s.query(AuditLog).delete()
        s.commit()

    r = client.put("/api/settings", json={"global": {"MAX_SIGNALS_PER_DAY": 42}})
    assert r.status_code == 200

    with SessionLocal() as s:
        rows = s.query(AuditLog).all()
    actions = [row.action for row in rows]
    assert "settings.update" in actions
    row = next(r for r in rows if r.action == "settings.update")
    assert row.actor == "api"
    assert row.target == "global"
    assert row.after["MAX_SIGNALS_PER_DAY"] == 42


def test_settings_updated_event_fires(client: TestClient) -> None:
    # Subscribe to the event bus BEFORE making the PUT.
    q = event_bus.subscribe("settings.updated")
    try:
        r = client.put("/api/settings", json={"global": {"MAX_CONCURRENT_POSITIONS": 7}})
        assert r.status_code == 200

        # Drain the queue with a short timeout
        event = _drain(q, timeout=2.0)
        assert event is not None, "expected settings.updated event"
        assert event.channel == "settings.updated"
        assert "MAX_CONCURRENT_POSITIONS" in event.payload.get("changed_keys", [])
    finally:
        event_bus.unsubscribe("settings.updated", q)


def _drain(q, timeout: float):
    """Pull a single event from a queue, but in a way that works with the
    synchronous TestClient (the event bus publishes from a coroutine
    that the test client drives via the request lifecycle)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            # Final attempt — return whatever is in the queue (may be empty).
            try:
                return q.get_nowait()
            except Exception:
                return None
        try:
            return q.get_nowait()
        except Exception:
            import time as _t

            _t.sleep(0.02)
