"""Edge Memory — the self-learning conviction engine.

Covers the direction-adjusted expectancy math, the tiered symbol/action
fallback, the fail-open verdict, and the risk-engine gate (opt-in,
default off).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.db.models import SignalOutcome
from app.services import historical_edge as he

pytestmark = pytest.mark.usefixtures("isolated_db")


def _mk(db, symbol, action, move_30m, move_5m=None):
    db.add(
        SignalOutcome(
            symbol=symbol, action=action, signal_status="pending",
            price_at_signal=100.0,
            price_30m=100.0 * (1 + move_30m / 100.0),
            move_30m_pct=move_30m,
            move_5m_pct=move_5m if move_5m is not None else move_30m / 2.0,
            price_5m=100.0,
            created_at=datetime.now(timezone.utc),
        )
    )


# ---- direction adjustment ---------------------------------------------


def test_buy_expectancy_uses_raw_move(db_session):
    for m in (1.0, 2.0, 3.0, -1.0, 0.0):  # avg raw = +1.0
        _mk(db_session, "AAA", "BUY", m)
    db_session.commit()
    edge = he.compute_edge(db_session, symbol="AAA", action="BUY", min_symbol=5)
    assert edge is not None
    assert edge.basis == "symbol"
    assert edge.n == 5
    assert edge.expectancy == pytest.approx(1.0)
    # 3 of 5 favorable (1,2,3 > 0; -1,0 not) -> win_rate 0.6
    assert edge.win_rate == pytest.approx(0.6)


def test_sell_expectancy_flips_sign(db_session):
    # For a SELL, a price DROP is favorable. Raw moves all negative ->
    # favorable (positive expectancy).
    for m in (-1.0, -2.0, -3.0, -2.0, -2.0):  # avg raw = -2.0
        _mk(db_session, "BBB", "SELL", m)
    db_session.commit()
    edge = he.compute_edge(db_session, symbol="BBB", action="SELL", min_symbol=5)
    assert edge is not None
    assert edge.expectancy == pytest.approx(2.0)   # flipped to +2.0
    assert edge.win_rate == pytest.approx(1.0)      # every drop is a win for a short


# ---- tiered fallback ---------------------------------------------------


def test_falls_back_to_action_tier_when_symbol_thin(db_session):
    # Only 2 samples for CCC (below min_symbol=5), but 25 BUY overall.
    _mk(db_session, "CCC", "BUY", 5.0)
    _mk(db_session, "CCC", "BUY", 5.0)
    for _ in range(25):
        _mk(db_session, "OTHER", "BUY", 1.0)
    db_session.commit()
    edge = he.compute_edge(
        db_session, symbol="CCC", action="BUY", min_symbol=5, min_action=20
    )
    assert edge is not None
    assert edge.basis == "action"         # fell back
    assert edge.symbol is None
    assert edge.expectancy == pytest.approx(1.0, abs=0.5)


def test_none_when_insufficient_everywhere(db_session):
    _mk(db_session, "DDD", "BUY", 2.0)
    db_session.commit()
    assert he.compute_edge(db_session, symbol="DDD", action="BUY") is None


def test_hold_action_has_no_edge(db_session):
    for _ in range(30):
        _mk(db_session, "EEE", "HOLD", 1.0)
    db_session.commit()
    assert he.compute_edge(db_session, symbol="EEE", action="HOLD") is None


# ---- verdict (fail-open) ----------------------------------------------


def test_verdict_insufficient_never_blocks():
    v, _ = he.edge_verdict(None, min_n=30, min_expectancy_pct=0.0)
    assert v == "insufficient"


def test_verdict_blocks_proven_loser():
    edge = he.EdgeStats(
        basis="action", symbol=None, action="BUY", n=100,
        win_rate=0.3, avg_move_5m=-0.5, expectancy=-1.2, best=2.0, worst=-8.0,
    )
    v, reason = he.edge_verdict(edge, min_n=30, min_expectancy_pct=0.0)
    assert v == "block"
    assert "-1.2" in reason


def test_verdict_allows_winner():
    edge = he.EdgeStats(
        basis="symbol", symbol="AAA", action="BUY", n=40,
        win_rate=0.65, avg_move_5m=0.8, expectancy=1.5, best=6.0, worst=-3.0,
    )
    v, _ = he.edge_verdict(edge, min_n=30, min_expectancy_pct=0.0)
    assert v == "allow"


def test_verdict_thin_evidence_allows_even_if_negative():
    # Negative expectancy but only 10 samples < min_n 30 -> insufficient.
    edge = he.EdgeStats(
        basis="symbol", symbol="AAA", action="BUY", n=10,
        win_rate=0.2, avg_move_5m=-1.0, expectancy=-2.0, best=0.0, worst=-5.0,
    )
    v, _ = he.edge_verdict(edge, min_n=30, min_expectancy_pct=0.0)
    assert v == "insufficient"


# ---- book expectancy ---------------------------------------------------


def test_book_expectancy_by_action(db_session):
    for _ in range(25):
        _mk(db_session, "AAA", "BUY", 1.0)
    db_session.commit()
    book = he.book_expectancy(db_session)
    assert book["BUY"] is not None
    assert book["BUY"]["expectancy"] == pytest.approx(1.0)
    assert book["SELL"] is None
