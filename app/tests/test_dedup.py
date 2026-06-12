"""Dedup tests: same `content_hash` => 1 row, 1 publish.

Exercises the BaseMonitor + the UNIQUE(content_hash) constraint on
`announcements` together. The unique constraint itself is covered by
`app/tests/test_db.py::test_announcement_content_hash_unique`; here
we focus on the monitor's dedup behaviour under realistic conditions.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Announcement
from app.monitors.base import BaseMonitor, RawAnnouncement
from app.services.event_bus import event_bus

pytestmark = pytest.mark.usefixtures("isolated_db")


class _OneShotFetcher:
    """Calls a factory once, then returns empty payloads."""

    def __init__(self, raw_payloads):
        self._raw_payloads = list(raw_payloads)
        self.call_count = 0

    async def __call__(self, url: str):
        self.call_count += 1
        if not self._raw_payloads:
            return ""
        return self._raw_payloads.pop(0)


def _fifo_parser(payloads):
    payloads = list(payloads)

    def _parse(raw, url):
        if not payloads:
            return []
        return payloads.pop(0)

    return _parse


@pytest.mark.asyncio
async def test_same_payload_dedupes_to_one_row_and_one_publish(db_session):
    """Two identical ticks => one row, one event_bus publish."""
    sub = event_bus.subscribe("announcements.new")
    try:
        posted_at = datetime(2026, 5, 1, 9, 30, tzinfo=timezone.utc)
        raw_a = RawAnnouncement(
            company="WIPRO",
            title="Dividend",
            posted_at=posted_at,
            pdf_url="https://example.com/w.pdf",
        )
        fetcher = _OneShotFetcher(["p1", "p2"])
        parser = _fifo_parser([[raw_a], [raw_a]])
        mon = BaseMonitor(
            fetcher=fetcher,
            parser=parser,
            poll_interval=0.01,
            source_url="https://stub",
        )
        mon.exchange = "DEDUP1"
        mon.start()
        try:
            evt = await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            mon.stop()
            await mon.wait_until_stopped()

        # Give the second tick a chance to run + dedup.
        await asyncio.sleep(0.3)
        rows = db_session.execute(select(Announcement)).scalars().all()
        assert len(rows) == 1
        assert rows[0].symbol == "WIPRO"
        # Only one publish was queued on the bus (we consumed it).
        # Confirm the queue is empty now.
        assert sub.empty(), f"expected empty queue; got {sub.qsize()} pending events"
    finally:
        event_bus.unsubscribe("announcements.new", sub)


@pytest.mark.asyncio
async def test_two_distinct_payloads_insert_two_rows(db_session):
    """Different content_hash => two rows, two publishes."""
    sub = event_bus.subscribe("announcements.new")
    try:
        a1 = RawAnnouncement(
            company="HCL",
            title="Update A",
            posted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        a2 = RawAnnouncement(
            company="HCL",
            title="Update B",  # different title -> different hash
            posted_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
        )
        fetcher = _OneShotFetcher(["p1", "p2"])
        parser = _fifo_parser([[a1], [a2]])
        mon = BaseMonitor(
            fetcher=fetcher,
            parser=parser,
            poll_interval=0.01,
            source_url="https://stub",
        )
        mon.exchange = "DEDUP2"
        mon.start()
        try:
            evt1 = await asyncio.wait_for(sub.get(), timeout=2.0)
            evt2 = await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            mon.stop()
            await mon.wait_until_stopped()
        await asyncio.sleep(0.2)
        rows = db_session.execute(select(Announcement)).scalars().all()
        assert len(rows) == 2
        titles = {r.headline for r in rows}
        assert titles == {"Update A", "Update B"}
    finally:
        event_bus.unsubscribe("announcements.new", sub)
