"""Tests for `app.monitors.base` — content hash, backoff math, and the
end-to-end BaseMonitor loop driven by a stub fetcher.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Announcement
from app.monitors.base import (
    BACKOFF_INITIAL,
    BACKOFF_MAX,
    BaseMonitor,
    RawAnnouncement,
    make_content_hash,
    next_backoff,
    reset_backoff,
)

pytestmark = pytest.mark.usefixtures("isolated_db")


# -------------------------------------------------------------------------
# content_hash
# -------------------------------------------------------------------------


def test_content_hash_is_64_hex():
    h = make_content_hash(
        exchange="NSE",
        company="RELIANCE",
        title="Board meeting",
        posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
    )
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_content_hash_normalises_inputs():
    a = make_content_hash(
        exchange="nse",
        company=" reliance ",
        title="Board meeting",
        posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
    )
    b = make_content_hash(
        exchange="NSE",
        company="RELIANCE",
        title=" Board meeting ",
        posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
    )
    assert a == b


def test_content_hash_changes_with_each_input():
    base = dict(
        exchange="NSE",
        company="RELIANCE",
        title="Board meeting",
        posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
    )
    h0 = make_content_hash(**base)
    for field, new_val in [
        ("exchange", "BSE"),
        ("company", "TCS"),
        ("title", "Other"),
        ("posted_at", datetime(2026, 1, 15, 9, 31, tzinfo=timezone.utc)),
    ]:
        new_kwargs = dict(base)
        new_kwargs[field] = new_val
        assert make_content_hash(**new_kwargs) != h0, f"hash did not change when {field} changed"


def test_content_hash_naive_treated_as_utc():
    h_naive = make_content_hash(
        exchange="NSE", company="X", title="Y",
        posted_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    h_aware = make_content_hash(
        exchange="NSE", company="X", title="Y",
        posted_at=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
    )
    assert h_naive == h_aware


def test_content_hash_matches_manual_sha256():
    exchange = "NSE"
    company = "reliance"
    title = "Board meeting"
    posted_at = datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc)
    expected = hashlib.sha256(
        f"{exchange.upper()}|{company.upper()}|{title}|{posted_at.isoformat()}".encode()
    ).hexdigest()
    actual = make_content_hash(
        exchange=exchange, company=company, title=title, posted_at=posted_at,
    )
    assert actual == expected


# -------------------------------------------------------------------------
# backoff
# -------------------------------------------------------------------------


def test_backoff_starts_at_initial():
    assert reset_backoff() == BACKOFF_INITIAL
    assert next_backoff(0, _rng=lambda *_: 0.0) == BACKOFF_INITIAL


def test_backoff_doubles():
    rng = lambda *_: 0.0
    assert next_backoff(BACKOFF_INITIAL, _rng=rng) == 2.0
    assert next_backoff(2.0, _rng=rng) == 4.0
    assert next_backoff(4.0, _rng=rng) == 8.0


def test_backoff_caps_at_60():
    rng = lambda *_: 0.0
    val = 30.0
    val = next_backoff(val, _rng=rng)  # 60.0
    assert val == BACKOFF_MAX
    val = next_backoff(val, _rng=rng)  # stays 60
    assert val == BACKOFF_MAX
    val = next_backoff(val, _rng=rng)
    assert val == BACKOFF_MAX


def test_backoff_jitter_is_bounded():
    # Force the jitter extreme positive — must not exceed cap by much.
    val = next_backoff(BACKOFF_INITIAL, _rng=lambda *_: 100.0)
    # 2.0 + 100% jitter = 4.0 raw; but `max(BACKOFF_INITIAL, ...)` and the
    # 60s cap should be respected through the *next* call.
    # Just assert it's > BACKOFF_INITIAL and not absurd.
    assert val >= BACKOFF_INITIAL
    assert val < BACKOFF_MAX * 2


def test_backoff_jitter_can_be_negative():
    val = next_backoff(10.0, _rng=lambda *_: -100.0)
    # 10 -> 20 raw, minus huge jitter -> clamped to BACKOFF_INITIAL.
    assert val >= BACKOFF_INITIAL


# -------------------------------------------------------------------------
# RawAnnouncement dataclass
# -------------------------------------------------------------------------


def test_raw_announcement_to_dict():
    a = RawAnnouncement(
        company="RELIANCE",
        title="Board meeting",
        posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
        pdf_url="https://example.com/x.pdf",
    )
    d = a.to_dict()
    assert d["company"] == "RELIANCE"
    assert d["pdf_url"] == "https://example.com/x.pdf"
    assert d["posted_at"] == "2026-01-15T09:30:00+00:00"


# -------------------------------------------------------------------------
# BaseMonitor end-to-end (stubbed fetcher)
# -------------------------------------------------------------------------


class _StubFetcher:
    """Records call count + serves a sequence of canned payloads."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.call_count = 0

    async def __call__(self, url: str):
        self.call_count += 1
        if not self._payloads:
            return ""
        return self._payloads.pop(0)


def _stub_parser(payloads_by_call):
    """Return a parser that yields the next RawAnnouncement set per call."""

    def _parse(raw, url):
        if not payloads_by_call:
            return []
        return payloads_by_call.pop(0)

    return _parse


@pytest.mark.asyncio
async def test_base_monitor_inserts_and_publishes(db_session):
    """One tick with one announcement => 1 row, 1 event_bus publish,
    1 webhook_service enqueue (if a matching webhook exists)."""
    from app.services.event_bus import event_bus
    sub = event_bus.subscribe("announcements.new")
    try:
        fetcher = _StubFetcher(["ignored-by-parser"])
        raw_a = RawAnnouncement(
            company="RELIANCE",
            title="Board meeting",
            posted_at=datetime(2026, 1, 15, 9, 30, tzinfo=timezone.utc),
            pdf_url="https://example.com/x.pdf",
        )
        parser = _stub_parser([[raw_a]])
        mon = BaseMonitor(
            fetcher=fetcher,
            parser=parser,
            poll_interval=0.01,
            source_url="https://stub",
        )
        mon.exchange = "STUB"
        task = mon.start()
        # Wait for at least one publish on the subscribed channel.
        try:
            evt = await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            mon.stop()
            await mon.wait_until_stopped()

        assert evt.channel == "announcements.new"
        payload = evt.payload
        assert payload["company"] == "RELIANCE"
        assert payload["exchange"] == "STUB"
        assert payload["pdf_url"] == "https://example.com/x.pdf"

        # Row was inserted.
        rows = db_session.execute(select(Announcement)).scalars().all()
        assert len(rows) == 1
        a = rows[0]
        assert a.symbol == "RELIANCE"
        assert a.exchange == "STUB"
        assert a.content_hash is not None
        assert len(a.content_hash) == 64
    finally:
        event_bus.unsubscribe("announcements.new", sub)


@pytest.mark.asyncio
async def test_base_monitor_dedupes_within_run(db_session):
    """Same content_hash across two ticks => exactly one row, one publish."""
    from app.services.event_bus import event_bus
    sub = event_bus.subscribe("announcements.new")
    try:
        raw_a = RawAnnouncement(
            company="TCS",
            title="Dividend",
            posted_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        fetcher = _StubFetcher(["p1", "p2"])
        parser = _stub_parser([[raw_a], [raw_a]])  # same payload twice
        mon = BaseMonitor(
            fetcher=fetcher,
            parser=parser,
            poll_interval=0.01,
            source_url="https://stub",
        )
        mon.exchange = "STUB"
        mon.start()
        # Wait for the FIRST publish only.
        try:
            evt = await asyncio.wait_for(sub.get(), timeout=2.0)
        finally:
            mon.stop()
            await mon.wait_until_stopped()
        # Give the second tick a chance to run + dedup.
        await asyncio.sleep(0.2)
        rows = db_session.execute(select(Announcement)).scalars().all()
        assert len(rows) == 1, f"expected dedup; got {len(rows)} rows"
        # event_bus saw exactly one publish (we only consumed one).
    finally:
        event_bus.unsubscribe("announcements.new", sub)


@pytest.mark.asyncio
async def test_base_monitor_dedupes_against_existing_row(db_session):
    """A row already in the DB (e.g. inserted by the other monitor)
    blocks a duplicate insert from this monitor."""
    from app.services.event_bus import event_bus
    from app.db.models import compute_content_hash as db_content_hash
    from app.db.models import Announcement

    # Insert a row with a known content_hash that matches what the
    # monitor will produce. Use the T1 helper since that's what the DB
    # will see; but T2 uses make_content_hash. For dedup to fire the
    # values must collide. So we'll insert via make_content_hash.
    from app.monitors.base import make_content_hash as mh

    posted_at = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
    h = mh(exchange="STUB2", company="INFY", title="Buyback", posted_at=posted_at)
    db_session.add(
        Announcement(
            content_hash=h,
            symbol="INFY",
            exchange="STUB2",
            event_type="ANNOUNCEMENT",
            headline="Buyback",
            filed_at=posted_at,
        )
    )
    db_session.commit()

    sub = event_bus.subscribe("announcements.new")
    try:
        raw_a = RawAnnouncement(
            company="INFY",
            title="Buyback",
            posted_at=posted_at,
        )
        fetcher = _StubFetcher(["p1"])
        parser = _stub_parser([[raw_a]])
        mon = BaseMonitor(
            fetcher=fetcher,
            parser=parser,
            poll_interval=0.01,
            source_url="https://stub",
        )
        mon.exchange = "STUB2"
        mon.start()
        # Expect NO publish because the row already exists.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(sub.get(), timeout=0.5)
        mon.stop()
        await mon.wait_until_stopped()

        # Still only the one row.
        rows = db_session.execute(select(Announcement)).scalars().all()
        assert len(rows) == 1
    finally:
        event_bus.unsubscribe("announcements.new", sub)


@pytest.mark.asyncio
async def test_base_monitor_retries_on_retryable_error(monkeypatch):
    """A fetcher that raises _RetryableError triggers the backoff path,
    then a successful tick resets the backoff and proceeds."""
    from app.monitors.base import _RetryableError

    fetcher = _StubFetcher(["bad", "good"])
    raw_a = RawAnnouncement(
        company="WIPRO",
        title="Update",
        posted_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
    )
    parser = _stub_parser([[], [raw_a]])
    # Make the parser raise the first time, succeed the second.
    call_count = {"n": 0}

    def flaky_parse(raw, url):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _RetryableError("simulated 503")
        return [raw_a]

    mon = BaseMonitor(
        fetcher=fetcher,
        parser=flaky_parse,
        poll_interval=0.01,
        source_url="https://stub",
    )
    mon.exchange = "STUB3"
    from app.services.event_bus import event_bus
    sub = event_bus.subscribe("announcements.new")
    try:
        mon.start()
        evt = await asyncio.wait_for(sub.get(), timeout=3.0)
        assert evt.payload["company"] == "WIPRO"
    finally:
        mon.stop()
        await mon.wait_until_stopped()
        event_bus.unsubscribe("announcements.new", sub)
    assert fetcher.call_count >= 2


@pytest.mark.asyncio
async def test_base_monitor_stops_cleanly(monkeypatch):
    """stop() interrupts a long poll sleep and the task exits quickly."""
    fetcher = _StubFetcher([])
    parser = _stub_parser([[]])
    mon = BaseMonitor(
        fetcher=fetcher,
        parser=parser,
        poll_interval=10.0,  # long enough that stop() must interrupt
        source_url="https://stub",
    )
    mon.exchange = "STUB4"
    task = mon.start()
    # Let it run one tick then stop.
    await asyncio.sleep(0.1)
    mon.stop()
    await asyncio.wait_for(mon.wait_until_stopped(), timeout=2.0)
    assert task.done()


# -------------------------------------------------------------------------
# Start offset (NSE/BSE anti-phase staggering) + tick telemetry
# -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_offset_delays_first_tick(db_session):
    """With start_offset the monitor must NOT fetch before the offset
    elapses, and must fetch after it."""
    fetcher = _StubFetcher([""] * 50)
    mon = BaseMonitor(
        fetcher=fetcher,
        parser=_stub_parser([]),
        poll_interval=0.01,
        source_url="https://stub",
        start_offset=0.30,
    )
    mon.exchange = "STUB-OFF"
    mon.start()
    try:
        await asyncio.sleep(0.10)
        assert fetcher.call_count == 0, "fetched during the start offset"
        await asyncio.sleep(0.40)
        assert fetcher.call_count >= 1, "never fetched after the offset"
    finally:
        mon.stop()
        await mon.wait_until_stopped()


@pytest.mark.asyncio
async def test_start_offset_stop_during_offset_exits(db_session):
    """stop() during the offset must exit promptly without a fetch."""
    fetcher = _StubFetcher([""])
    mon = BaseMonitor(
        fetcher=fetcher,
        parser=_stub_parser([]),
        poll_interval=0.01,
        source_url="https://stub",
        start_offset=30.0,
    )
    mon.exchange = "STUB-OFF2"
    mon.start()
    await asyncio.sleep(0.05)
    mon.stop()
    await asyncio.wait_for(mon.wait_until_stopped(), timeout=2.0)
    assert fetcher.call_count == 0


@pytest.mark.asyncio
async def test_tick_stats_recorded(db_session):
    """Successful ticks publish rolling telemetry into MONITOR_TICK_STATS."""
    from app.monitors.base import MONITOR_TICK_STATS

    MONITOR_TICK_STATS.pop("STUB-STATS-API", None)
    fetcher = _StubFetcher([""] * 50)
    mon = BaseMonitor(
        fetcher=fetcher,
        parser=_stub_parser([]),
        poll_interval=0.01,
        source_url="https://stub",
    )
    mon.exchange = "STUB-STATS"
    mon.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.02)
            if MONITOR_TICK_STATS.get("STUB-STATS-API", {}).get("ticks_sampled", 0) >= 2:
                break
    finally:
        mon.stop()
        await mon.wait_until_stopped()
    stats = MONITOR_TICK_STATS.get("STUB-STATS-API")
    assert stats is not None
    assert stats["ticks_sampled"] >= 2
    assert stats["avg_tick_ms"] >= 0.0
    # The 2nd+ tick has a measured gap close to the poll interval.
    assert stats["last_gap_s"] is not None


def test_manager_staggers_bse_by_half_interval():
    """The default MonitorManager gives BSE a start offset of half the
    live poll interval and NSE none."""
    from app.monitors.manager import MonitorManager

    m = MonitorManager()
    assert m.nse._start_offset == 0.0
    expected = float(
        __import__("app.config", fromlist=["get_settings"]).get_settings().POLL_INTERVAL_SECONDS
    ) / 2.0
    assert m.bse._start_offset == pytest.approx(expected)
