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
    """A throwaway store per test.

    The module caches its connection in a thread-local, and other tests in the
    suite may have opened the REAL store on this thread already. Closing on both
    sides of the test keeps that cache from leaking a stale handle either way —
    without it the suite passes alone and fails intermittently in full runs.
    """
    # shutdown(), not close(): the instance is process-wide now, and pointing
    # STORE at a temp file means nothing while the old instance is still open.
    W.shutdown()
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    con = W.connect()
    yield con
    W.shutdown()


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


def test_cursors_are_independent_across_threads(store):
    """One instance, many cursors — the model the API depends on.

    Opening a second CONNECTION per thread is what raised "Cannot switch
    temporary directory after the current one has been used" and 500'd every
    dataset endpoint as soon as a second request arrived.
    """
    import threading

    W.ingest_live(**_row(content_hash="h1"))
    errors: list[str] = []

    def read():
        try:
            W.connect().execute(f"select count(*) from {W.TABLE}").fetchone()
        except Exception as e:  # noqa: BLE001
            errors.append(str(e)[:120])

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent cursors failed: {errors}"


def test_sql_console_rejects_writes():
    """Write protection lives in the statement screen, not the connection.

    The API shares the process's read-write instance (it has to — the monitor
    holds it to mirror filings), so this screen is the only thing standing
    between a typed query and the dataset.
    """
    from app.api.warehouse import _FORBIDDEN

    for bad in ("delete from announcements",
                "update announcements set symbol='x'",
                "drop table announcements",
                "attach '/etc/passwd' as x",
                "copy announcements to '/tmp/x.csv'",
                "select * from read_parquet('/etc/hosts')"):
        assert _FORBIDDEN.search(bad), f"not blocked: {bad}"
