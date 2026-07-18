"""NSE RSS racer channel — parser, name resolver, dedupe, and the
multi-source manager wiring.

The RSS fixture is the real item observed live on
nsearchives.nseindia.com/content/RSS/Online_announcements.xml (the
Vedant Fashions board-meeting intimation) — the parser is tested against
exactly what NSE serves.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import Announcement
from app.monitors.base import BaseMonitor, RawAnnouncement
from app.monitors.nse_rss import (
    NameResolver,
    NSERSSMonitor,
    normalize_company_name,
    parse_nse_rss_payload,
    parse_pub_date,
)

pytestmark = pytest.mark.usefixtures("isolated_db")


RSS_FIXTURE = """<rss xmlns:atom="http://www.w3.org/2005/Atom" version="2.0"><channel>
<title>NSE News - Latest Announcements</title>
<item><title>Vedant Fashions Limited</title>
<link>https://nsearchives.nseindia.com/corporate/xbrl/IntimationBM25072026_PRIOR_INTIMATION_WebXMLFile_20260719_000248746.xml</link>
<description>VEDANT FASHIONS LIMITED has informed the Exchange about Board Meeting to be held on 25-Jul-2026 to inter-alia consider and approve the Unaudited Financial results of the Company for the Quarterly ended June 2026 . |SUBJECT: Board Meeting Intimation</description>
<pubDate>19-Jul-2026 00:02:49</pubDate></item>
<item><title>Unknown Widgets Private Limited</title>
<link>https://example.com/x.pdf</link>
<description>Some text |SUBJECT: Other</description>
<pubDate>19-Jul-2026 00:03:00</pubDate></item>
</channel></rss>"""

EQUITY_CSV = (
    "SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING\n"
    "MANYAVAR,Vedant Fashions Limited,EQ,08-FEB-2022\n"
    "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995\n"
)


def _resolver() -> NameResolver:
    r = NameResolver()
    r.load_csv_text(EQUITY_CSV)
    return r


# ---- name normalization -----------------------------------------------


def test_normalize_company_name_variants():
    assert normalize_company_name("Vedant Fashions Limited") == "VEDANT FASHIONS"
    assert normalize_company_name("VEDANT  FASHIONS LTD.") == "VEDANT FASHIONS"
    assert normalize_company_name("Vedant Fashions") == "VEDANT FASHIONS"
    # Punctuation is stripped, & is kept (e.g. L&T-style names).
    assert normalize_company_name("Larsen & Toubro Ltd") == "LARSEN & TOUBRO"


def test_resolver_exact_match_only():
    r = _resolver()
    assert r.resolve("Vedant Fashions Limited") == "MANYAVAR"
    assert r.resolve("VEDANT FASHIONS LTD") == "MANYAVAR"
    assert r.resolve("Vedant Fashion") is None  # near-miss must NOT match


def test_resolver_rejects_non_master_payload():
    r = NameResolver()
    with pytest.raises(ValueError):
        r.load_csv_text("<html>not a csv</html>\nfoo,bar")


# ---- pubDate parsing ---------------------------------------------------


def test_parse_pub_date_ist_to_utc():
    dt = parse_pub_date("19-Jul-2026 00:02:49")
    assert dt == datetime(2026, 7, 18, 18, 32, 49, tzinfo=timezone.utc)


def test_parse_pub_date_garbage_is_none():
    assert parse_pub_date("not a date") is None
    assert parse_pub_date("") is None


# ---- payload parsing ---------------------------------------------------


def test_parse_rss_resolves_symbol_and_drops_unknown():
    out = parse_nse_rss_payload(RSS_FIXTURE, "https://stub", resolver=_resolver())
    # Vedant resolves to MANYAVAR; "Unknown Widgets" is dropped (fail-safe).
    assert len(out) == 1
    a = out[0]
    assert a.company == "MANYAVAR"
    assert a.title.startswith("VEDANT FASHIONS LIMITED has informed the Exchange")
    assert "|SUBJECT" not in a.title
    assert a.pdf_url and a.pdf_url.endswith(".xml")
    assert a.posted_at == datetime(2026, 7, 18, 18, 32, 49, tzinfo=timezone.utc)


def test_parse_rss_bytes_input():
    out = parse_nse_rss_payload(
        RSS_FIXTURE.encode("utf-8"), "https://stub", resolver=_resolver()
    )
    assert len(out) == 1


# ---- cross-source dedupe ----------------------------------------------


@pytest.mark.asyncio
async def test_cross_source_dedupe_same_headline_close_timestamps(db_session):
    """The same filing via RSS (t) and API (t+1s) has different content
    hashes but identical headline text — only ONE row may result."""

    async def fetcher(url):
        return ""

    headline = "VEDANT FASHIONS LIMITED has informed the Exchange about Board Meeting"
    t0 = datetime(2026, 7, 18, 18, 32, 48, tzinfo=timezone.utc)
    raw_rss = RawAnnouncement(
        company="MANYAVAR", title=headline, posted_at=t0.replace(second=49),
        pdf_url="https://nsearchives.nseindia.com/x.xml",
    )
    raw_api = RawAnnouncement(
        company="MANYAVAR", title=headline, posted_at=t0,  # 1s earlier
        pdf_url="https://nsearchives.nseindia.com/x.pdf",
    )
    mon = BaseMonitor(
        fetcher=fetcher, parser=lambda raw, url: [], poll_interval=0.01,
        source_url="https://stub",
    )
    mon.exchange = "NSE"
    await mon._insert_and_announce(raw_rss)
    await mon._insert_and_announce(raw_api)

    rows = db_session.execute(select(Announcement)).scalars().all()
    assert len(rows) == 1, "cross-source duplicate was inserted"


@pytest.mark.asyncio
async def test_different_filings_same_symbol_both_insert(db_session):
    """Different headlines on the same symbol are NOT deduped."""

    async def fetcher(url):
        return ""

    t0 = datetime(2026, 7, 18, 10, 0, 0, tzinfo=timezone.utc)
    a = RawAnnouncement(company="MANYAVAR", title="Filing one", posted_at=t0)
    b = RawAnnouncement(company="MANYAVAR", title="Filing two", posted_at=t0)
    mon = BaseMonitor(
        fetcher=fetcher, parser=lambda raw, url: [], poll_interval=0.01,
        source_url="https://stub",
    )
    mon.exchange = "NSE"
    await mon._insert_and_announce(a)
    await mon._insert_and_announce(b)
    rows = db_session.execute(select(Announcement)).scalars().all()
    assert len(rows) == 2


# ---- enabled_fn (frontend toggle) --------------------------------------


@pytest.mark.asyncio
async def test_disabled_monitor_never_fetches(db_session):
    calls = {"n": 0}

    async def fetcher(url):
        calls["n"] += 1
        return ""

    mon = BaseMonitor(
        fetcher=fetcher, parser=lambda raw, url: [], poll_interval=0.01,
        source_url="https://stub", enabled_fn=lambda: False,
    )
    mon.exchange = "STUB-DISABLED"
    mon.start()
    await asyncio.sleep(0.1)
    mon.stop()
    await mon.wait_until_stopped()
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_toggle_flips_live(db_session):
    """Flipping the enabled flag mid-run starts ticking without restart."""
    calls = {"n": 0}
    enabled = {"on": False}

    async def fetcher(url):
        calls["n"] += 1
        return ""

    mon = BaseMonitor(
        fetcher=fetcher, parser=lambda raw, url: [], poll_interval=0.01,
        source_url="https://stub", enabled_fn=lambda: enabled["on"],
    )
    mon.exchange = "STUB-TOGGLE"
    mon.start()
    await asyncio.sleep(0.06)
    assert calls["n"] == 0
    enabled["on"] = True
    await asyncio.sleep(0.2)
    mon.stop()
    await mon.wait_until_stopped()
    assert calls["n"] >= 1


# ---- manager wiring ----------------------------------------------------


def test_manager_builds_three_sources():
    from app.monitors.manager import MonitorManager

    m = MonitorManager()
    labels = [f"{x.exchange}-{x.channel}" for x in m.monitors]
    assert labels == ["NSE-API", "BSE-API", "NSE-RSS"]
    # Offsets spread across the poll cycle.
    assert m.nse._start_offset == 0.0
    assert m.bse._start_offset > 0.0
    assert m.nse_rss._start_offset > m.bse._start_offset


def test_nse_rss_monitor_defaults():
    mon = NSERSSMonitor(poll_interval=1.0)
    assert mon.exchange == "NSE"
    assert mon.channel == "RSS"
    assert "Online_announcements.xml" in mon.source_url
