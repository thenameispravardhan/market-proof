"""The announcement dataset must honour what it advertises.

Every bug on the Dataset page this session came from one shape: the endpoint
serves two different GRAINS (per-signal and per-announcement) through one set
of query params, and the announcement branch quietly disagreed with the signal
branch about what those params mean.

  - columns were validated against the SIGNAL catalog, so 94 of the 97
    announcement columns were dropped before the query ran and the table
    rendered "—" under correct headers
  - enriched_only was honoured on one branch and ignored on the other
  - action/taken/label were accepted and ignored, handing back UNFILTERED
    rows to a caller who believed they were filtered

Each was invisible: no error, no log, just wrong data. These lock the contract
so the next divergence fails in CI instead of in a training set.
"""
from __future__ import annotations

import pytest

pytest.importorskip("duckdb")

from app.services import warehouse_store as W  # noqa: E402


@pytest.fixture()
def store(tmp_path, monkeypatch, isolated_db):
    """A throwaway warehouse, and a cleaned SQLite after.

    `isolated_db` matters: the `client` fixture boots the real app, whose
    startup seeds the default prompt templates. Without the cleanup those rows
    outlive this module and the next test to insert a template trips
    UNIQUE(prompt_templates.event_type).
    """
    W.shutdown()
    monkeypatch.setattr(W, "STORE", tmp_path / "wh.duckdb")
    W.connect()
    yield
    W.shutdown()


def test_every_advertised_column_is_actually_returnable(client, store):
    """The catalog is a promise. If /columns lists it, /dataset must return it.

    This is the one that bit: asking for 13 columns returned 3, because the
    selection was filtered against the wrong grain's catalog first.
    """
    W.ingest_live(symbol="ACME", headline="ACME Ltd has informed the Exchange",
                  announced_at="2026-07-29 10:00:00", exchange="NSE")

    catalog = client.get("/api/dataset/columns?source=announcements").json()
    keys = [c["key"] for c in catalog["columns"]]
    assert len(keys) > 50, "catalog looks empty — the rest of this proves nothing"

    r = client.get("/api/dataset?source=announcements&limit=1&columns=" + ",".join(keys))
    assert r.status_code == 200, r.text
    body = r.json()

    missing = [k for k in keys if k not in body["columns"]]
    assert not missing, f"catalog advertises columns the rows endpoint drops: {missing[:10]}"

    if body["rows"]:
        absent = [k for k in keys if k not in body["rows"][0]]
        assert not absent, f"columns missing from the row dict: {absent[:10]}"


def test_count_reports_rows_actually_returned(client, store):
    """The page renders "{count} of {total}". A hard-coded count is a lie."""
    for i in range(3):
        W.ingest_live(symbol=f"SYM{i}", headline=f"filing number {i}",
                      announced_at=f"2026-07-29 1{i}:00:00", exchange="NSE")

    body = client.get("/api/dataset?source=announcements&limit=2").json()
    assert body["count"] == len(body["rows"])
    assert body["count"] <= 2
    assert body["total"] >= len(body["rows"])


def test_enriched_only_excludes_unpriced_rows(client, store):
    """Rows without prices have no labels. A training export must be able to
    drop them, on BOTH grains — the announcement branch used to ignore this."""
    W.ingest_live(symbol="PRICED", headline="this one has prices",
                  announced_at="2026-07-29 10:00:00", exchange="NSE")
    W.ingest_live(symbol="UNPRICED", headline="this one does not",
                  announced_at="2026-07-29 11:00:00", exchange="NSE")
    W.connect().execute(
        "UPDATE announcements SET price_status='filled', px_t0=100.0 "
        "WHERE symbol='PRICED'")

    body = client.get(
        "/api/dataset?source=announcements&limit=50&enriched_only=true").json()
    symbols = {r["symbol"] for r in body["rows"]}
    assert "PRICED" in symbols
    assert "UNPRICED" not in symbols, "unpriced row survived enriched_only"


@pytest.mark.parametrize("bad", ["action=BUY", "taken=taken", "label=UP"])
def test_signal_only_filters_are_rejected_not_ignored(client, store, bad):
    """Silently ignoring a filter returns UNFILTERED data to someone who thinks
    it is filtered. For a training export that is worse than an error."""
    r = client.get(f"/api/dataset?source=announcements&limit=5&{bad}")
    assert r.status_code == 422, f"{bad} was accepted and ignored"
    assert "signal-dataset filter" in r.text

    r_export = client.get(f"/api/dataset/export?source=announcements&{bad}")
    assert r_export.status_code == 422, f"export accepted and ignored {bad}"
