"""OriginGuard: DNS-rebinding Host check + cross-site write / WebSocket block."""
import pytest
from starlette.websockets import WebSocketDisconnect


def test_foreign_host_rejected(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health", headers={"host": "evil.example:8000"}).status_code == 403


def test_cross_site_post_rejected(client):
    r = client.post("/api/risk/resume", headers={"origin": "https://evil.example"})
    assert r.status_code == 403


def test_same_site_and_no_origin_posts_pass(client):
    for headers in ({}, {"origin": "http://localhost:5173"}):
        assert client.post("/api/risk/resume", headers=headers).status_code != 403


def test_postback_exempt(client):
    r = client.post("/api/fyers/postback", json={}, headers={"origin": "https://api.fyers.in"})
    assert r.status_code != 403


def test_cross_site_websocket_rejected(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": "https://evil.example"}):
            pass
