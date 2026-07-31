"""The two things in candle_sync that fail silently rather than loudly:
a wrong timezone (prices land on the wrong minute) and a broken dedupe
(the same day appended twice, doubling volume)."""
from __future__ import annotations

import datetime as dt
from datetime import datetime

import pytest

from app.services import candle_sync

duckdb = pytest.importorskip("duckdb")


def test_epoch_converts_to_naive_ist():
    """Fyers sends epoch UTC; the store is naive IST. 03:45Z = 09:15 IST —
    the open. Off by 5h30m and every px_* offset points at the wrong candle.
    """
    # 2026-07-24 03:45:00Z -> 09:15 IST (session open)
    rows = candle_sync._candle_rows([[1784864700, 100.0, 101.0, 99.0, 100.5, 1234]])
    assert len(rows) == 1
    ts, dt, o, h, l, c, v = rows[0]
    assert dt == datetime(2026, 7, 24, 9, 15), dt
    assert dt.tzinfo is None, "the parquet column is naive; a tz-aware value shifts it again"
    assert (o, h, l, c, v) == (100.0, 101.0, 99.0, 100.5, 1234)
    assert ts == 1784864700


def test_malformed_candles_are_dropped_not_crashed():
    assert candle_sync._candle_rows([]) == []
    assert candle_sync._candle_rows([[1, 2, 3]]) == []          # too short
    assert candle_sync._candle_rows([["x", 1, 2, 3, 4, 5]]) == []  # unparseable


def test_append_is_idempotent(tmp_path, monkeypatch):
    """Re-running the same day must add nothing. Without the ts anti-join a
    re-run doubles day_volume, which silently corrupts every volume feature.
    """
    monkeypatch.setattr(candle_sync, "CANDLES", tmp_path)
    raw = [[1784864700 + i * 60, 100.0, 101.0, 99.0, 100.0 + i, 10 * (i + 1)]
           for i in range(5)]
    rows = candle_sync._candle_rows(raw)

    assert candle_sync._append("TESTSYM", rows) == 5
    assert candle_sync._append("TESTSYM", rows) == 0        # idempotent

    # one new minute merges in without disturbing the rest
    more = candle_sync._candle_rows([[1784864700 + 5 * 60, 1.0, 2.0, 0.5, 1.5, 7]])
    assert candle_sync._append("TESTSYM", more) == 1

    from app.services import warehouse_prices as P

    monkeypatch.setattr(P, "CANDLES", tmp_path)
    src = P.candle_source("TESTSYM")
    con = duckdb.connect()
    n, distinct = con.execute(
        f"SELECT count(*), count(DISTINCT ts) FROM {src}").fetchone()
    assert n == distinct == 6, "duplicate ts rows survived the merge"
    # ordered by ts, so the price fill's BETWEEN window scans cleanly
    assert con.execute(
        f"SELECT ts FROM {src} ORDER BY ts LIMIT 1").fetchone()[0] == 1784864700


def test_retry_window_is_bounded(tmp_path, monkeypatch):
    """`no_candles` rows are retried only inside the window this run can reach.

    Without the bound every past filing is re-fetched every night forever —
    one Fyers history call per symbol, for rows whose candles Fyers no longer
    serves. The daily job must stay proportional to the day's news.
    """
    from app.services import warehouse_store as W

    W.shutdown()
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    con = W.connect()
    try:
        now = dt.datetime.now()
        W.ingest_live(symbol="FRESH", headline="recent filing",
                      announced_at=now - dt.timedelta(days=2))
        W.ingest_live(symbol="ANCIENT", headline="old filing",
                      announced_at=now - dt.timedelta(days=400))
        con.execute("UPDATE announcements SET price_status='no_candles'")

        syms = candle_sync.pending_symbols(days=90)

        assert syms[0] == candle_sync.NIFTY_SYMBOL, "the index must never be skipped"
        assert "FRESH" in syms
        assert "ANCIENT" not in syms, "unreachable row was queued for a pointless fetch"
        # and the out-of-window row keeps its status rather than churning
        assert con.execute("SELECT price_status FROM announcements "
                           "WHERE symbol='ANCIENT'").fetchone()[0] == "no_candles"
    finally:
        W.shutdown()


def test_increments_read_back_as_one_series(tmp_path, monkeypatch):
    """Appends land in monthly increment files, and candle_source() stitches
    base + increments back into one table. If that read is wrong the price fill
    silently sees a gap and marks perfectly good rows no_candles.
    """
    from app.services import warehouse_prices as P

    monkeypatch.setattr(candle_sync, "CANDLES", tmp_path)
    monkeypatch.setattr(P, "CANDLES", tmp_path)

    # two different months, appended separately
    jul = [[1784864700 + i * 60, 1.0, 2.0, 0.5, 100.0 + i, 10] for i in range(3)]
    aug = [[1787543100 + i * 60, 1.0, 2.0, 0.5, 200.0 + i, 20] for i in range(3)]
    assert candle_sync._append("ACME", candle_sync._candle_rows(jul)) == 3
    assert candle_sync._append("ACME", candle_sync._candle_rows(aug)) == 3
    assert candle_sync._append("ACME", candle_sync._candle_rows(jul)) == 0  # idempotent

    months = sorted(p.name for p in (tmp_path / "_inc" / "ACME").glob("*.parquet"))
    assert len(months) == 2, f"expected one file per month, got {months}"

    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM {P.candle_source('ACME')}").fetchone()[0]
    assert n == 6, "base + increments did not read back as one series"

    # the glob must not bleed across symbols with a shared prefix
    assert candle_sync._append("ACMEPOWER", candle_sync._candle_rows(jul)) == 3
    assert con.execute(
        f"SELECT count(*) FROM {P.candle_source('ACME')}").fetchone()[0] == 6


def test_ripe_excludes_announcements_too_new_to_price(tmp_path, monkeypatch):
    """px_60m needs 60 minutes of trading. Fetching sooner cannot fill the row,
    it just marks it no_candles and spends a Fyers call to do so.
    """
    from app.services import warehouse_store as W

    W.shutdown()
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    W.connect()
    try:
        now = dt.datetime.now()
        W.ingest_live(symbol="RIPE", headline="filed two hours ago",
                      announced_at=now - dt.timedelta(hours=2))
        W.ingest_live(symbol="TOONEW", headline="filed five minutes ago",
                      announced_at=now - dt.timedelta(minutes=5))

        syms = candle_sync.ripe_symbols(max_age_days=3, min_age_minutes=61)

        assert "RIPE" in syms
        assert "TOONEW" not in syms, "fetched before px_60m could possibly exist"
    finally:
        W.shutdown()
