"""The unified dataset: identity, dedup, timezone, and read-only safety.

These guard the two bugs that actually bit while building it — a uid that
collapsed genuinely distinct filings, and a 5h30m timezone gap that would have
stored 15,743 announcements twice.
"""
from __future__ import annotations

import datetime as dt

import pytest

duckdb = pytest.importorskip("duckdb")

from app.services import warehouse_store as W  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    W.close()
    con = W.connect()
    yield con
    W.close()


def _row(**kw):
    base = dict(symbol="ACME", headline="ACME Ltd has informed the Exchange about X",
                announced_at=dt.datetime(2026, 7, 29, 14, 30, 15), exchange="NSE")
    base.update(kw)
    return base


def test_insert_then_duplicate_is_rejected(store):
    assert W.ingest_live(**_row(content_hash="h1")) is True
    assert W.ingest_live(**_row(content_hash="h1")) is False
    assert store.execute(f"select count(*) from {W.TABLE}").fetchone()[0] == 1


def test_distinct_filings_in_the_same_second_both_survive(store):
    """NSE publishes several attachments per second under one headline; one
    historical group has 9. Collapsing them would silently drop filings."""
    assert W.ingest_live(**_row(content_hash="h1")) is True
    assert W.ingest_live(**_row(content_hash="h2")) is True
    assert store.execute(f"select count(*) from {W.TABLE}").fetchone()[0] == 2
    uids = [r[0] for r in store.execute(f"select uid from {W.TABLE}").fetchall()]
    assert len(set(uids)) == 2, "distinct filings must get distinct uids"


def test_new_row_lands_pending_with_no_prices(store):
    W.ingest_live(**_row(content_hash="h1"))
    r = store.execute(
        f"select price_status, px_t0, adj_30m, ai_sentiment from {W.TABLE}").fetchone()
    assert r == ("pending", None, None, None)


def test_analysis_attaches_to_an_existing_row(store):
    W.ingest_live(**_row(content_hash="h1", event_id=42))
    W.apply_analysis(42, {"sentiment": "positive", "sentiment_score": 60,
                          "confidence": 0.8, "recommendation": "BUY",
                          "event_type": "ORDER_WIN"})
    r = store.execute(
        f"select ai_sentiment, ai_sentiment_score, ai_recommendation, price_status "
        f"from {W.TABLE} where event_id=42").fetchone()
    assert r == ("positive", 60.0, "BUY", "pending")


def test_uid_is_timezone_sensitive(store):
    """The live DB is UTC and the dataset is IST. Storing an unconverted
    timestamp puts the same filing 5h30m from its historical twin, under a
    different uid — which is exactly how 15,743 duplicates nearly shipped."""
    W.ingest_live(**_row(content_hash="h1"))
    W.ingest_live(**_row(content_hash="h2",
                         announced_at=dt.datetime(2026, 7, 29, 9, 0, 15)))
    uids = sorted(r[0] for r in store.execute(f"select uid from {W.TABLE}").fetchall())
    assert uids[0] != uids[1]
    assert "20260729143015" in uids[1] or "20260729143015" in uids[0]


def test_symbol_and_time_are_required(store):
    with pytest.raises(Exception):
        W.ingest_live(symbol="", headline="x", announced_at=None)


def test_read_only_connection_cannot_write(store, tmp_path):
    W.ingest_live(**_row(content_hash="h1"))
    W.close()
    ro = duckdb.connect(str(W.STORE), read_only=True)
    with pytest.raises(Exception):
        ro.execute(f"delete from {W.TABLE}")
    ro.close()
