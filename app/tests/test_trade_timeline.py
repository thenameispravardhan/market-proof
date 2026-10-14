"""GET /api/trades/timeline — the chart's Timeline panel."""
from __future__ import annotations

import pytest

from app.db.models import Trade
from app.tests.factories import seed_signal_chain

pytestmark = pytest.mark.usefixtures("isolated_db")


def test_timeline_chains_filing_to_fill_and_matches_both_symbol_forms(client, db_session):
    chain = seed_signal_chain(db_session, symbol="JSTL", action="BUY", status="executed", with_outcome=False)
    db_session.add_all([
        Trade(signal_id=chain["signal"].id, symbol="JSTL", side="BUY", quantity=25, price=101.5, status="filled"),
        Trade(symbol="BSE:JSTL-X", side="SELL", quantity=25, price=104.2, status="filled", pnl=67.5),
        Trade(symbol="JSTL", side="BUY", quantity=5, price=99.0, status="rejected"),   # not a fill
        Trade(symbol="JSTLX", side="BUY", quantity=1, price=1.0, status="filled"),     # another stock
    ])
    db_session.commit()

    rows = client.get("/api/trades/timeline", params={"symbol": "BSE:JSTL-X"}).json()
    assert len(rows) == 2
    kinds = {len(r["steps"]): [s["kind"] for s in r["steps"]] for r in rows}
    assert kinds[5] == ["filed", "detected", "analysis", "signal", "fill"]   # bot entry
    assert kinds[1] == ["fill"]                                                # exit: fill only
    fills = sorted((r["steps"][-1] for r in rows), key=lambda s: s["side"])
    assert (fills[0]["side"], fills[0]["qty"], fills[0]["price"]) == ("BUY", 25, 101.5)
    assert fills[1]["pnl"] == 67.5
