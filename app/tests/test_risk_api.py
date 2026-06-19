"""Kill-switch / circuit-breaker control API."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_state_kill_resume_cycle(client: TestClient) -> None:
    # Initial state: trading enabled.
    r = client.get("/api/risk/state")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_disabled"] is False
    assert body["entries_allowed"] is True

    # Kill switch disables trading and flattens (nothing open -> 0).
    r = client.post("/api/risk/kill")
    assert r.status_code == 200
    k = r.json()
    assert k["ok"] is True
    assert k["trading_disabled"] is True
    assert k["closed_positions"] == 0

    # State now reflects the halt.
    r = client.get("/api/risk/state")
    s = r.json()
    assert s["trading_disabled"] is True
    assert s["entries_allowed"] is False
    assert s["entry_block_reason"]

    # Resume re-enables trading.
    r = client.post("/api/risk/resume")
    assert r.status_code == 200
    assert r.json()["trading_disabled"] is False
    r = client.get("/api/risk/state")
    assert r.json()["entries_allowed"] is True
