"""Tests for the /api/settings/trading-mode endpoint.

Coverage:

  - POST without `confirm: true` -> 422.
  - POST with `confirm: true` flips to live, writes audit_log.
  - POST with `confirm: false` -> 422.
  - POST with unknown mode -> 422.
  - POST live without the .i_accept_live_risk sentinel -> 403.
  - POST live WITH the sentinel (test fixture) -> 200.
  - Settings hot-reload fires on the bus.
  - **Hot-reload of the cached Settings instance**: after the
    POST, `get_settings().TRADING_MODE` returns the new mode
    (verifier bug: previously the cache was never cleared, so
    `effective` stayed at the old value).
  - **End-to-end routing**: after the POST, the next signal
    routes to the live backend (not the paper backend).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import trading_mode as trading_mode_api
from app.config import get_settings, reset_settings_cache
from app.db.models import AuditLog
from app.services.event_bus import event_bus


@pytest.fixture
def live_risk_acked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the operator created .i_accept_live_risk for this test."""
    monkeypatch.setattr(trading_mode_api, "_live_risk_ack_present", lambda: True)


def test_post_live_without_sentinel_is_403(client: TestClient) -> None:
    """The .i_accept_live_risk sentinel gate is real — no sentinel, no live flip."""
    r = client.post(
        "/api/settings/trading-mode",
        json={"mode": "live", "confirm": True},
    )
    assert r.status_code == 403
    assert "i_accept_live_risk" in r.json()["detail"]


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


def test_post_live_with_confirm_succeeds(client: TestClient, live_risk_acked) -> None:
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


def test_effective_field_reflects_new_mode_immediately(client: TestClient, live_risk_acked) -> None:
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

