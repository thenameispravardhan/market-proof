"""Tests for the portfolio circuit breakers + kill switch."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db.models import RiskState, Trade as TradeRow
from app.risk import circuit_breakers as cb


def _filled(session, pnl=None, when=None, status="filled"):
    session.add(TradeRow(
        symbol="X", side="BUY", quantity=1, price=100.0, order_type="market",
        status=status, pnl=pnl,
        executed_at=when or datetime.now(timezone.utc),
    ))
    session.commit()


def test_daily_loss_trips_and_flattens(db_session, isolated_db):
    # First call anchors day-start equity at the starting capital.
    first = cb.evaluate_breakers(db_session, equity=1_000_000.0)
    assert first["trading_disabled"] is False
    # Equity drops 3% (> 2.5% cap) -> halt + flatten.
    out = cb.evaluate_breakers(db_session, equity=970_000.0)
    assert out["trading_disabled"] is True
    assert out["flatten_required"] is True
    assert "daily_loss" in out["newly_tripped"]
    # And entries are now blocked.
    allowed, reason = cb.entry_allowed(db_session, confidence=0.9)
    assert allowed is False
    assert reason == cb.REASON_DAILY


def test_daily_loss_clears_next_day(db_session, isolated_db):
    day1 = datetime(2026, 6, 19, 6, 0, tzinfo=timezone.utc)   # IST ~11:30
    day2 = datetime(2026, 6, 22, 6, 0, tzinfo=timezone.utc)   # next Monday
    cb.evaluate_breakers(db_session, equity=1_000_000.0, now=day1)
    # -4%: trips the daily cap (>2.5%) but NOT weekly (5%) or monthly (8%).
    out = cb.evaluate_breakers(db_session, equity=960_000.0, now=day1)
    assert out["trading_disabled"] is True
    assert out["disabled_reason"] == cb.REASON_DAILY
    # New day re-anchors and clears the daily halt.
    out2 = cb.evaluate_breakers(db_session, equity=960_000.0, now=day2)
    assert out2["trading_disabled"] is False


def test_weekly_loss_throttles_risk(db_session, isolated_db):
    settings = get_settings()
    cb.evaluate_breakers(db_session, equity=1_000_000.0)         # week peak
    out = cb.evaluate_breakers(db_session, equity=940_000.0)     # -6% > 5%
    assert "weekly_loss" in out["newly_tripped"]
    state = db_session.get(RiskState, 1)
    assert state.current_risk_pct == pytest.approx(settings.WEEKLY_BREACH_RISK_PCT)


def test_consecutive_losers_cooldown(db_session, isolated_db):
    settings = get_settings()
    for _ in range(settings.MAX_CONSECUTIVE_LOSERS):
        _filled(db_session, pnl=-100.0)
    out = cb.evaluate_breakers(db_session, equity=1_000_000.0)
    assert "consecutive_losers" in out["newly_tripped"]
    # Cooldown blocks a normal-confidence entry...
    allowed, reason = cb.entry_allowed(db_session, confidence=0.7)
    assert allowed is False and reason == "consecutive_loser_cooldown"
    # ...but a high-conviction signal trades through it.
    allowed2, _ = cb.entry_allowed(
        db_session, confidence=settings.CONSECUTIVE_LOSER_RESUME_CONFIDENCE
    )
    assert allowed2 is True


def test_consecutive_streak_resets_on_a_win(db_session, isolated_db):
    _filled(db_session, pnl=-100.0)
    _filled(db_session, pnl=-100.0)
    _filled(db_session, pnl=50.0)   # most recent is a winner
    assert cb.trailing_consecutive_losers(db_session) == 0


def test_max_trades_per_day_blocks(db_session, isolated_db):
    settings = get_settings()
    # Entry fills = filled trades with NULL pnl.
    for _ in range(settings.MAX_TRADES_PER_DAY):
        _filled(db_session, pnl=None)
    allowed, reason = cb.entry_allowed(db_session, confidence=0.9)
    assert allowed is False and reason == "max_trades_per_day"


def test_manual_kill_and_resume(db_session, isolated_db):
    cb.trip_manual_kill(db_session)
    allowed, reason = cb.entry_allowed(db_session, confidence=0.99)
    assert allowed is False and reason == cb.REASON_MANUAL
    cb.clear_halt(db_session)
    allowed2, _ = cb.entry_allowed(db_session, confidence=0.5)
    assert allowed2 is True


def test_monthly_drawdown_trips(db_session, isolated_db):
    cb.evaluate_breakers(db_session, equity=1_000_000.0)
    out = cb.evaluate_breakers(db_session, equity=910_000.0)  # -9% > 8%
    assert out["trading_disabled"] is True
    assert "monthly_drawdown" in out["newly_tripped"]
    state = db_session.get(RiskState, 1)
    assert state.disabled_reason == cb.REASON_MONTHLY
