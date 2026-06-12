"""Smoke test for GET /health."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_ok(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert body["db"] in ("ok", "down")
    assert "deepseek_configured" in body
    assert "fyers_configured" in body
    assert "version" in body
    assert "trading_mode" in body


def test_health_request_id_header_echo(client: TestClient) -> None:
    r = client.get("/health", headers={"X-Request-ID": "abc123"})
    assert r.headers.get("x-request-id") == "abc123"
