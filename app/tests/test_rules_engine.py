"""Signal-rules engine tests.

Coverage:
  - Each operator works (==, !=, in, not_in, >=, <=, >, <).
  - `all_of` (AND) composition.
  - `any_of` (OR) composition.
  - `all_of` + `any_of` combined.
  - Priority ordering: lower number wins, first match wins.
  - Disabled rules are skipped.
  - No matching rule -> HOLD fallback with rule_id=None.
  - Malformed conditions raise RuleError (we tolerate + skip, not crash).
  - End-to-end via DB: SignalRule rows in priority order.
  - signal_rules.matched event fires on match.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.analyzer.rules_engine import (
    CHANNEL_RULE_MATCHED,
    DEFAULT_STRATEGY_NAME,
    RuleError,
    RuleMatch,
    evaluate,
    get_or_create_default_strategy,
    load_rules_for_strategy,
    publish_match,
)
from app.db.models import SignalRule, Strategy
from app.services.event_bus import event_bus


# -- Helpers -------------------------------------------------------------


def _rule(
    *,
    id=None,
    name="r",
    priority=100,
    enabled=True,
    conditions=None,
    action="BUY",
    action_params=None,
):
    """Build a plain-dict rule (the engine's pure-function path)."""
    return {
        "id": id,
        "name": name,
        "priority": priority,
        "enabled": enabled,
        "conditions": conditions if conditions is not None else {},
        "action": action,
        "action_params": action_params or {},
    }


def _base_analysis(**overrides) -> dict:
    a = {
        "event_type": "ORDER_WIN",
        "sentiment": "positive",
        "sentiment_score": 60.0,
        "confidence": 0.8,
        "recommendation": "BUY",
        "deal_value_inr_crore": 100.0,
        "stake_change_pct": None,
        "dividend_per_share": None,
        "buyback_value_inr_crore": None,
    }
    a.update(overrides)
    return a


# -- Operator matrix -----------------------------------------------------


def test_op_eq_string_case_insensitive():
    m = evaluate(
        _base_analysis(event_type="ORDER_WIN"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "==", "value": "order_win"}]})],
    )
    assert m.action == "BUY"


def test_op_neq_string():
    m = evaluate(
        _base_analysis(event_type="OTHER"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "!=", "value": "ORDER_WIN"}]})],
    )
    assert m.action == "BUY"


def test_op_in_list():
    m = evaluate(
        _base_analysis(event_type="ACQUISITION"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "in", "value": ["ORDER_WIN", "ACQUISITION"]}]})],
    )
    assert m.action == "BUY"


def test_op_in_list_misses():
    m = evaluate(
        _base_analysis(event_type="DIVIDEND"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "in", "value": ["ORDER_WIN", "ACQUISITION"]}]})],
    )
    assert m.action == "HOLD"
    assert m.rule_id is None


def test_op_not_in():
    m = evaluate(
        _base_analysis(event_type="OTHER"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "not_in", "value": ["ORDER_WIN", "ACQUISITION"]}]})],
    )
    assert m.action == "BUY"


def test_op_gte():
    m = evaluate(
        _base_analysis(sentiment_score=50.0),
        [_rule(conditions={"all_of": [{"field": "sentiment_score", "op": ">=", "value": 50}]})],
    )
    assert m.action == "BUY"


def test_op_gte_misses_at_49():
    m = evaluate(
        _base_analysis(sentiment_score=49.0),
        [_rule(conditions={"all_of": [{"field": "sentiment_score", "op": ">=", "value": 50}]})],
    )
    assert m.action == "HOLD"


def test_op_lte():
    m = evaluate(
        _base_analysis(sentiment_score=-30.0),
        [_rule(conditions={"all_of": [{"field": "sentiment_score", "op": "<=", "value": -10}]})],
    )
    assert m.action == "BUY"


def test_op_gt_lt():
    m = evaluate(
        _base_analysis(sentiment_score=10.0),
        [_rule(conditions={"all_of": [{"field": "sentiment_score", "op": ">", "value": 5}]})],
    )
    assert m.action == "BUY"
    m2 = evaluate(
        _base_analysis(sentiment_score=5.0),
        [_rule(conditions={"all_of": [{"field": "sentiment_score", "op": "<", "value": 5}]})],
    )
    assert m2.action == "HOLD"


# -- Composition ---------------------------------------------------------


def test_all_of_requires_all():
    m = evaluate(
        _base_analysis(sentiment_score=60.0, confidence=0.8),
        [
            _rule(
                conditions={
                    "all_of": [
                        {"field": "event_type", "op": "in", "value": ["ORDER_WIN", "ACQUISITION"]},
                        {"field": "sentiment_score", "op": ">=", "value": 50},
                        {"field": "confidence", "op": ">=", "value": 0.7},
                        {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                    ]
                }
            )
        ],
    )
    assert m.action == "BUY"


def test_all_of_short_circuits_on_first_miss():
    """If any `all_of` entry fails, the rule fails — even if the rest
    would pass. We assert this by making the first condition fail."""
    m = evaluate(
        _base_analysis(sentiment_score=60.0, confidence=0.8),
        [
            _rule(
                conditions={
                    "all_of": [
                        {"field": "event_type", "op": "==", "value": "DIVIDEND"},  # fail
                        {"field": "sentiment_score", "op": ">=", "value": 50},    # pass
                        {"field": "confidence", "op": ">=", "value": 0.7},         # pass
                    ]
                }
            )
        ],
    )
    assert m.action == "HOLD"


def test_any_of_one_passes():
    m = evaluate(
        _base_analysis(deal_value_inr_crore=500.0, confidence=0.5),
        [
            _rule(
                conditions={
                    "any_of": [
                        {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                        {"field": "confidence", "op": ">=", "value": 0.99},
                    ]
                }
            )
        ],
    )
    assert m.action == "BUY"


def test_any_of_none_passes():
    m = evaluate(
        _base_analysis(deal_value_inr_crore=10.0, confidence=0.5),
        [
            _rule(
                conditions={
                    "any_of": [
                        {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                        {"field": "confidence", "op": ">=", "value": 0.99},
                    ]
                }
            )
        ],
    )
    assert m.action == "HOLD"


def test_all_of_and_any_of_combined():
    """Rule matches iff all_of is satisfied AND any_of has ≥1 hit."""
    # all_of passes (sentiment_score + confidence). any_of passes
    # (deal_value >= 100). => match.
    m = evaluate(
        _base_analysis(sentiment_score=70.0, confidence=0.9, deal_value_inr_crore=200.0),
        [
            _rule(
                action="SELL",
                conditions={
                    "all_of": [
                        {"field": "sentiment_score", "op": ">=", "value": 50},
                        {"field": "confidence", "op": ">=", "value": 0.7},
                    ],
                    "any_of": [
                        {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                        {"field": "recommendation", "op": "==", "value": "SELL"},
                    ],
                },
            )
        ],
    )
    assert m.action == "SELL"


def test_all_of_passes_any_of_fails_blocks_match():
    m = evaluate(
        _base_analysis(sentiment_score=70.0, confidence=0.9, deal_value_inr_crore=10.0),
        [
            _rule(
                conditions={
                    "all_of": [
                        {"field": "sentiment_score", "op": ">=", "value": 50},
                        {"field": "confidence", "op": ">=", "value": 0.7},
                    ],
                    "any_of": [
                        {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
                        {"field": "recommendation", "op": "==", "value": "SELL"},
                    ],
                }
            )
        ],
    )
    assert m.action == "HOLD"


# -- Priority + first-match-wins ---------------------------------------


def test_priority_lower_wins():
    """Two rules that both match: the lower-priority (lower number)
    wins."""
    m = evaluate(
        _base_analysis(deal_value_inr_crore=200.0),
        [
            _rule(name="high-pri", priority=10, action="BUY",
                  conditions={"all_of": [{"field": "deal_value_inr_crore", "op": ">=", "value": 100}]}),
            _rule(name="low-pri", priority=20, action="SELL",
                  conditions={"all_of": [{"field": "deal_value_inr_crore", "op": ">=", "value": 100}]}),
        ],
    )
    assert m.action == "BUY"
    assert m.rationale.startswith("Rule 'high-pri'")


def test_disabled_rule_is_skipped():
    """A disabled rule never wins, even if it has the lowest priority."""
    m = evaluate(
        _base_analysis(deal_value_inr_crore=200.0),
        [
            _rule(name="disabled", priority=1, enabled=False, action="BUY",
                  conditions={"all_of": [{"field": "deal_value_inr_crore", "op": ">=", "value": 100}]}),
            _rule(name="enabled", priority=10, action="SELL",
                  conditions={"all_of": [{"field": "deal_value_inr_crore", "op": ">=", "value": 100}]}),
        ],
    )
    assert m.action == "SELL"


def test_no_match_returns_hold_with_none_rule_id():
    m = evaluate(
        _base_analysis(event_type="OTHER"),
        [_rule(conditions={"all_of": [{"field": "event_type", "op": "==", "value": "ORDER_WIN"}]})],
    )
    assert m.action == "HOLD"
    assert m.rule_id is None
    assert m.action_params.get("reason") == "no_matching_rule"


# -- `between` operator + market-context fields ---------------------------


def test_op_between_matches_inside_range():
    m = evaluate(
        _base_analysis(india_vix=15.0),
        [_rule(conditions={"all_of": [{"field": "india_vix", "op": "between", "value": [10, 20]}]})],
    )
    assert m.action == "BUY"


def test_op_between_misses_outside_range():
    m = evaluate(
        _base_analysis(india_vix=25.0),
        [_rule(conditions={"all_of": [{"field": "india_vix", "op": "between", "value": [10, 20]}]})],
    )
    assert m.action == "HOLD"


def test_op_between_missing_field_is_fail_safe():
    # india_vix absent (feed not wired) → the condition is a non-match,
    # not a crash, not a phantom pass.
    m = evaluate(
        _base_analysis(),
        [_rule(conditions={"all_of": [{"field": "india_vix", "op": "between", "value": [10, 20]}]})],
    )
    assert m.action == "HOLD"


def test_op_between_bad_bounds_skips_rule():
    # A malformed `between` (one bound) raises RuleError internally, which
    # the engine catches → the rule is skipped, falling through to HOLD.
    m = evaluate(
        _base_analysis(india_vix=15.0),
        [_rule(conditions={"all_of": [{"field": "india_vix", "op": "between", "value": [10]}]})],
    )
    assert m.action == "HOLD"


def test_new_field_sector_in_list():
    m = evaluate(
        _base_analysis(sector="BANK"),
        [_rule(conditions={"all_of": [{"field": "sector", "op": "in", "value": ["BANK", "NBFC"]}]})],
    )
    assert m.action == "BUY"


def test_unsupported_field_still_raises():
    with pytest.raises(RuleError):
        from app.analyzer.rules_engine import _match_condition
        _match_condition({"field": "not_a_field", "op": "==", "value": 1}, _base_analysis())


def test_enrich_analysis_context_adds_sector_and_price():
    from types import SimpleNamespace
    from app.analyzer.rules_engine import enrich_analysis_context

    quote = SimpleNamespace(
        last_price=2500.0, change_pct=3.2, average_daily_volume_crore=50.0
    )
    enriched = enrich_analysis_context(
        {"symbol": "RELIANCE", "event_type": "ORDER_WIN"},
        quote=quote,
        sector_map={"RELIANCE": "ENERGY"},
    )
    assert enriched["sector"] == "ENERGY"
    assert enriched["price"] == 2500.0
    assert enriched["change_pct"] == 3.2
    assert enriched["adv_crore"] == 50.0
    # Now a rule can gate on the enriched fields.
    m = evaluate(
        enriched,
        [_rule(conditions={"all_of": [{"field": "change_pct", "op": "between", "value": [1, 5]}]})],
    )
    assert m.action == "BUY"


def test_enrich_does_not_overwrite_existing_keys():
    from app.analyzer.rules_engine import enrich_analysis_context

    enriched = enrich_analysis_context(
        {"symbol": "RELIANCE", "sector": "CUSTOM"},
        sector_map={"RELIANCE": "ENERGY"},
    )
    assert enriched["sector"] == "CUSTOM"


def test_empty_rules_list_returns_hold():
    m = evaluate(_base_analysis(), [])
    assert m.action == "HOLD"
    assert m.rule_id is None


# -- action_params propagation ------------------------------------------


def test_action_params_propagated():
    m = evaluate(
        _base_analysis(),
        [_rule(
            action="BUY",
            action_params={"position_size_pct": 1.5, "override": "x"},
            conditions={},  # no conditions = permissive
        )],
    )
    assert m.action == "BUY"
    assert m.action_params == {"position_size_pct": 1.5, "override": "x"}


# -- Malformed rules ---------------------------------------------------


def test_malformed_field_logs_and_skips(caplog):
    """A rule with an unknown field is skipped, not crash."""
    m = evaluate(
        _base_analysis(),
        [
            _rule(name="bogus", conditions={"all_of": [{"field": "made_up_field", "op": "==", "value": 1}]}),
            _rule(name="good", action="SELL", conditions={}),  # permissive
        ],
    )
    # The bogus rule is skipped; the permissive rule matches.
    assert m.action == "SELL"
    assert m.rationale.startswith("Rule 'good'")


def test_malformed_op_skipped():
    m = evaluate(
        _base_analysis(),
        [
            _rule(name="bogus", conditions={"all_of": [{"field": "event_type", "op": "~~", "value": "x"}]}),
            _rule(name="good", action="SELL", conditions={}),  # permissive
        ],
    )
    assert m.action == "SELL"


def test_all_of_must_be_list():
    m = evaluate(
        _base_analysis(),
        [
            _rule(conditions={"all_of": "not a list"}),
            _rule(name="fallback", action="SELL", conditions={}),  # permissive
        ],
    )
    assert m.action == "SELL"


def test_empty_all_of_is_malformed():
    """An empty `all_of` list is itself a malformed condition per
    spec (all_of must be non-empty if present)."""
    m = evaluate(
        _base_analysis(),
        [
            _rule(conditions={"all_of": []}),
            _rule(name="fallback", action="SELL", conditions={}),
        ],
    )
    assert m.action == "SELL"


# -- DB integration ---------------------------------------------------


def test_load_rules_for_strategy_orders_by_priority(db_session):
    s = Strategy(name="Test")
    db_session.add(s)
    db_session.flush()
    # Insert out-of-order; load should sort by priority ASC.
    db_session.add(SignalRule(strategy_id=s.id, name="b", priority=20, conditions={}, action="BUY"))
    db_session.add(SignalRule(strategy_id=s.id, name="a", priority=10, conditions={}, action="SELL"))
    db_session.add(SignalRule(strategy_id=s.id, name="disabled", priority=5, enabled=False, conditions={}, action="BLOCK"))
    db_session.commit()

    rules = load_rules_for_strategy(db_session, s.id)
    assert [r.name for r in rules] == ["a", "b"]  # disabled is filtered out


def test_get_or_create_default_strategy_idempotent(db_session):
    s1 = get_or_create_default_strategy(db_session)
    db_session.commit()
    s2 = get_or_create_default_strategy(db_session)
    assert s1.id == s2.id
    assert s1.name == DEFAULT_STRATEGY_NAME


def test_evaluate_end_to_end_with_db_rules(db_session):
    """Build SignalRule rows directly; run evaluate() with the rule
    objects — no service plumbing required."""
    s = Strategy(name="e2e")
    db_session.add(s)
    db_session.flush()
    db_session.add(SignalRule(
        strategy_id=s.id, name="big-acq",
        priority=10,
        conditions={
            "all_of": [
                {"field": "event_type", "op": "in", "value": ["ORDER_WIN", "ACQUISITION"]},
                {"field": "sentiment_score", "op": ">=", "value": 50},
                {"field": "confidence", "op": ">=", "value": 0.7},
                {"field": "deal_value_inr_crore", "op": ">=", "value": 100},
            ]
        },
        action="BUY",
    ))
    db_session.commit()

    rules = load_rules_for_strategy(db_session, s.id)
    m = evaluate(_base_analysis(), rules)
    assert m.action == "BUY"
    assert m.rule_id is not None


# -- Event bus ----------------------------------------------------------


def test_publish_match_emits_event():
    sub = event_bus.subscribe(CHANNEL_RULE_MATCHED)
    try:
        m = RuleMatch(rule_id=42, action="BUY", rationale="test")
        # publish_match is async; run it via asyncio.run.
        delivered = asyncio.run(publish_match(m, analysis={"event_type": "ORDER_WIN", "symbol": "RELIANCE"}))
        assert delivered >= 1
        evt = sub.get_nowait()
        assert evt.payload["rule_id"] == 42
        assert evt.payload["action"] == "BUY"
    finally:
        event_bus.unsubscribe(CHANNEL_RULE_MATCHED, sub)
