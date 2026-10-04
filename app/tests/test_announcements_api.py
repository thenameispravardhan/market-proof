"""GET /api/announcements/recent — the optional `symbol` filter the Trade
page's chart (event marks) and symbol-details headlines use."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.models import Announcement


def _add(db, symbol: str, headline: str, n: int) -> None:
    db.add(Announcement(symbol=symbol, exchange="NSE", event_type="ANNOUNCEMENT", headline=headline, content_hash=f"ann-api-{symbol}-{n}"))


def test_recent_announcements_filters_by_symbol(client: TestClient, db_session, isolated_db) -> None:
    _add(db_session, "RELIANCE", "Board meeting - dividend", 1)
    _add(db_session, "TCS", "Financial results", 2)
    _add(db_session, "Reliance", "Outcome of board meeting", 3)
    db_session.commit()

    rows = client.get("/api/announcements/recent", params={"symbol": "reliance", "limit": 50}).json()
    assert {r["headline"] for r in rows} == {"Board meeting - dividend", "Outcome of board meeting"}

    everything = client.get("/api/announcements/recent", params={"limit": 50}).json()
    assert len(everything) == 3
