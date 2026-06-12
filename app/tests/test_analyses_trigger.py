"""Tests for the operator analysis-trigger endpoints.

POST /api/analyses/run/{id} and POST /api/analyses/backfill publish
`announcements.new` onto the in-process event bus — the running
analyzer consumes them exactly like a fresh scrape. Here we assert
the publish itself (the analyzer's consumption is covered by
test_service.py).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.models import Analysis, Announcement
from app.monitors.base import CHANNEL_NEW
from app.services.event_bus import event_bus


def _mk_announcement(session, symbol="TESTCO", headline="Order win worth Rs 100 crore"):
    a = Announcement(
        symbol=symbol,
        exchange="NSE",
        event_type="ANNOUNCEMENT",
        headline=headline,
        pdf_url="https://example.com/test.pdf",
        source="test",
        filed_at=datetime.now(timezone.utc).replace(tzinfo=None),
        content_hash=f"hash-{symbol}-{headline[:20]}",
    )
    session.add(a)
    session.commit()
    session.refresh(a)
    return a


def test_run_unknown_announcement_404(client):
    r = client.post("/api/analyses/run/999999")
    assert r.status_code == 404


def test_run_queues_event_with_monitor_payload_shape(client, db_session, isolated_db):
    a = _mk_announcement(db_session)
    sub = event_bus.subscribe(CHANNEL_NEW)
    try:
        r = client.post(f"/api/analyses/run/{a.id}")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued"
        assert body["announcement_id"] == a.id
        assert body["already_analyzed"] is False

        # publish() uses put_nowait, so the event is already in the
        # queue by the time the request returns.
        event = sub.get_nowait()
        assert event.channel == CHANNEL_NEW
        assert event.payload["announcement_id"] == a.id
        assert event.payload["company"] == "TESTCO"
        assert event.payload["pdf_url"] == "https://example.com/test.pdf"
    finally:
        event_bus.unsubscribe(CHANNEL_NEW, sub)


def test_run_reports_already_analyzed(client, db_session, isolated_db):
    a = _mk_announcement(db_session, symbol="DONECO")
    db_session.add(
        Analysis(
            announcement_id=a.id,
            model="test",
            sentiment="neutral",
            sentiment_score=0.0,
            confidence=0.5,
            recommendation="HOLD",
            rationale="prior",
        )
    )
    db_session.commit()
    r = client.post(f"/api/analyses/run/{a.id}")
    assert r.status_code == 200
    assert r.json()["already_analyzed"] is True


def test_backfill_queues_only_unanalyzed(client, db_session, isolated_db):
    fresh = _mk_announcement(db_session, symbol="FRESH1")
    done = _mk_announcement(db_session, symbol="DONE1")
    db_session.add(
        Analysis(
            announcement_id=done.id,
            model="test",
            sentiment="neutral",
            sentiment_score=0.0,
            confidence=0.5,
            recommendation="HOLD",
            rationale="prior",
        )
    )
    db_session.commit()

    r = client.post("/api/analyses/backfill?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert fresh.id in body["announcement_ids"]
    assert done.id not in body["announcement_ids"]


def test_backfill_limit_validation(client):
    assert client.post("/api/analyses/backfill?limit=0").status_code == 422
    assert client.post("/api/analyses/backfill?limit=51").status_code == 422
