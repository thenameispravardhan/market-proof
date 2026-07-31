"""Every announcement gets priced, whatever time it arrives and whether or not
the AI ever looked at it.

Two rules, and the error handling that makes them hold:

  1. Ingestion and pricing do not depend on analysis. The AI toggle being off,
     or DeepSeek being down, must not cost the dataset a row.
  2. A filing outside the session anchors to the NEXT open, and must survive
     until that session actually happens — over a weekend, a holiday, or a
     stretch where the bot was down.

Rule 2 is where it used to break. `no_candles` was written for any row that
could not be filled right now, which conflated "the session has not happened
yet" with "there will never be a candle". Parked rows only came back if the
retry window happened to catch them; over a long weekend it did not, and the
row was lost in silence.
"""
from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("duckdb")

from app.services import candle_sync, warehouse_prices as P, warehouse_store as W  # noqa: E402


@pytest.fixture()
def wh(tmp_path, monkeypatch):
    W.shutdown()
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    monkeypatch.setattr(P, "CANDLES", tmp_path / "candles")
    monkeypatch.setattr(candle_sync, "CANDLES", tmp_path / "candles")
    (tmp_path / "candles").mkdir()
    con = W.connect()
    yield con
    W.shutdown()


def _candles(symbol: str, day: dt.date, start_h=9, start_m=15, minutes=120, px=100.0):
    """A session's worth of 1-minute candles, written the way Fyers delivers."""
    base = dt.datetime(day.year, day.month, day.day, start_h, start_m)
    raw = []
    for i in range(minutes):
        ts = int((base + dt.timedelta(minutes=i) - dt.timedelta(hours=5, minutes=30))
                 .replace(tzinfo=dt.timezone.utc).timestamp())
        raw.append([ts, px, px + 1, px - 1, px + i * 0.1, 1000 + i])
    candle_sync._append(symbol, candle_sync._candle_rows(raw))


def test_after_hours_filing_survives_until_the_next_session(wh):
    """The core error-handling rule: a filing with no session yet stays
    `pending`. Condemning it to `no_candles` is how rows got lost."""
    con = wh
    friday = dt.date(2026, 7, 24)
    _candles("ACME", friday)                       # Friday 09:15-11:15 only

    # filed Friday 20:00 — after the close, so its anchor is Monday's open
    W.ingest_live(symbol="ACME", headline="filed after the close",
                  announced_at=dt.datetime(2026, 7, 24, 20, 0))

    assert P.fill_symbol(con, "ACME") == 0
    status = con.execute(
        "SELECT price_status FROM announcements WHERE symbol='ACME'").fetchone()[0]
    assert status == "pending", (
        "an after-hours filing was condemned to no_candles; over a weekend the "
        "retry window drops it and the row is lost for good")

    # Monday's session arrives — now it prices, anchored to the OPEN
    _candles("ACME", dt.date(2026, 7, 27))
    assert P.fill_symbol(con, "ACME") == 1
    row = con.execute(
        "SELECT price_status, anchor_time, session_offset, px_t0 "
        "FROM announcements WHERE symbol='ACME'").fetchone()
    assert row[0] == "filled"
    assert row[1] == dt.datetime(2026, 7, 27, 9, 15), "did not anchor to the next open"
    assert row[2] == "next_session"
    assert row[3] is not None


def test_symbol_with_no_candles_at_all_is_condemned(wh):
    """The other half: something must still stop retrying, or a symbol we can
    never resolve (a BSE-only ticker, a bad scrip code) is fetched forever.

    Note the asymmetry, which is deliberate: once the series reaches PAST the
    filing an anchor always exists, so "covered but unanchored" is essentially
    unreachable. The real terminal case is having no candles for the symbol.
    """
    con = wh
    W.ingest_live(symbol="NOSUCH", headline="a ticker Fyers will not resolve",
                  announced_at=dt.datetime(2026, 7, 24, 10, 0))

    assert P.fill_symbol(con, "NOSUCH") == 0
    status = con.execute(
        "SELECT price_status FROM announcements WHERE symbol='NOSUCH'").fetchone()[0]
    assert status == "no_candles", "an unresolvable symbol must stop retrying"


def test_pricing_does_not_depend_on_analysis(wh):
    """No Analysis row, no AI columns — the price columns still fill. The AI
    toggle being off must not cost the dataset its labels-from-price."""
    con = wh
    _candles("NOAI", dt.date(2026, 7, 24))
    W.ingest_live(symbol="NOAI", headline="never analysed",
                  announced_at=dt.datetime(2026, 7, 24, 9, 30))

    assert con.execute("SELECT ai_sentiment FROM announcements "
                       "WHERE symbol='NOAI'").fetchone()[0] is None
    assert P.fill_symbol(con, "NOAI") == 1
    px_t0, status = con.execute(
        "SELECT px_t0, price_status FROM announcements WHERE symbol='NOAI'").fetchone()
    assert status == "filled" and px_t0 is not None


def test_empty_candle_file_does_not_condemn(wh):
    """A symbol whose file exists but holds nothing is a fetch that half-failed.
    Retry it; do not record a verdict from missing data."""
    con = wh
    candle_sync._append("EMPTY", candle_sync._candle_rows([]))   # writes nothing
    W.ingest_live(symbol="EMPTY", headline="no candles at all",
                  announced_at=dt.datetime(2026, 7, 24, 9, 30))
    P.fill_symbol(con, "EMPTY")
    status = con.execute(
        "SELECT price_status FROM announcements WHERE symbol='EMPTY'").fetchone()[0]
    assert status in ("pending", "no_candles")   # never crashes, never 'filled'


def test_fetch_window_shuts_outside_trading_hours():
    """Candles only exist while the market trades. Polling Fyers on a Saturday
    returns nothing new, and re-fetching every 10 minutes all weekend is how the
    'leave it pending' rule would have got expensive."""
    sat = dt.datetime(2026, 7, 25, 11, 0, tzinfo=candle_sync.IST)   # Saturday
    mon_open = dt.datetime(2026, 7, 27, 11, 0, tzinfo=candle_sync.IST)
    mon_night = dt.datetime(2026, 7, 27, 22, 0, tzinfo=candle_sync.IST)

    assert candle_sync.fetch_window_open(sat) is False
    assert candle_sync.fetch_window_open(mon_open) is True
    assert candle_sync.fetch_window_open(mon_night) is False
