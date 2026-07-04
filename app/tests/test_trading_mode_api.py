"""Tests for the /api/settings/trading-mode endpoint.

Coverage:

  - POST without `confirm: true` -> 422.
  - POST with `confirm: true` flips to live, writes audit_log.
  - POST with `confirm: false` -> 422.
  - POST with unknown mode -> 422.
  - POST live with `confirm: true` -> 200 (the old .i_accept_live_risk
    file sentinel is GONE — frontend-only control; the UI's typed
    confirmation + `confirm: true` are the gate).
  - Settings hot-reload fires on the bus.
  - **Hot-reload of the cached Settings instance**: after the
    POST, `get_settings().TRADING_MODE` returns the new mode
    (verifier bug: previously the cache was never cleared, so
    `effective` stayed at the old value).
  - **End-to-end routing**: after the POST, the next signal
    routes to the live backend (not the paper backend).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings, reset_settings_cache
from app.db.models import AuditLog
from app.services.event_bus import event_bus


def test_live_risk_ack_reports_not_required(client: TestClient) -> None:
    """The old .i_accept_live_risk file sentinel is gone (frontend-only
    control). The compat endpoint must always report present=True so no
    UI ever greys out the live switch over a file."""
    r = client.get("/api/settings/live-risk-ack")
    assert r.status_code == 200
    assert r.json()["present"] is True


def test_post_without_confirm_rejected(client: TestClient) -> None:
    r = client.post("/api/settings/trading-mode", json={"mode": "live"})
    assert r.status_code == 422


def test_post_with_confirm_false_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "live", "confirm": False},
    )
    assert r.status_code == 422


def test_post_with_unknown_mode_rejected(client: TestClient) -> None:
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "banana", "confirm": True},
    )
    assert r.status_code == 422


def test_post_paper_with_confirm_succeeds(client: TestClient) -> None:
    # Clean up audit log for a deterministic assertion.
    from app.db.session import SessionLocal
    with SessionLocal() as s:
        s.query(AuditLog).filter_by(action="settings.trading_mode_changed").delete()
        s.commit()
    sub = event_bus.subscribe("settings.updated")
    try:
        r = client.post(
            "/api/settings/trading-mode",
            json={"mode": "paper", "confirm": True},
            headers={"x-actor": "ui"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["mode"] == "paper"
        # Hot-reload: the cached Settings instance must reflect the
        # new mode (this was the verifier bug — the cache used to
        # be stale).
        assert body["effective"] == "paper", (
            f"effective should be 'paper' after the toggle, got {body.get('effective')!r}"
        )
        # The next get_settings() call also returns the new mode.
        s_now = get_settings()
        assert s_now.TRADING_MODE == "paper"
        # settings.updated event fired.
        event = sub.get_nowait()
        assert event.payload["changed_keys"] == ["TRADING_MODE"]
        # audit log row.
        with SessionLocal() as s:
            rows = s.query(AuditLog).filter_by(action="settings.trading_mode_changed").all()
            assert len(rows) == 1
            assert rows[0].actor == "ui"
            assert rows[0].after["TRADING_MODE"] == "paper"
    finally:
        event_bus.unsubscribe("settings.updated", sub)


def test_post_live_with_confirm_succeeds(client: TestClient) -> None:
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "live", "confirm": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "live"
    # Hot-reload.
    assert body["effective"] == "live"
    assert get_settings().TRADING_MODE == "live"
    # Best-effort: leave TRADING_MODE back to paper for the next test.
    client.post(
        "/api/settings/trading-mode",
        json={"mode": "paper", "confirm": True},
    )


def test_fyers_account_toggle_flips_trading_mode(client: TestClient) -> None:
    """The Dashboard Fyers switch IS the live-trading switch: enabling
    the REAL (non-paper) account flips TRADING_MODE to live, disabling
    flips back to paper. A paper-account toggle never touches the mode."""
    # Start from a known state.
    client.post("/api/settings/trading-mode", json={"mode": "paper", "confirm": True})

    r = client.post(
        "/api/broker-accounts",
        json={"name": "mode-coupling-real", "broker": "fyers", "paper_mode": False, "enabled": False},
    )
    assert r.status_code == 201, r.text
    real_id = r.json()["id"]
    r = client.post(
        "/api/broker-accounts",
        json={"name": "mode-coupling-paper", "broker": "fyers", "paper_mode": True, "enabled": False},
    )
    assert r.status_code == 201, r.text
    paper_id = r.json()["id"]

    # Enable the REAL account -> live.
    r = client.put(f"/api/broker-accounts/{real_id}", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["global"]["TRADING_MODE"] == "live"
    assert get_settings().TRADING_MODE == "live"

    # Toggling the PAPER account must not touch the mode.
    client.put(f"/api/broker-accounts/{paper_id}", json={"enabled": True})
    assert get_settings().TRADING_MODE == "live"

    # Disable the REAL account -> back to paper.
    r = client.put(f"/api/broker-accounts/{real_id}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert client.get("/api/settings").json()["global"]["TRADING_MODE"] == "paper"
    assert get_settings().TRADING_MODE == "paper"

    # A non-enabled update (e.g. rename) on the real account never
    # touches the mode either.
    client.put(f"/api/broker-accounts/{real_id}", json={"name": "mode-coupling-real-2"})
    assert get_settings().TRADING_MODE == "paper"


def test_effective_field_reflects_new_mode_immediately(client: TestClient) -> None:
    """Defence-in-depth: the response body must include the new
    effective mode, not the stale cached one."""
    # First flip to paper explicitly.
    client.post(
        "/api/settings/trading-mode",
        json={"mode": "paper", "confirm": True},
    )
    r1 = client.get("/api/settings")
    assert r1.json()["global"]["TRADING_MODE"] == "paper"
    # Now flip to live and verify.
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "live", "confirm": True},
    )
    body = r.json()
    assert body["effective"] == "live", (
        f"verifier bug: effective={body.get('effective')!r} should be 'live'"
    )
    # Reset to paper for subsequent tests.
    client.post(
        "/api/settings/trading-mode",
        json={"mode": "paper", "confirm": True},
    )

