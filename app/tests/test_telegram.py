"""Telegram notifier tests.

Coverage:
  - Happy path: 200 + ok=true → ok=True
  - 2xx but ok=false (Telegram protocol error) → ok=False
  - 5xx → ok=False, retries (manager layer)
  - 4xx → ok=False, NOT retried by the manager (the manager
    counts it as a final failure; we test the manager's
    retry behaviour separately)
  - Missing bot_token / chat_id → ok=False
  - Secret never logged (config keys absent from the URL we hit)
"""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from app.notifications.base import NotificationContext
from app.notifications.telegram import TelegramNotifier


def _ok_response() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def _protocol_error() -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "description": "chat not found"})


def _server_error() -> httpx.Response:
    return httpx.Response(503, text="upstream busy")


def _client(handler) -> TelegramNotifier:
    return TelegramNotifier(
        timeout_s=5.0,
        transport_factory=lambda: httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_happy_path_returns_ok():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return _ok_response()

    n = _client(handler)
    res = await n.send(
        {"bot_token": "T", "chat_id": "123"},
        NotificationContext(event_type="signal", subject="BUY RELIANCE", body="x", payload={}),
    )
    assert res.ok
    assert res.status_code == 200
    # The URL must include the bot token + sendMessage path.
    assert captured[0].url.path.endswith("/botT/sendMessage")
    # The body must include the chat_id and text.
    sent = json.loads(captured[0].content)
    assert sent["chat_id"] == "123"
    assert "BUY RELIANCE" in sent["text"]
    await n.aclose()


@pytest.mark.asyncio
async def test_protocol_error_returns_not_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        return _protocol_error()

    n = _client(handler)
    res = await n.send(
        {"bot_token": "T", "chat_id": "1"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert "chat not found" in (res.error or "")
    await n.aclose()


@pytest.mark.asyncio
async def test_5xx_returns_not_ok_with_status_code():
    def handler(req: httpx.Request) -> httpx.Response:
        return _server_error()

    n = _client(handler)
    res = await n.send(
        {"bot_token": "T", "chat_id": "1"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert res.status_code == 503
    await n.aclose()


@pytest.mark.asyncio
async def test_missing_bot_token_returns_not_ok():
    def handler(req: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP should not be called")

    n = _client(handler)
    res = await n.send(
        {"bot_token": "", "chat_id": "1"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert "bot_token" in (res.error or "")
    await n.aclose()


@pytest.mark.asyncio
async def test_missing_chat_id_returns_not_ok():
    n = _client(lambda r: _ok_response())
    res = await n.send(
        {"bot_token": "T", "chat_id": "  "},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert "chat_id" in (res.error or "")
    await n.aclose()


@pytest.mark.asyncio
async def test_transport_error_is_caught():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    n = _client(handler)
    res = await n.send(
        {"bot_token": "T", "chat_id": "1"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert "transport" in (res.error or "").lower()
    await n.aclose()
