"""Negative news -> short signals, and the SHORTING_ENABLED guard."""
from __future__ import annotations

import pytest

from app.analyzer.default_rules import seed_default_rules
from app.analyzer.rules_engine import (
    evaluate,
    get_or_create_default_strategy,
    load_rules_for_strategy,
)
from app.config import get_settings
from app.db.models import BrokerAccount, Position as PositionRow, Signal, Strategy
from app.execution.market_data import MarketDataBus
from app.risk.engine import RiskEngine


def _seed_and_load(session):
    strat = get_or_create_default_strategy(session)
    seed_default_rules(session)
    session.commit()
    return load_rules_for_strategy(session, strat.id)


def test_strong_negative_news_shorts(db_session, isolated_db):
    rules = _seed_and_load(db_session)
    analysis = {
        "event_type": "OTHER", "sentiment": "negative",
        "sentiment_score": -72, "confidence": 0.82, "recommendation": "SELL",
    }
    match = evaluate(analysis, rules)
    assert match.action == "SELL"


def test_moderate_negative_news_shorts(db_session, isolated_db):
    rules = _seed_and_load(db_session)
    analysis = {
        "event_type": "OTHER", "sentiment": "negative",
        "sentiment_score": -45, "confidence": 0.65, "recommendation": "SELL",
    }
    match = evaluate(analysis, rules)
    assert match.action == "SELL"


def test_strong_positive_still_buys(db_session, isolated_db):
    rules = _seed_and_load(db_session)
    analysis = {
        "event_type": "OTHER", "sentiment": "positive",
        "sentiment_score": 70, "confidence": 0.8, "recommendation": "BUY",
    }
    match = evaluate(analysis, rules)
    assert match.action == "BUY"


def test_no_block_rule_for_negatives_anymore(db_session, isolated_db):
    rules = _seed_and_load(db_session)
    assert not any(getattr(r, "action", "") == "BLOCK" for r in rules)


@pytest.mark.asyncio
async def test_shorting_disabled_blocks_opening_short(db_session, isolated_db, monkeypatch):
    monkeypatch.setenv("SHORTING_ENABLED", "0")
    get_settings.cache_clear()
    try:
        md = MarketDataBus()
        md.set_quote_sync("RELIANCE", last_price=2500.0)
        strat = Strategy(name="noshort", enabled=True, config={})
        db_session.add(strat)
        acct = BrokerAccount(name="a", broker="fyers", paper_mode=True)
        db_session.add(acct)
        db_session.commit()
        sig = Signal(symbol="RELIANCE", action="SELL", confidence=0.9,
                     status="pending", strategy_id=strat.id)
        db_session.add(sig)
        db_session.commit()
        db_session.refresh(sig)
        engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
        decision = await engine.evaluate(
            signal=sig, strategy=strat, account=acct,
            entry=2500.0, stop_loss=2575.0, session=db_session,
        )
        assert decision.approved is False
        assert "RISK_SHORTING_DISABLED" in decision.codes
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_shorting_disabled_allows_closing_long(db_session, isolated_db, monkeypatch):
    monkeypatch.setenv("SHORTING_ENABLED", "0")
    get_settings.cache_clear()
    try:
        md = MarketDataBus()
        md.set_quote_sync("RELIANCE", last_price=2500.0)
        strat = Strategy(name="noshort2", enabled=True, config={})
        db_session.add(strat)
        acct = BrokerAccount(name="b", broker="fyers", paper_mode=True)
        # Existing long — a SELL here just closes it, so it's allowed.
        db_session.add(PositionRow(symbol="RELIANCE", quantity=50,
                                   average_price=2500.0, last_price=2500.0))
        db_session.add(acct)
        db_session.commit()
        sig = Signal(symbol="RELIANCE", action="SELL", confidence=0.9,
                     status="pending", strategy_id=strat.id)
        db_session.add(sig)
        db_session.commit()
        db_session.refresh(sig)
        engine = RiskEngine(market_data=md, portfolio_value=1_000_000.0)
        decision = await engine.evaluate(
            signal=sig, strategy=strat, account=acct,
            entry=2500.0, stop_loss=2575.0, session=db_session,
        )
        assert "RISK_SHORTING_DISABLED" not in decision.codes
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
