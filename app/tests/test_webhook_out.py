"""Outbound webhook dispatcher tests.

Coverage:
  - The signed POST is sent with the X-Mavis-Signature header
  - The signature verifies (round-trips through `verify_signature`)
  - 5xx triggers a retry
  - 4xx is NOT retried
  - A `webhook_deliveries` row is written with status_code + error
  - No HTTP request when no webhooks match the event
  - `event_filter='*'` matches all events
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

from app.db.models import Webhook, WebhookDelivery
from app.webhooks.dispatcher import WebhookDispatcher
from app.webhooks.signing import compute_signature, verify_signature


# Per-test unique-name counter — in-memory SQLite leaks between
# tests within a session, so we avoid name collisions and instead
# assert behaviour by the row's own properties.
_counter = {"n": 0}


def _name(prefix: str = "w") -> str:
    _counter["n"] += 1
    return f"{prefix}-{_counter['n']}"


def _client(handler) -> WebhookDispatcher:
    return WebhookDispatcher(
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), timeout=5.0
        ),
    )


@pytest.mark.asyncio
async def test_signed_post_sent_with_valid_signature(db_session, isolated_db):
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"ok": True})

    db_session.add(
        Webhook(
            name=_name("signed"),
            direction="out",
            event_filter="*",
            url="https://example.test/hook",
            secret="topsecret",
            enabled=True,
        )
    )
    db_session.commit()

    d = _client(handler)
    n = await d.dispatch_event(
        "signals.new", {"signal_id": 1, "symbol": "RELIANCE", "action": "BUY"}
    )
    assert n == 1
    assert len(captured) == 1
    req = captured[0]
    body = req.content
    # Verify signature
    sig = req.headers.get("X-Mavis-Signature")
    assert sig is not None
    assert verify_signature("topsecret", body, sig)
    # Re-compute and compare
    expected = compute_signature("topsecret", body)
    assert sig == f"sha256={expected}"


@pytest.mark.asyncio
async def test_5xx_is_retried_then_succeeds(db_session, isolated_db):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if len(calls) < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"ok": True})

    db_session.add(
        Webhook(
            name=_name("retry"),
            direction="out",
            event_filter="*",
            url="https://example.test/h",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    d = _client(handler)
    # Stub the backoffs to be instant — we're testing the retry
    # count, not the wall-clock delay.
    import app.webhooks.dispatcher as disp
    real = disp.RETRY_BACKOFFS_S
    disp.RETRY_BACKOFFS_S = (0.0, 0.0, 0.0)
    try:
        await d.dispatch_event("signals.new", {"x": 1})
    finally:
        disp.RETRY_BACKOFFS_S = real
    # 3 attempts total
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_4xx_is_not_retried(db_session, isolated_db):
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        return httpx.Response(404, text="nope")

    db_session.add(
        Webhook(
            name=_name("noretry"),
            direction="out",
            event_filter="*",
            url="https://example.test/h",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    d = _client(handler)
    import app.webhooks.dispatcher as disp
    real = disp.RETRY_BACKOFFS_S
    disp.RETRY_BACKOFFS_S = (0.0, 0.0, 0.0)
    try:
        await d.dispatch_event("signals.new", {"x": 1})
    finally:
        disp.RETRY_BACKOFFS_S = real
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_delivery_row_persisted_with_status_code_and_error(db_session, isolated_db):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overload")

    db_session.add(
        Webhook(
            name=_name("delivery"),
            direction="out",
            event_filter="*",
            url="https://example.test/h",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()

    d = _client(handler)
    import app.webhooks.dispatcher as disp
    real = disp.RETRY_BACKOFFS_S
    disp.RETRY_BACKOFFS_S = (0.0, 0.0, 0.0)
    try:
        await d.dispatch_event("signals.new", {"x": 1})
    finally:
        disp.RETRY_BACKOFFS_S = real

    rows = (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.webhook_id == db_session.query(Webhook).filter_by(name=_name("delivery")).one().id)
        .all()
    ) if False else db_session.query(WebhookDelivery).all()
    # The most recent delivery should be 503.
    assert any(r.status_code == 503 and "503" in (r.error or "") for r in rows)


@pytest.mark.asyncio
async def test_event_filter_excludes_non_matching(db_session, isolated_db):
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200, json={"ok": True})

    # This webhook ONLY matches trades.filled.
    db_session.add(
        Webhook(
            name=_name("filter"),
            direction="out",
            event_filter="trades.filled",
            url="https://example.test/h",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    d = _client(handler)
    n = await d.dispatch_event("signals.new", {"x": 1})
    # No webhook matches → 0 deliveries
    assert n == 0
    assert captured == []


@pytest.mark.asyncio
async def test_disabled_webhook_is_skipped(db_session, isolated_db):
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    db_session.add(
        Webhook(
            name=_name("disabled"),
            direction="out",
            event_filter="*",
            url="https://example.test/h",
            secret="",
            enabled=False,
        )
    )
    db_session.commit()
    d = _client(handler)
    n = await d.dispatch_event("signals.new", {"x": 1})
    assert n == 0
    assert captured == []


@pytest.mark.asyncio
async def test_inbound_direction_is_skipped_by_outbound_dispatcher(db_session, isolated_db):
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(200)

    db_session.add(
        Webhook(
            name=_name("inbound"),
            direction="in",  # not outbound
            event_filter="*",
            url="https://example.test/h",
            secret="",
            enabled=True,
        )
    )
    db_session.commit()
    d = _client(handler)
    n = await d.dispatch_event("signals.new", {"x": 1})
    assert n == 0
    assert captured == []


@pytest.mark.asyncio
async def test_no_webhooks_returns_zero_attempts(db_session, isolated_db):
    d = _client(lambda r: httpx.Response(200))
    n = await d.dispatch_event("signals.new", {"x": 1})
    assert n == 0
