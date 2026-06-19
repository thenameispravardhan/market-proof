"""Default signal-rule seeding.

Out of the box the `signal_rules` table is empty, so every analysis
falls through to the HOLD fallback and the bot never trades. This
module seeds a small, sensible rule set on the default strategy so a
fresh install does something useful in paper mode immediately. The
operator can edit / disable / reorder these in the Rules UI.

Rule set (priority ASC — first match wins). Longs and shorts are
symmetric: strong positive news goes long, strong negative news goes
short (intraday). The old "block strong negatives" rule is gone — we
now TRADE negatives instead of sitting them out.

  10  High-conviction short   negative + score<=-60 + conf>=0.7    -> SELL (1.5x size)
  20  High-conviction buy     positive + score>=60 + conf>=0.7     -> BUY  (1.5x size)
  30  Momentum buy            BUY rec + score>=40 + conf>=0.6      -> BUY
  35  Momentum short          SELL rec + score<=-40 + conf>=0.6    -> SELL
  40  Big-deal buy            ORDER_WIN/ACQUISITION + deal>=100cr   -> BUY

Shorting is also gated globally by `Settings.SHORTING_ENABLED` (the risk
engine refuses to OPEN a short when it's off), so disabling shorts needs
no rule edits.

Idempotent: rules are keyed by (strategy_id, name). Re-running updates
the existing row's conditions/action in place rather than duplicating.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzer.rules_engine import get_or_create_default_strategy
from app.db.models import BrokerAccount, SignalRule
from app.logging_config import get_logger

log = get_logger(__name__)


# Name of the auto-created paper account so a fresh install can route
# (and fill) trades without the operator configuring a broker first.
DEFAULT_PAPER_ACCOUNT_NAME = "Paper Account"


def seed_default_paper_account(session: Session) -> bool:
    """Ensure a default paper broker account exists and is linked to the
    default strategy, so approved signals can route out of the box.

    Returns True if anything was created/linked. Idempotent.
    """
    changed = False
    acct = session.execute(
        select(BrokerAccount).where(BrokerAccount.name == DEFAULT_PAPER_ACCOUNT_NAME)
    ).scalar_one_or_none()
    if acct is None:
        acct = BrokerAccount(
            name=DEFAULT_PAPER_ACCOUNT_NAME,
            broker="fyers",
            paper_mode=True,
            enabled=True,
        )
        session.add(acct)
        session.flush()
        changed = True
    # Link the default strategy to this account if it isn't already.
    strategy = get_or_create_default_strategy(session)
    cfg = dict(strategy.config or {})
    if cfg.get("broker_account_id") != acct.id:
        cfg["broker_account_id"] = acct.id
        strategy.config = cfg
        changed = True
    session.flush()
    if changed:
        log.info(
            "default_paper_account.seeded",
            account_id=acct.id, strategy_id=strategy.id,
        )
    return changed


# Each entry: (name, priority, conditions, action, action_params)
DEFAULT_RULES: list[tuple[str, int, dict[str, Any], str, dict[str, Any]]] = [
    (
        "High-conviction short",
        10,
        {
            "all_of": [
                {"field": "sentiment", "op": "==", "value": "negative"},
                {"field": "sentiment_score", "op": "<=", "value": -60},
                {"field": "confidence", "op": ">=", "value": 0.7},
            ]
        },
        "SELL",
        {"position_size_pct": 15.0, "size_multiplier": 1.5},
    ),
    (
        "High-conviction buy",
        20,
        {
            "all_of": [
                {"field": "sentiment", "op": "==", "value": "positive"},
                {"field": "sentiment_score", "op": ">=", "value": 60},
                {"field": "confidence", "op": ">=", "value": 0.7},
            ]
        },
        "BUY",
        {"position_size_pct": 15.0, "size_multiplier": 1.5},
    ),
    (
        "Momentum buy",
        30,
        {
            "all_of": [
                {"field": "recommendation", "op": "==", "value": "BUY"},
                {"field": "sentiment_score", "op": ">=", "value": 40},
                {"field": "confidence", "op": ">=", "value": 0.6},
            ]
        },
        "BUY",
        {"position_size_pct": 10.0},
    ),
    (
        "Momentum short",
        35,
        {
            "all_of": [
                {"field": "recommendation", "op": "==", "value": "SELL"},
                {"field": "sentiment_score", "op": "<=", "value": -40},
                {"field": "confidence", "op": ">=", "value": 0.6},
            ]
        },
        "SELL",
        {"position_size_pct": 10.0},
    ),
    (
        "Big-deal buy",
        40,
        {
            "all_of": [
                {
                    "field": "event_type",
                    "op": "in",
                    "value": ["ORDER_WIN", "ACQUISITION"],
                },
                {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                {"field": "confidence", "op": ">=", "value": 0.5},
            ]
        },
        "BUY",
        {"position_size_pct": 12.0},
    ),
]


def seed_default_rules(session: Session) -> int:
    """Seed the default rule set on the default strategy.

    Idempotent: keyed by (strategy_id, name). Returns the number of
    rows inserted or updated. Does NOT commit — the caller owns the
    transaction.
    """
    strategy = get_or_create_default_strategy(session)
    touched = 0
    for name, priority, conditions, action, action_params in DEFAULT_RULES:
        existing = session.execute(
            select(SignalRule).where(
                SignalRule.strategy_id == strategy.id,
                SignalRule.name == name,
            )
        ).scalar_one_or_none()
        if existing is None:
            session.add(
                SignalRule(
                    strategy_id=strategy.id,
                    name=name,
                    priority=priority,
                    conditions=conditions,
                    action=action,
                    action_params=action_params,
                    enabled=True,
                )
            )
            touched += 1
        else:
            # Update in place only if something material changed, so we
            # don't disturb an operator's manual tweaks needlessly.
            changed = (
                existing.priority != priority
                or existing.conditions != conditions
                or existing.action != action
                or existing.action_params != action_params
            )
            if changed:
                existing.priority = priority
                existing.conditions = conditions
                existing.action = action
                existing.action_params = action_params
                touched += 1
    session.flush()
    log.info("default_rules.seeded", count=touched, strategy_id=strategy.id)
    return touched
