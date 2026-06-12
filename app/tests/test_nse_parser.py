"""Tests for `app.monitors.nse.parse_nse_payload` — pure parser on
captured HTML / JSON fixtures.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.monitors.nse import parse_nse_payload


NSE_FIXTURE_JSON = """[
  {
    "symbol": "RELIANCE",
    "sm_name": "Reliance Industries Limited",
    "desc": "Reliance Industries Limited - Board Meeting Intimation",
    "attchmntFile": "https://nseindia.com/x/y/abc.pdf",
    "an_dt": "15-Jan-2026 09:30:00"
  },
  {
    "symbol": "TCS",
    "sm_name": "Tata Consultancy Services Limited",
    "desc": "TCS - Submission Of Audited Financial Results",
    "attchmntFile": "https://nseindia.com/x/y/tcs.pdf",
    "an_dt": "15-Jan-2026 11:45:12"
  },
  {
    "symbol": "HDFCBANK",
    "sm_name": "HDFC Bank Limited",
    "desc": "HDFC Bank - Disclosure Under Regulation 30",
    "attchmntFile": "",
    "an_dt": "16-Jan-2026"
  }
]"""


def test_parse_nse_basic():
    rows = parse_nse_payload(NSE_FIXTURE_JSON, "https://nse/x")
    assert len(rows) == 3
    assert rows[0].company == "RELIANCE"
    assert "Board Meeting" in rows[0].title
    assert rows[0].pdf_url == "https://nseindia.com/x/y/abc.pdf"
    # Naive IST datetime -> UTC.
    expected = datetime(2026, 1, 15, 4, 0, 0, tzinfo=timezone.utc)
    assert rows[0].posted_at == expected
    assert rows[0].posted_at.tzinfo is not None


def test_parse_nse_handles_wrapped_response():
    wrapped = '{"data": ' + NSE_FIXTURE_JSON + '}'
    rows = parse_nse_payload(wrapped, "https://nse/x")
    assert len(rows) == 3


def test_parse_nse_handles_bytes():
    rows = parse_nse_payload(NSE_FIXTURE_JSON.encode("utf-8"), "https://nse/x")
    assert len(rows) == 3


def test_parse_nse_skips_missing_symbol():
    bad = '[{"sm_name": "X", "desc": "Y", "an_dt": "15-Jan-2026 09:30:00"}]'
    assert parse_nse_payload(bad, "https://nse/x") == []


def test_parse_nse_skips_missing_title():
    bad = '[{"symbol": "X", "an_dt": "15-Jan-2026 09:30:00"}]'
    assert parse_nse_payload(bad, "https://nse/x") == []


def test_parse_nse_skips_missing_date():
    bad = '[{"symbol": "X", "desc": "Y"}]'
    assert parse_nse_payload(bad, "https://nse/x") == []


def test_parse_nse_skips_unparseable_date():
    bad = '[{"symbol": "X", "desc": "Y", "an_dt": "not a date"}]'
    assert parse_nse_payload(bad, "https://nse/x") == []


def test_parse_nse_skips_non_dict_rows():
    bad = '[{"symbol": "X", "desc": "Y", "an_dt": "15-Jan-2026 09:30:00"}, "not a dict"]'
    rows = parse_nse_payload(bad, "https://nse/x")
    assert len(rows) == 1


def test_parse_nse_empty_string():
    assert parse_nse_payload("", "x") == []
    assert parse_nse_payload("   ", "x") == []


def test_parse_nse_invalid_json():
    assert parse_nse_payload("not json at all", "x") == []


def test_parse_nse_blank_pdf_is_none():
    rows = parse_nse_payload(NSE_FIXTURE_JSON, "x")
    blank = [r for r in rows if r.company == "HDFCBANK"]
    assert blank and blank[0].pdf_url is None
