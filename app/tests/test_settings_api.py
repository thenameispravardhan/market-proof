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
    assert g["POLL_INTERVAL_SECONDS"] == 2
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


def test_pre_llm_filter_bool_roundtrips_and_hot_applies(client: TestClient) -> None:
    """The pre-LLM noise filter toggle is a real boolean: it defaults ON,
    PUT false flips it (and hot-applies to get_settings without restart),
    and PUT true flips it back. Guards against bool('false') == True."""
    import os
    from app.config import get_settings

    saved = os.environ.get("PRE_LLM_FILTER_ENABLED")
    try:
        # Default is ON.
        assert client.get("/api/settings").json()["global"]["PRE_LLM_FILTER_ENABLED"] is True

        # Turn it OFF.
        r = client.put("/api/settings", json={"global": {"PRE_LLM_FILTER_ENABLED": False}})
        assert r.status_code == 200, r.text
        assert r.json()["global"]["PRE_LLM_FILTER_ENABLED"] is False
        assert client.get("/api/settings").json()["global"]["PRE_LLM_FILTER_ENABLED"] is False
        # Hot-applied to the live Settings (no restart).
        assert get_settings().PRE_LLM_FILTER_ENABLED is False

        # Turn it back ON.
        r2 = client.put("/api/settings", json={"global": {"PRE_LLM_FILTER_ENABLED": True}})
        assert r2.json()["global"]["PRE_LLM_FILTER_ENABLED"] is True
        assert get_settings().PRE_LLM_FILTER_ENABLED is True
    finally:
        if saved is None:
            os.environ.pop("PRE_LLM_FILTER_ENABLED", None)
        else:
            os.environ["PRE_LLM_FILTER_ENABLED"] = saved
        get_settings.cache_clear()


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


def test_send_extracted_text_toggle_roundtrip(client: TestClient) -> None:
    """SEND_EXTRACTED_TEXT defaults OFF (legacy URL mode) and PUT toggles
    it live — same contract as the other boolean switches."""
    import os
    from app.config import get_settings

    saved = os.environ.get("SEND_EXTRACTED_TEXT")
    try:
        # Default is OFF — the legacy behavior is the default behavior.
        assert client.get("/api/settings").json()["global"]["SEND_EXTRACTED_TEXT"] is False

        r = client.put("/api/settings", json={"global": {"SEND_EXTRACTED_TEXT": True}})
        assert r.status_code == 200, r.text
        assert r.json()["global"]["SEND_EXTRACTED_TEXT"] is True
        # Hot-applied to the live Settings (no restart).
        assert get_settings().SEND_EXTRACTED_TEXT is True

        r2 = client.put("/api/settings", json={"global": {"SEND_EXTRACTED_TEXT": False}})
        assert r2.json()["global"]["SEND_EXTRACTED_TEXT"] is False
        assert get_settings().SEND_EXTRACTED_TEXT is False
    finally:
        if saved is None:
            os.environ.pop("SEND_EXTRACTED_TEXT", None)
        else:
            os.environ["SEND_EXTRACTED_TEXT"] = saved
        get_settings.cache_clear()


# -------------------------------------------------------------------------
# Exit Manager keys (Exits page)
# -------------------------------------------------------------------------


def _delete_overrides(keys: list[str]) -> None:
    """Remove app_settings override rows so they can't leak into later
    tests via the next apply_overrides_to_env()."""
    from app.db.infra_models import AppSetting
    from app.db.session import SessionLocal

    with SessionLocal() as s:
        for k in keys:
            row = s.get(AppSetting, k)
            if row is not None:
                s.delete(row)
        s.commit()


def test_exit_keys_roundtrip_and_hot_apply(client: TestClient) -> None:
    """Every Exit Manager knob PUT via the settings API round-trips through
    GET and hot-applies to the live Settings without a restart."""
    import os
    from app.config import get_settings

    payload = {
        "MAX_ENTRY_DRIFT_PCT": 2.0,
        "ATR_STOP_MULT": 2.5,
        "BREAKEVEN_AT_PCT": 1.5,
        "TRAIL_DISTANCE_R": 0.75,
        "SCALE_OUT_ENABLED": False,
        "CONSOLIDATION_WINDOW_SECONDS": 180,
        "STALL_ROC_PCT": 0.5,
        "MAX_HOLD_SECONDS": 900,
        "SQUARE_OFF_TIME_IST": "14:45",
    }
    saved = {k: os.environ.get(k) for k in payload}
    try:
        r = client.put("/api/settings", json={"global": payload})
        assert r.status_code == 200, r.text
        g = r.json()["global"]
        for k, v in payload.items():
            assert g[k] == v, k
        # Hot-applied: the engine reads these via get_settings() per tick.
        s = get_settings()
        assert s.SCALE_OUT_ENABLED is False
        assert s.MAX_HOLD_SECONDS == 900
        assert s.SQUARE_OFF_TIME_IST == "14:45"
        assert s.TRAIL_DISTANCE_R == 0.75
    finally:
        # Remove the DB override rows too — any later PUT re-applies ALL
        # overrides to env, which would leak these values into unrelated
        # tests (e.g. disable scale-out for the trade-exit tests).
        _delete_overrides(list(payload))
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def test_exit_keys_bounds_rejected(client: TestClient) -> None:
    """Out-of-range exit values are refused at the API door so a typo can
    never poison get_settings() for the whole process."""
    cases = [
        {"SQUARE_OFF_TIME_IST": "15:25"},   # later than 15:15 collides with close
        {"SQUARE_OFF_TIME_IST": "09:00"},   # before the session
        {"SQUARE_OFF_TIME_IST": "banana"},  # not HH:MM
        {"TRAIL_DISTANCE_R": 0},            # R-multiples must be > 0
        {"SCALE_OUT_R": 11},                # > 10 is a typo, not a strategy
        {"MAX_HOLD_SECONDS": 30},           # sub-minute forced exit is churn
        {"STALL_WINDOW_SECONDS": 5},        # sub-10s observation window
        {"BREAKEVEN_AT_PCT": 0},            # percent must be in (0, 100]
        {"ATR_PERIOD": 0},                  # must be >= 1
    ]
    for body in cases:
        r = client.put("/api/settings", json={"global": body})
        assert r.status_code == 422, f"{body} should be rejected, got {r.status_code}"


def test_square_off_time_normalized(client: TestClient) -> None:
    """'9:45' is accepted and stored zero-padded, matching the config
    validator's normalization."""
    import os
    from app.config import get_settings

    saved = os.environ.get("SQUARE_OFF_TIME_IST")
    try:
        r = client.put("/api/settings", json={"global": {"SQUARE_OFF_TIME_IST": "9:45"}})
        assert r.status_code == 200, r.text
        assert r.json()["global"]["SQUARE_OFF_TIME_IST"] == "09:45"
    finally:
        _delete_overrides(["SQUARE_OFF_TIME_IST"])
        if saved is None:
            os.environ.pop("SQUARE_OFF_TIME_IST", None)
        else:
            os.environ["SQUARE_OFF_TIME_IST"] = saved
        get_settings.cache_clear()
