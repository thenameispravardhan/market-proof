"""GET /api/dashboard/summary — the Dashboard's top stat row."""

from __future__ import annotations

from datetime import datetime

from app.api import core
from app.db.models import Position as PositionRow, Trade as TradeRow


def test_summary_counts_only_open_positions_and_uses_the_ist_day(
    client, db_session, monkeypatch, isolated_db
):
    # 02:00 IST on 2030-01-02 is still 2030-01-01 in UTC.
    monkeypatch.setattr(core, "_utcnow", lambda: datetime(2030, 1, 1, 20, 30))
    before = client.get("/api/dashboard/summary").json()

    db_session.add_all([
        PositionRow(symbol="DSOPEN", quantity=5, average_price=100.0, last_price=110.0, unrealized_pnl=50.0),
        # Closed positions stay in the table with quantity 0.
        PositionRow(symbol="DSSHUT", quantity=0, average_price=100.0, last_price=90.0, unrealized_pnl=-999.0),
        # 00:30 IST on 2030-01-02: today.
        TradeRow(symbol="DSOPEN", side="SELL", quantity=1, price=1.0, status="filled",
                 pnl=30.0, executed_at=datetime(2030, 1, 1, 19, 0)),
        # 23:30 IST on 2030-01-01: yesterday.
        TradeRow(symbol="DSOPEN", side="SELL", quantity=1, price=1.0, status="filled",
                 pnl=700.0, executed_at=datetime(2030, 1, 1, 18, 0)),
    ])
    db_session.commit()

    after = client.get("/api/dashboard/summary").json()
    assert "hard_rules_count" not in after
    assert after["open_positions"] - before["open_positions"] == 1
    assert round(after["todays_unrealized_pnl"] - before["todays_unrealized_pnl"], 2) == 50.0
    assert round(after["todays_realized_pnl"] - before["todays_realized_pnl"], 2) == 30.0
