"""Record Python's answer for every case pinned in cpp/tests/test_rules_engine.cpp.

Same contract as gen_fast_track_cases.py: the C++ asserts against the PYTHON's
behaviour, so the expected values are printed by running the Python, not read
off it (§10.1 -- the Python is the reference).

    PYTHONPATH=. TESTING=1 python scripts/gen_rules_cases.py
"""
from __future__ import annotations

import sys

from app.analyzer.rules_engine import evaluate

ANALYSIS = {
    "event_type": "ORDER_WIN",
    "sentiment": "positive",
    "sentiment_score": 70.0,
    "confidence": 0.8,
    "recommendation": "BUY",
    "deal_value_inr_crore": 450.0,
    "stake_change_pct": None,
    "dividend_per_share": None,
    "buyback_value_inr_crore": None,
}


def rule(**kw):
    d = {"id": 1, "name": "r", "priority": 100, "enabled": True,
         "action": "BUY", "action_params": {}, "conditions": {}}
    d.update(kw)
    return d


CASES: dict[str, list[dict]] = {
    "empty conditions match": [rule(conditions={})],
    "all_of pass": [rule(conditions={"all_of": [{"field": "event_type", "op": "==", "value": "order_win"}]})],
    "all_of fail": [rule(conditions={"all_of": [{"field": "event_type", "op": "==", "value": "MERGER"}]})],
    "any_of one hit": [rule(conditions={"any_of": [
        {"field": "event_type", "op": "==", "value": "MERGER"},
        {"field": "confidence", "op": ">=", "value": 0.7}]})],
    "any_of none": [rule(conditions={"any_of": [{"field": "event_type", "op": "==", "value": "MERGER"}]})],
    "in list": [rule(conditions={"all_of": [{"field": "event_type", "op": "in", "value": ["ORDER_WIN", "MERGER"]}]})],
    "not_in": [rule(conditions={"all_of": [{"field": "event_type", "op": "not_in", "value": ["MERGER"]}]})],
    "between": [rule(conditions={"all_of": [{"field": "deal_value_inr_crore", "op": "between", "value": [100, 500]}]})],
    "between reversed": [rule(conditions={"all_of": [{"field": "deal_value_inr_crore", "op": "between", "value": [500, 100]}]})],
    "None never orders": [rule(conditions={"all_of": [{"field": "stake_change_pct", "op": ">=", "value": 0}]})],
    "None between": [rule(conditions={"all_of": [{"field": "stake_change_pct", "op": "between", "value": [0, 1]}]})],
    "absent field": [rule(conditions={"all_of": [{"field": "atr_pct", "op": ">=", "value": 1}]})],
    "unsupported field": [rule(conditions={"all_of": [{"field": "nope", "op": "==", "value": 1}]})],
    "unsupported op": [rule(conditions={"all_of": [{"field": "confidence", "op": "~=", "value": 1}]})],
    "empty all_of": [rule(conditions={"all_of": []})],
    "disabled skipped": [rule(enabled=False)],
    "priority order": [rule(id=2, priority=50, action="SELL"), rule(id=1, priority=10, action="HOLD")],
    "tie stable": [rule(id=7, priority=10, action="BUY"), rule(id=8, priority=10, action="SELL")],
    "no rules": [],
    "malformed skipped then match": [
        rule(id=1, priority=1, conditions={"all_of": [{"field": "bad", "op": "==", "value": 1}]}),
        rule(id=2, priority=2, action="SELL")],
}


def main() -> int:
    for name, rules in CASES.items():
        m = evaluate(ANALYSIS, rules)
        print(f"{name:<32} rule_id={m.rule_id} action={m.action} "
              f"params={m.action_params} rationale={m.rationale!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
