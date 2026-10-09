"""`GET /api/orders/pending`: the Trade page's working-orders list."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.api.orders import _expired_day_order
from app.db.models import AuditLog, Trade

IST = timezone(timedelta(hours=5, minutes=30))


def _row(symbol: str, created_ist: datetime) -> Trade:
    return Trade(symbol=symbol, side="BUY", quantity=1, price=1.0, status="placed",
                 created_at=created_ist.astimezone(timezone.utc).replace(tzinfo=None))


def test_day_order_expires_at_the_session_close() -> None:
    placed = datetime(2026, 10, 9, 10, 0, tzinfo=IST)  # Friday morning
    row = _row("NSE:SBIN-EQ", placed)
    assert not _expired_day_order(row, datetime(2026, 10, 9, 15, 29, tzinfo=IST))
    assert _expired_day_order(row, datetime(2026, 10, 9, 15, 31, tzinfo=IST))
    assert _expired_day_order(row, datetime(2026, 10, 12, 9, 20, tzinfo=IST))  # Monday


def test_evening_mcx_order_is_still_live_until_its_own_close() -> None:
    placed = datetime(2026, 10, 9, 18, 0, tzinfo=IST)
    row = _row("MCX:CRUDEOIL26OCTFUT", placed)
    assert not _expired_day_order(row, datetime(2026, 10, 9, 23, 0, tzinfo=IST))
    assert _expired_day_order(row, datetime(2026, 10, 9, 23, 31, tzinfo=IST))


def test_pending_settles_stale_rows_and_sends_utc_times(client: TestClient, db_session, isolated_db) -> None:
    now = datetime.now(timezone.utc)
    fresh = Trade(symbol="NSE:SBIN-EQ", side="BUY", quantity=10, price=600.0, order_type="LIMIT",
                  status="placed", broker_order_id="FRESH", filled_qty=4, created_at=now.replace(tzinfo=None))
    stale = Trade(symbol="NSE:SBIN-EQ", side="BUY", quantity=5, price=590.0, order_type="LIMIT",
                  status="placed", broker_order_id="STALE",
                  created_at=(now - timedelta(days=3)).replace(tzinfo=None))
    db_session.add_all([fresh, stale])
    db_session.commit()

    r = client.get("/api/orders/pending")
    assert r.status_code == 200, r.text
    orders = r.json()["orders"]
    assert [o["broker_order_id"] for o in orders] == ["FRESH"]
    assert orders[0]["filled_qty"] == 4
    assert orders[0]["created_at"].endswith("Z")
    db_session.expire_all()
    assert db_session.query(Trade).filter_by(broker_order_id="STALE").one().status == "cancelled"
    assert db_session.query(AuditLog).filter_by(action="order.expired_eod").count() == 1
