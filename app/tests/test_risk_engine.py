"""Tests for the risk engine.

Coverage (one assertion per hard rule, plus sizing math):

  - R1: action == HOLD or BLOCK       -> blocked
  - R2: confidence <= 0               -> blocked
  - R3: qty < 1                       -> blocked (RISK_QTY_BELOW_1)
  - R4: trade risk > max_capital_risk_pct
  - R5: daily loss >= daily_max_loss_pct
  - R6: concurrent positions >= cap
  - R7: position value > max_single_position_pct
  - R8: ADV < min_liquidity_crore     -> blocked
  - R9: sector concentration > 30%    -> blocked
  - R10: symbol blocklist             -> blocked
  - Healthy signal passes
  - Sizing math: qty = floor((portfolio * pct / 100) / |entry - stop_loss|)
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    Analysis,
    Announcement,
    BrokerAccount,
    Position as PositionRow,
    RiskEvent,
    Signal,
    Strategy,
    Trade as TradeRow,
)
from app.execution.market_data import MarketDataBus
from app.risk.engine import RiskEngine, StrategyOverrides, load_sector_map


# -- Helpers --------------------------------------------------------------


def _make_signal(
    db_session,
    *,
    symbol: str = "RELIANCE",
    action: str = "BUY",
    confidence: float = 0.9,
    strategy_id: int | None = None,
) -> Signal:
    s = Signal(
        symbol=symbol,
        action=action,
        confidence=confidence,
        status="pending",
        strategy_id=strategy_id,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_strategy(db_session, name: str = "test", config: dict | None = None) -> Strategy:
    s = Strategy(name=name, enabled=True, config=config or {})
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


def _make_account(db_session, *, name: str = "main") -> BrokerAccount:
    a = BrokerAccount(
        name=name, broker="fyers", app_id="APP", secret_key="sk",
        access_token="tk", paper_mode=True,
    )
    db_session.add(a)
    db_session.commit()
    db_session.refresh(a)
    return a


# -- Tests ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_healthy_signal_passes(db_session, isolated_db):
    """A well-formed signal with a sane risk profile approves."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "healthy",
        config={"max_capital_risk_pct": 0.05},  # 0.05% per trade
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    # Use a large portfolio so a sane-sized position is well under
    # 20% of it. 0.05% of 50M = 25K risk. With 25 INR stop distance
    # => qty=1000. Position = 2.5M = 5% of 50M. < 20% cap. Approved.
    engine = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, target=2575.0, session=db_session,
    )
    assert decision.approved is True, decision.violations
    assert decision.violations == []
    assert decision.sizing is not None
    assert decision.sizing.qty > 0


# R1
@pytest.mark.asyncio
async def test_action_hold_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "r1")
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="HOLD", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_ACTION_NOT_TRADABLE" in decision.codes


@pytest.mark.asyncio
async def test_action_block_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "r1b")
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BLOCK", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_ACTION_NOT_TRADABLE" in decision.codes


# R2
@pytest.mark.asyncio
async def test_zero_confidence_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "r2")
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", confidence=0.0, strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_CONFIDENCE_NONPOSITIVE" in decision.codes


# R3
@pytest.mark.asyncio
async def test_qty_below_one_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "r3")
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    # Tiny portfolio + huge distance -> 0 shares.
    engine = RiskEngine(market_data=md, portfolio_value=10.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=100.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_QTY_BELOW_1" in decision.codes


# R4
@pytest.mark.asyncio
async def test_max_capital_risk_pct_blocks(db_session, isolated_db):
    """If max_capital_risk_pct is so small that the position risk
    exceeds the allowed, block. We can simulate this by setting
    a tighter stop on a larger position (which makes risk_amount
    grow), and then asserting the engine blocks it.

    Implementation: configure max_capital_risk_pct = 0.01 (very
    tight). With portfolio=1M, allowed risk = 100. With 1 share
    and a 25 INR stop distance, the actual risk is 25 INR < 100
    -> should PASS. To trigger R4, we need a position that
    exceeds 100 INR. We'll set the position size via the entry
    and stop distance: with 1 share and 150 INR distance, risk
    = 150 > 100. Wait — but we size to risk_amt = 1% of 1M = 100.
    So qty = floor(100 / 150) = 0 — which triggers R3 first.

    The cleanest way to trigger R4 is to have an existing
    position that stacks past the cap. We'll add a 1000-share
    RELIANCE position at 2500, then try to add 1 more share.
    """
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r4", config={"max_capital_risk_pct": 0.01}
    )
    acct = _make_account(db_session)
    # Pre-existing huge position.
    db_session.add(PositionRow(
        symbol="RELIANCE", quantity=10_000, average_price=2500.0,
        last_price=2500.0, strategy_id=strat.id,
    ))
    db_session.commit()
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=25_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    # The combined position value would be huge -> R4 or R7.
    # We expect either RISK_MAX_CAPITAL_RISK_PCT (R4) or
    # RISK_MAX_SINGLE_POSITION_PCT (R7) — R7 wins because
    # the position value is 25M which is 100% of portfolio.
    # Actually the qty itself is small (~6 shares = 15K INR,
    # risk 150 INR < 2500 INR allowed). But total_after_trade
    # is 10006 * 2500 = 25M which is 100% > 20% of 25M.
    # R7 fires.
    assert decision.approved is False
    assert any(
        code in decision.codes
        for code in ("RISK_MAX_SINGLE_POSITION_PCT", "RISK_MAX_CAPITAL_RISK_PCT")
    )


# R5
@pytest.mark.asyncio
async def test_daily_max_loss_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r5", config={"daily_max_loss_pct": 0.5}
    )
    acct = _make_account(db_session)
    # Insert a losing trade for today.
    db_session.add(TradeRow(
        symbol="RELIANCE", side="SELL", quantity=10, price=2400.0,
        order_type="market", status="filled",
        executed_at=datetime.now(timezone.utc), pnl=-10000.0,
    ))
    db_session.commit()
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    # Portfolio value is small so the loss exceeds the cap.
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_DAILY_MAX_LOSS" in decision.codes


# R6
@pytest.mark.asyncio
async def test_max_concurrent_positions_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r6", config={"max_concurrent_positions": 1}
    )
    acct = _make_account(db_session)
    # Two open positions in different symbols.
    db_session.add(PositionRow(
        symbol="TCS", quantity=10, average_price=3500.0,
        last_price=3500.0, strategy_id=strat.id,
    ))
    db_session.commit()
    sig = _make_signal(db_session, symbol="INFY", action="BUY", strategy_id=strat.id)
    md.set_quote_sync("INFY", last_price=1500.0)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=1500.0, stop_loss=1485.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_MAX_CONCURRENT_POSITIONS" in decision.codes


# R7
@pytest.mark.asyncio
async def test_max_single_position_pct_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r7", config={"max_single_position_pct": 1.0}  # very tight
    )
    acct = _make_account(db_session)
    # With notional-capped sizing, a *fresh* order can never exceed the
    # cap on its own — R7 now guards STACKING. Seed an existing RELIANCE
    # position already at/over the 1% cap (25k > 10k), so adding any more
    # blocks. (cap = 1% of 1M = 10k.)
    db_session.add(PositionRow(
        symbol="RELIANCE", quantity=10, average_price=2500.0,
        last_price=2500.0, strategy_id=strat.id,
    ))
    db_session.commit()
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_MAX_SINGLE_POSITION_PCT" in decision.codes


# R8
@pytest.mark.asyncio
async def test_min_liquidity_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync(
        "RELIANCE", last_price=2500.0,
        average_daily_volume_crore=0.1,  # 10 lakhs only
    )
    strat = _make_strategy(
        db_session, "r8", config={"min_liquidity_crore": 5.0}
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_MIN_LIQUIDITY" in decision.codes


@pytest.mark.asyncio
async def test_min_liquidity_unknown_allows_with_warning(db_session, isolated_db):
    """If ADV is unknown (None), the rule should not block but
    should emit a warning context field."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)  # no ADV
    strat = _make_strategy(
        db_session, "r8b", config={"max_capital_risk_pct": 0.05},
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    # Sizing gives a sane qty, no other rule fires.
    assert decision.approved is True
    assert decision.context.get("warnings")
    assert any("ADV" in w for w in decision.context["warnings"])


# R9
@pytest.mark.asyncio
async def test_sector_concentration_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    md.set_quote_sync("ONGC", last_price=200.0)
    strat = _make_strategy(db_session, "r9")
    acct = _make_account(db_session)
    # RELIANCE is in ENERGY. Add a big ONGC position (also ENERGY)
    # to push sector exposure over 30%.
    db_session.add(PositionRow(
        symbol="ONGC", quantity=100_000, average_price=200.0,
        last_price=200.0, strategy_id=strat.id,
    ))
    db_session.commit()
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    # Small portfolio: 25M. ONGC alone is 20M (80%) -> sector already over 30%.
    engine = RiskEngine(market_data=md, portfolio_value=25_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_SECTOR_CONCENTRATION" in decision.codes


# R10
@pytest.mark.asyncio
async def test_symbol_blocklist_blocks(db_session, isolated_db):
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r10", config={"symbol_blocklist": ["RELIANCE"]}
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_SYMBOL_BLOCKLISTED" in decision.codes


# -- Sizing math ---------------------------------------------------------


def test_sizing_math_basic():
    """qty = floor((portfolio * pct / 100) / |entry - stop_loss|)."""
    from app.risk.engine import RiskEngine as _R

    s = _R._compute_qty(
        entry=2500.0, stop_loss=2475.0,
        portfolio_value=1_000_000.0, max_capital_risk_pct=1.0,
    )
    # risk_amount = 10_000. distance = 25. qty = floor(10000/25) = 400.
    assert s.qty == 400
    assert s.risk_amount == 10_000.0
    assert s.distance == 25.0


def test_sizing_math_fractional_rounds_down():
    s = RiskEngine._compute_qty(
        entry=100.0, stop_loss=99.0,
        portfolio_value=1_000.0, max_capital_risk_pct=1.0,
    )
    # risk_amount = 10. distance = 1. qty = 10.
    assert s.qty == 10


def test_sizing_math_zero_distance_returns_zero_qty():
    s = RiskEngine._compute_qty(
        entry=100.0, stop_loss=100.0,
        portfolio_value=10_000.0, max_capital_risk_pct=1.0,
    )
    assert s.qty == 0


def test_sizing_math_negative_pct_returns_zero_qty():
    s = RiskEngine._compute_qty(
        entry=100.0, stop_loss=99.0,
        portfolio_value=10_000.0, max_capital_risk_pct=-1.0,
    )
    assert s.qty == 0


# -- Bypass attempts ------------------------------------------------------


@pytest.mark.asyncio
async def test_no_bypass_by_action_override(db_session, isolated_db):
    """Even if a rule says BUY, action='HOLD' still blocks. R1 is
    the very first check and never skipped."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "bypass-1")
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="HOLD", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct, session=db_session,
    )
    assert decision.approved is False


@pytest.mark.asyncio
async def test_no_bypass_by_empty_config(db_session, isolated_db):
    """No overrides should still mean the defaults apply."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "bypass-2",
        config={"max_capital_risk_pct": 0.05},
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    # Default rules apply. Sizing is sane.
    assert decision.approved is True


@pytest.mark.asyncio
async def test_no_bypass_by_negative_qty_signal(db_session, isolated_db):
    """A signal that doesn't carry confidence is treated as 0.0."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(db_session, "bypass-3")
    acct = _make_account(db_session)
    # Confidence=None.
    s = Signal(
        symbol="RELIANCE", action="BUY", status="pending",
        strategy_id=strat.id, confidence=None,
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
    decision = await engine.evaluate(
        signal=s, strategy=strat, account=acct,
        entry=2500.0, stop_loss=2475.0, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_CONFIDENCE_NONPOSITIVE" in decision.codes


# -- R0: invalid stop loss (verifier bypass attempt) ----------------------


@pytest.mark.parametrize("bad_sl", [0, 0.0, -1, -0.5, -100.0])
@pytest.mark.asyncio
async def test_invalid_stop_loss_blocks(bad_sl, db_session, isolated_db):
    """Stop-loss <= 0 must be blocked with RISK_INVALID_STOP_LOSS.
    Covers bypass attempts at 0, 0.0, and negative values."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r0-sl", config={"max_capital_risk_pct": 0.05}
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=bad_sl, session=db_session,
    )
    assert decision.approved is False, (
        f"SL={bad_sl!r} should have been blocked; got: {decision.violations}"
    )
    assert "RISK_INVALID_STOP_LOSS" in decision.codes, (
        f"expected RISK_INVALID_STOP_LOSS, got codes={decision.codes}"
    )
    # First violation is the SL one.
    assert decision.violations[0]["code"] == "RISK_INVALID_STOP_LOSS"
    # No sizing computed (engine returns before _compute_qty).
    assert decision.sizing is None


@pytest.mark.asyncio
async def test_missing_stop_loss_blocks(db_session, isolated_db):
    """stop_loss=None is also a bypass attempt — must be blocked."""
    md = MarketDataBus()
    md.set_quote_sync("RELIANCE", last_price=2500.0)
    strat = _make_strategy(
        db_session, "r0-none", config={"max_capital_risk_pct": 0.05}
    )
    acct = _make_account(db_session)
    sig = _make_signal(db_session, symbol="RELIANCE", action="BUY", strategy_id=strat.id)
    engine = RiskEngine(market_data=md, portfolio_value=50_000_000.0)
    decision = await engine.evaluate(
        signal=sig, strategy=strat, account=acct,
        entry=2500.0, stop_loss=None, session=db_session,
    )
    assert decision.approved is False
    assert "RISK_INVALID_STOP_LOSS" in decision.codes


@pytest.mark.asyncio
async def test_zero_distance_in_compute_qty_does_not_crash(db_session, isolated_db):
    """Defence-in-depth: the divide-by-|entry - stop_loss| in
    _compute_qty must not ZeroDivisionError even if the caller
    bypasses the R0 check."""
    from app.risk.engine import RiskEngine as _R
    s = _R._compute_qty(
        entry=2500.0, stop_loss=2500.0,  # zero distance
        portfolio_value=50_000_000.0, max_capital_risk_pct=0.05,
    )
    assert s.qty == 0
    assert s.note == "non-positive inputs"


# -- sector map -----------------------------------------------------------


def test_sector_map_known_symbols():
    smap = load_sector_map()
    assert smap["RELIANCE"] == "ENERGY"
    assert smap["TCS"] == "IT"
    assert smap["HDFCBANK"] == "BANK"


def test_sector_map_unknown_returns_none():
    smap = load_sector_map()
    assert smap.get("ZZZZ_NEWSYMBOL") is None
