"""Tests for the equity ledger + position sizing helpers."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import get_settings
from app.db.models import Position as PositionRow, RiskState, Trade as TradeRow
from app.risk import position_sizer as ps


def test_override_short_circuits_equity(db_session, isolated_db):
    assert ps.compute_equity(db_session, None, override=2_500_000.0) == 2_500_000.0


def test_equity_is_capital_plus_realised_plus_unrealised(db_session, isolated_db):
    settings = get_settings()
    start = settings.PORTFOLIO_VALUE
    # A closed (realised) winner of +5000 and an open position 5000 in profit.
    db_session.add(TradeRow(
        symbol="TCS", side="SELL", quantity=10, price=3600.0,
        order_type="market", status="filled",
        executed_at=datetime.now(timezone.utc), pnl=5000.0,
    ))
    db_session.add(PositionRow(
        symbol="INFY", quantity=100, average_price=1500.0, last_price=1550.0,
    ))  # unrealised = (1550-1500)*100 = 5000
    db_session.commit()
    equity = ps.compute_equity(db_session, None)
    assert equity == pytest.approx(start + 5000.0 + 5000.0)


def test_live_anchor_replaces_capital_and_realised(db_session, isolated_db):
    """With a live Fyers funds anchor, equity = broker balance +
    unrealised only. Realised P&L must NOT be double-counted — the
    broker balance already includes it."""
    db_session.add(TradeRow(
        symbol="TCS", side="SELL", quantity=10, price=3600.0,
        order_type="market", status="filled",
        executed_at=datetime.now(timezone.utc), pnl=5000.0,
    ))
    db_session.add(PositionRow(
        symbol="INFY", quantity=100, average_price=1500.0, last_price=1550.0,
    ))  # unrealised = +5000
    db_session.commit()
    equity = ps.compute_equity(db_session, None, live_anchor=200_000.0)
    assert equity == pytest.approx(200_000.0 + 5000.0)


def test_live_anchor_nonpositive_falls_back_to_ledger(db_session, isolated_db):
    """A junk (<= 0) anchor must be ignored — fail-safe to the
    PORTFOLIO_VALUE ledger, never sizing off a broken funds call."""
    settings = get_settings()
    assert ps.compute_equity(db_session, None, live_anchor=0.0) == pytest.approx(
        float(settings.PORTFOLIO_VALUE)
    )


@pytest.mark.asyncio
async def test_funds_provider_ttl_and_fail_safe():
    """FyersFundsProvider caches within the TTL and keeps the last
    good value when the fetch starts failing."""
    calls = {"n": 0}
    value = {"v": 150_000.0}

    async def fetch():
        calls["n"] += 1
        if value["v"] is None:
            raise RuntimeError("boom")
        return value["v"]

    p = ps.FyersFundsProvider(fetch, ttl_s=60.0)
    assert p.equity() is None
    await p.refresh()
    assert p.equity() == 150_000.0
    assert calls["n"] == 1
    # Within the TTL: cached, no second call.
    await p.refresh()
    assert calls["n"] == 1
    # Fetch failing later must not clear the last good value.
    p._ts = 0.0  # force TTL expiry
    value["v"] = None
    await p.refresh()
    assert p.equity() == 150_000.0


def test_equity_floors_at_positive(db_session, isolated_db):
    # A realised loss bigger than starting capital must not yield <= 0.
    db_session.add(TradeRow(
        symbol="X", side="SELL", quantity=1, price=1.0, order_type="market",
        status="filled", executed_at=datetime.now(timezone.utc),
        pnl=-99_999_999.0,
    ))
    db_session.commit()
    assert ps.compute_equity(db_session, None) > 0


def test_notional_cap_qty():
    # 20% of 1M = 200k; at 2500/share => 80 shares.
    assert ps.notional_cap_qty(entry=2500.0, equity=1_000_000.0, max_single_position_pct=20.0) == 80
    # Non-positive inputs => 0.
    assert ps.notional_cap_qty(entry=0.0, equity=1_000_000.0, max_single_position_pct=20.0) == 0
    assert ps.notional_cap_qty(entry=100.0, equity=0.0, max_single_position_pct=20.0) == 0


def test_resolve_risk_pct_ramps_for_young_account(db_session, isolated_db):
    # No filled trades yet => held down to the ramp start pct.
    settings = get_settings()
    got = ps.resolve_risk_pct(db_session, cap_pct=settings.MAX_CAPITAL_RISK_PCT, ramp=True)
    assert got == pytest.approx(settings.RISK_RAMP_START_PCT)


def test_resolve_risk_pct_full_after_ramp(db_session, isolated_db):
    settings = get_settings()
    # Seed enough filled trades to clear the ramp window.
    for _ in range(settings.RISK_RAMP_TRADES):
        db_session.add(TradeRow(
            symbol="Z", side="BUY", quantity=1, price=1.0, order_type="market",
            status="filled", executed_at=datetime.now(timezone.utc),
        ))
    db_session.commit()
    got = ps.resolve_risk_pct(db_session, cap_pct=settings.MAX_CAPITAL_RISK_PCT, ramp=True)
    assert got == pytest.approx(settings.MAX_CAPITAL_RISK_PCT)


def test_resolve_risk_pct_riskstate_throttle_wins(db_session, isolated_db):
    db_session.add(RiskState(id=1, current_risk_pct=0.25))
    db_session.commit()
    # Even with ramp disabled (full cap), the throttle pulls it down.
    got = ps.resolve_risk_pct(db_session, cap_pct=0.75, ramp=False)
    assert got == pytest.approx(0.25)


def test_resolve_risk_pct_no_ramp_uses_cap(db_session, isolated_db):
    got = ps.resolve_risk_pct(db_session, cap_pct=0.75, ramp=False)
    assert got == pytest.approx(0.75)
