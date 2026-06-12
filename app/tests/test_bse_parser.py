"""Tests for `app.monitors.bse.parse_bse_payload`."""
from __future__ import annotations

from datetime import datetime, timezone

from app.monitors.bse import parse_bse_payload


BSE_FIXTURE_JSON = """{
  "Table": [
    {
      "SCRIP_CD": "500325",
      "COMPANY_NAME": "RELIANCE INDUSTRIES LTD",
      "HEADLINE": "Board Meeting Intimation",
      "NEWS_DT": "15 Jan 2026 09:30:00",
      "ATTACHMENTNAME": "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"
    },
    {
      "SCRIP_CD": "532540",
      "COMPANY_NAME": "TCS LTD",
      "NEWSSUB": "TCS - Audited Results",
      "DT_TM": "15 Jan 2026 11:45:00",
      "ATTACH_URL": "AttachLive/tcs.pdf"
    },
    {
      "SCRIP_CD": "500180",
      "COMPANY_NAME": "HDFC BANK LTD",
      "HEADLINE": "Disclosure",
      "NEWS_DT": "16 Jan 2026"
    }
  ],
  "Table1": []
}"""


def test_parse_bse_basic():
    rows = parse_bse_payload(BSE_FIXTURE_JSON, "https://bse/x")
    assert len(rows) == 3
    assert rows[0].company == "500325"
    assert rows[0].title == "Board Meeting Intimation"
    assert rows[0].pdf_url == "https://www.bseindia.com/xml-data/corpfiling/AttachLive/abc.pdf"
    # IST 09:30 -> UTC 04:00
    expected = datetime(2026, 1, 15, 4, 0, 0, tzinfo=timezone.utc)
    assert rows[0].posted_at == expected


def test_parse_bse_falls_back_to_alternate_field_names():
    # TCS row uses NEWSSUB / DT_TM / ATTACH_URL
    rows = parse_bse_payload(BSE_FIXTURE_JSON, "https://bse/x")
    tcs = next(r for r in rows if r.company == "532540")
    assert tcs.title == "TCS - Audited Results"
    # ATTACH_URL is a partial path starting with AttachLive/ — we
    # host-prefix it directly.
    assert tcs.pdf_url == "https://www.bseindia.com/AttachLive/tcs.pdf"


def test_parse_bse_handles_top_level_array():
    alt = """[{"SCRIP_CD": "X", "HEADLINE": "Y", "NEWS_DT": "15 Jan 2026 09:30:00"}]"""
    rows = parse_bse_payload(alt, "x")
    assert len(rows) == 1


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
    hdfc = next(r for r in rows if r.company == "500180")
    # 16 Jan 2026 00:00 IST -> 15 Jan 2026 18:30 UTC
    expected = datetime(2026, 1, 15, 18, 30, 0, tzinfo=timezone.utc)
    assert hdfc.posted_at == expected
