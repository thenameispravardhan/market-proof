"""Notification manager — the bus must actually reach a channel.

This file exists because of a silent total failure: notifications had
never worked through the event bus. `notification_log` on the live
server held exactly one row, a `test` from the day the channel was
created, while 36,767 signals came and went.
"""
from __future__ import annotations

import asyncio

import pytest

from app.notifications.base import NotificationResult


class _KeepOpen:
    """`_load_enabled_channels` / `_write_log` do `with session_factory() as s`,
    which would close the fixture's session on first use. Hand it over
    without closing so the test keeps its one in-memory DB."""

    def __init__(self, session) -> None:  # noqa: ANN001
        self._s = session

    def __enter__(self):
        return self._s

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        return False


class _Spy:
    """Stands in for a real notifier; records what it was asked to send."""

    def __init__(self, sent: list[tuple[str, str]]) -> None:
        self._sent = sent

    async def send(self, config, ctx):  # noqa: ANN001
        self._sent.append((ctx.event_type, str(config.get("chat_id", ""))))
        return NotificationResult(ok=True)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_bus_event_reaches_the_channel(db_session, isolated_db):
    """The bug this guards: the manager raced every queue in one
    `asyncio.wait`, then recovered which queue a completed task came from
    with `task in q._getters`. That is False even WHILE waiting — asyncio
    stores a bare Future there, not the Task wrapping it — so the lookup
    returned None every time and EVERY bus event was dropped. Only the
    direct /test endpoint, which bypasses the bus, ever delivered."""
    from app.analyzer.service import CHANNEL_NEW_SIGNAL
    from app.db.models import NotificationChannel
    from app.notifications.manager import NotificationManager
    from app.services.event_bus import event_bus

    db_session.add(NotificationChannel(
        name="tg", kind="telegram", enabled=True, events_filter="*",
        config={"bot_token": "t", "chat_id": "1749345975"},
    ))
    db_session.commit()

    sent: list[tuple[str, str]] = []
    mgr = NotificationManager(
        session_factory=lambda: _KeepOpen(db_session),
        notifier_factory=lambda kind: _Spy(sent),
    )
    mgr.start()
    await mgr.wait_until_ready()
    try:
        await event_bus.publish(
            CHANNEL_NEW_SIGNAL, {"symbol": "HINDCOPPER", "action": "BUY"}
        )
        for _ in range(100):
            if sent:
                break
            await asyncio.sleep(0.02)
    finally:
        mgr.stop()
        await mgr.wait_until_stopped()

    assert sent, "a signals.new event never reached the notifier"
    assert sent[0] == ("signal", "1749345975")


@pytest.mark.asyncio
async def test_every_subscribed_channel_delivers(db_session, isolated_db):
    """One consumer per channel — so a second channel is not starved by
    the first, which the single-race loop could also do."""
    from app.db.models import NotificationChannel
    from app.notifications.manager import NotificationManager
    from app.services.event_bus import event_bus

    db_session.add(NotificationChannel(
        name="tg", kind="telegram", enabled=True, events_filter="*",
        config={"bot_token": "t", "chat_id": "1"},
    ))
    db_session.commit()

    sent: list[tuple[str, str]] = []
    mgr = NotificationManager(
        session_factory=lambda: _KeepOpen(db_session),
        notifier_factory=lambda kind: _Spy(sent),
    )
    mgr.start()
    await mgr.wait_until_ready()
    try:
        for channel in ("signals.new", "trades.filled", "system.error"):
            await event_bus.publish(channel, {"symbol": "X"})
        for _ in range(100):
            if len(sent) >= 3:
                break
            await asyncio.sleep(0.02)
    finally:
        mgr.stop()
        await mgr.wait_until_stopped()

    assert {k for k, _ in sent} == {"signal", "trade", "error"}
