"""Discord notifier tests."""
from __future__ import annotations

import json
from typing import Any, Callable

import httpx
import pytest

from app.notifications.base import NotificationContext
from app.notifications.discord import DiscordNotifier


def _client(handler) -> DiscordNotifier:
    return DiscordNotifier(
        timeout_s=5.0,
        transport_factory=lambda: httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_happy_path_posts_content():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    n = _client(handler)
    res = await n.send(
        {"webhook_url": "https://discord.com/api/webhooks/abc/xyz"},
        NotificationContext("signal", "BUY RELIANCE", "body line", {}),
    )
    assert res.ok
    assert captured[0].url.host == "discord.com"
    body = json.loads(captured[0].content)
    assert "BUY RELIANCE" in body["content"]
    assert "body line" in body["content"]
    await n.aclose()


@pytest.mark.asyncio
async def test_missing_webhook_url_returns_not_ok():
    n = _client(lambda r: httpx.Response(204))
    res = await n.send(
        {"webhook_url": ""},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert "webhook_url" in (res.error or "")
    await n.aclose()


@pytest.mark.asyncio
async def test_5xx_returns_not_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    n = _client(handler)
    res = await n.send(
        {"webhook_url": "https://x"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert res.status_code == 500
    await n.aclose()


@pytest.mark.asyncio
async def test_4xx_returns_not_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="unknown webhook")

    n = _client(handler)
    res = await n.send(
        {"webhook_url": "https://x"},
        NotificationContext("signal", "x", "x", {}),
    )
    assert not res.ok
    assert res.status_code == 404
    await n.aclose()


@pytest.mark.asyncio
async def test_long_body_truncated():
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    n = _client(handler)
    long_body = "x" * 5000
    res = await n.send(
        {"webhook_url": "https://x"},
        NotificationContext("signal", "S", long_body, {}),
    )
    assert res.ok
    sent = json.loads(captured[0].content)
    assert "<truncated" in sent["content"]
    await n.aclose()
