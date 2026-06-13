"""Tests for `app.monitors.bse.parse_bse_payload`."""
from __future__ import annotations

from datetime import datetime, timezone

from app.monitors.bse import parse_bse_payload


# Fixture mirrors the LIVE BSE announcement response shape (probed
# 2026-06-13): each row carries SLONGNAME (long company name) and
# NSURL (the public stock-share-price URL whose last path segment
# is the trading symbol).
BSE_FIXTURE_JSON = """{
  "Table": [
    {
      "NEWSID": "abc-123",
      "SCRIP_CD": "500325",
      "SLONGNAME": "RELIANCE INDUSTRIES LTD",
      "NSURL": "https://www.bseindia.com/stock-share-price/reliance-industries-ltd/reliance/500325/",
      "HEADLINE": "Board Meeting Intimation",
      "NEWS_DT": "15 Jan 2026 09:30:00",
      "ATTACHMENTNAME": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"
    },
    {
      "NEWSID": "def-456",
      "SCRIP_CD": "532540",
      "SLONGNAME": "TCS LTD",
      "NSURL": "https://www.bseindia.com/stock-share-price/tcs-ltd/tcs/532540/",
      "NEWSSUB": "TCS - Audited Results",
      "DT_TM": "15 Jan 2026 11:45:00",
      "ATTACH_URL": "AttachLive/tcs.pdf"
    },
    {
      "NEWSID": "ghi-789",
      "SCRIP_CD": "500180",
      "SLONGNAME": "HDFC BANK LTD",
      "NSURL": "https://www.bseindia.com/stock-share-price/hdfc-bank-ltd/hdfcbank/500180/",
      "HEADLINE": "Disclosure",
      "NEWS_DT": "16 Jan 2026"
    }
  ],
  "Table1": []
}"""


def test_parse_bse_basic():
    rows = parse_bse_payload(BSE_FIXTURE_JSON, "https://bse/x")
    assert len(rows) == 3
    # The parser prefers the NSURL trading symbol (uppercased) —
    # that's the actual ticker operators recognise.
    assert rows[0].company == "RELIANCE"
    assert rows[0].title == "Board Meeting Intimation"
    assert rows[0].pdf_url == "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"
    # IST 09:30 -> UTC 04:00
    expected = datetime(2026, 1, 15, 4, 0, 0, tzinfo=timezone.utc)
    assert rows[0].posted_at == expected


def test_parse_bse_falls_back_to_alternate_field_names():
    # TCS row uses NEWSSUB / DT_TM / ATTACH_URL
    rows = parse_bse_payload(BSE_FIXTURE_JSON, "https://bse/x")
    tcs = next(r for r in rows if r.company == "TCS")
    assert tcs.title == "TCS - Audited Results"
    # ATTACH_URL is a partial path starting with AttachLive/ — we
    # host-prefix it directly.
    assert tcs.pdf_url == "https://www.bseindia.com/AttachLive/tcs.pdf"


def test_parse_bse_handles_top_level_array():
    alt = """[{"SCRIP_CD": "X", "SLONGNAME": "X Corp", "HEADLINE": "Y", "NEWS_DT": "15 Jan 2026 09:30:00"}]"""
    rows = parse_bse_payload(alt, "x")
    assert len(rows) == 1
    # SLONGNAME is preserved as-is (mixed case), unlike the NSURL
    # path which we uppercase.
    assert rows[0].company == "X Corp"


def test_parse_bse_handles_bytes():
    rows = parse_bse_payload(BSE_FIXTURE_JSON.encode("utf-8"), "x")
    assert len(rows) == 3


def test_parse_bse_skips_missing_symbol():
    bad = '{"Table": [{"HEADLINE": "Y", "NEWS_DT": "15 Jan 2026"}]}'
    assert parse_bse_payload(bad, "x") == []


def test_parse_bse_skips_missing_title():
    bad = '{"Table": [{"SCRIP_CD": "X", "NEWS_DT": "15 Jan 2026"}]}'
    assert parse_bse_payload(bad, "x") == []


def test_parse_bse_skips_missing_date():
    bad = '{"Table": [{"SCRIP_CD": "X", "HEADLINE": "Y"}]}'
    assert parse_bse_payload(bad, "x") == []


def test_parse_bse_invalid_json():
    assert parse_bse_payload("not json", "x") == []


def test_parse_bse_empty():
    assert parse_bse_payload("", "x") == []


def test_parse_bse_uses_ist_for_naive_datetime():
    rows = parse_bse_payload(BSE_FIXTURE_JSON, "x")
    hdfc = next(r for r in rows if r.company == "HDFCBANK")
    # 16 Jan 2026 00:00 IST -> 15 Jan 2026 18:30 UTC
    expected = datetime(2026, 1, 15, 18, 30, 0, tzinfo=timezone.utc)
    assert hdfc.posted_at == expected


def test_parse_bse_falls_back_to_slongname_when_nsurl_missing():
    """If NSURL is absent, fall back to SLONGNAME so we still get a
    human-readable name rather than a numeric scrip code."""
    alt = """[{"SCRIP_CD": "500325", "SLONGNAME": "RELIANCE INDUSTRIES LTD", "HEADLINE": "Board Meeting", "NEWS_DT": "15 Jan 2026 09:30:00"}]"""
    rows = parse_bse_payload(alt, "x")
    assert len(rows) == 1
    assert rows[0].company == "RELIANCE INDUSTRIES LTD"


def test_parse_bse_falls_back_to_scrip_cd_when_nothing_else_available():
    """If both NSURL and SLONGNAME are absent, fall back to the
    numeric SCRIP_CD so the row isn't dropped."""
    alt = """[{"SCRIP_CD": "500325", "HEADLINE": "Board Meeting", "NEWS_DT": "15 Jan 2026 09:30:00"}]"""
    rows = parse_bse_payload(alt, "x")
    assert len(rows) == 1
    assert rows[0].company == "500325"


def test_parse_bse_extracts_trading_symbol_from_nsurl_path():
    """NSURL ends in /<scrip_cd>/ — the trading symbol is the segment
    immediately before the scrip code, e.g.
    /pds-ltd/pdsl/538730/  ->  'pdsl' (we uppercase it)."""
    alt = """[{"SCRIP_CD": "538730", "NSURL": "https://www.bseindia.com/stock-share-price/pds-ltd/pdsl/538730/", "HEADLINE": "x", "NEWS_DT": "15 Jan 2026 09:30:00"}]"""
    rows = parse_bse_payload(alt, "x")
    assert rows[0].company == "PDSL"
