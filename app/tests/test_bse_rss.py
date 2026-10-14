"""BSE official RSS feed — parser + scrip-code resolver.

The fixture is the real feed shape observed 2026-10-14 on
www.bseindia.com/data/xml/announcements.xml (BOM included), and the CSV
rows are real Fyers BSE_CM.csv rows.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.monitors.bse_rss import BSERSSMonitor, ScripCodeResolver, parse_bse_rss_payload

RSS = """﻿<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>
<title>BSE - Latest Corporate Announcements</title>
<item><title>Exhicon Events Media Solutions Ltd (543895)</title>
<link>https://www.bseindia.com/xml-data/corpfiling/AttachLive/0398da0b-f99e-441d-ba6a-1b7812593ffa.pdf</link>
<scripcode>543895</scripcode><description>Intimation is enclosed herewith</description>
<pubDate>14-Oct-2026 09:26:44</pubDate></item>
<item><title>Unknown Co Ltd (999999)</title><link>https://x/y.pdf</link>
<scripcode>999999</scripcode><description>Something</description>
<pubDate>14-Oct-2026 09:25:00</pubDate></item>
<item><title>Park Medi World Ltd (544645)</title><link></link>
<scripcode>544645</scripcode><description></description>
<pubDate>14-Oct-2026 09:23:50</pubDate></item>
</channel></rss>"""

BSE_CM = (
    "1210000000543895,Exhicon Events Media Solutions,50,250,0.05,INE0O8901016,"
    "0915-1530|1815-1915:,2026-10-05,,BSE:EXHICON-M,12,10,543895,EXHICON,543895,-1.0,XX\n"
    "1210000000544645,PARK MEDI WORLD LIMITED,50,1,0.05,INE119201023,"
    "0915-1530|1815-1915:,2026-10-05,,BSE:PARKHOSPS-B,12,10,544645,PARKHOSPS,544645,-1.0,XX\n"
    "1210000000999901,SENSEX,10,1,0.05,,0915-1530|1815-1915:,2026-10-05,,BSE:SENSEX-INDEX,12,10,1,SENSEX,1,-1.0,XX\n"
)


def _resolver() -> ScripCodeResolver:
    r = ScripCodeResolver()
    assert r.load_csv_text(BSE_CM) == 2  # the index row is skipped
    return r


def test_parse_maps_scrip_code_to_symbol_and_drops_unknown():
    out = parse_bse_rss_payload(RSS, "u", resolver=_resolver())
    assert [a.company for a in out] == ["EXHICON", "PARKHOSPS"]
    a = out[0]
    assert a.title == "Intimation is enclosed herewith"
    assert a.pdf_url.endswith("0398da0b-f99e-441d-ba6a-1b7812593ffa.pdf")
    # 09:26:44 IST == 03:56:44 UTC
    assert a.posted_at == datetime(2026, 10, 14, 3, 56, 44, tzinfo=timezone.utc)
    # Empty description falls back to the company name; empty link -> None.
    assert out[1].title == "Park Medi World Ltd"
    assert out[1].pdf_url is None


def test_empty_payload_is_zero_items():
    assert parse_bse_rss_payload("", "u", resolver=_resolver()) == []


def test_monitor_defaults():
    mon = BSERSSMonitor(poll_interval=1.0, resolver=_resolver())
    assert (mon.exchange, mon.channel) == ("BSE", "RSS")
    assert mon.source_url.endswith("/data/xml/announcements.xml")
