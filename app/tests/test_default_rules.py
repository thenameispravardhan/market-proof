"""Tests for the default signal-rule seeding.

The default rules must (a) seed idempotently, (b) actually fire the
rules engine for representative analyses, so a fresh install trades in
paper mode instead of HOLDing everything.
"""
from __future__ import annotations

from app.analyzer.default_rules import DEFAULT_RULES, seed_default_rules
from app.analyzer.rules_engine import (
    evaluate,
    get_or_create_default_strategy,
    load_rules_for_strategy,
)
from app.db.models import SignalRule


def test_seed_is_idempotent(db_session):
    n1 = seed_default_rules(db_session)
    db_session.commit()
    assert n1 == len(DEFAULT_RULES)

    # Second run touches nothing (no material change).
    n2 = seed_default_rules(db_session)
    db_session.commit()
    assert n2 == 0

    strategy = get_or_create_default_strategy(db_session)
    rows = (
        db_session.query(SignalRule)
        .filter(SignalRule.strategy_id == strategy.id)
        .all()
    )
    assert len(rows) == len(DEFAULT_RULES)


def test_high_conviction_buy_matches(db_session):
    seed_default_rules(db_session)
    db_session.commit()
    strategy = get_or_create_default_strategy(db_session)
    rules = load_rules_for_strategy(db_session, strategy.id)

    analysis = {
        "event_type": "ORDER_WIN",
        "sentiment": "positive",
        "sentiment_score": 80,
        "confidence": 0.85,
        "recommendation": "BUY",
    }
    match = evaluate(analysis, rules)
    assert match.action == "BUY"
    assert match.rule_id is not None


def test_strong_negative_is_blocked(db_session):
    seed_default_rules(db_session)
    db_session.commit()
    strategy = get_or_create_default_strategy(db_session)
    rules = load_rules_for_strategy(db_session, strategy.id)

    analysis = {
        "event_type": "OTHER",
        "sentiment": "negative",
        "sentiment_score": -75,
        "confidence": 0.9,
        "recommendation": "SELL",
    }
    match = evaluate(analysis, rules)
    assert match.action == "BLOCK"


def test_neutral_news_falls_through_to_hold(db_session):
    seed_default_rules(db_session)
    db_session.commit()
    strategy = get_or_create_default_strategy(db_session)
    rules = load_rules_for_strategy(db_session, strategy.id)

    analysis = {
        "event_type": "OTHER",
        "sentiment": "neutral",
        "sentiment_score": 5,
        "confidence": 0.5,
        "recommendation": "HOLD",
    }
    match = evaluate(analysis, rules)
    assert match.action == "HOLD"
    assert match.rule_id is None
