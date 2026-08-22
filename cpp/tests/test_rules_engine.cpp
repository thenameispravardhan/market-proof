// Expected values recorded from app/analyzer/rules_engine.evaluate() -- see
// the case table in scripts/gen_rules_cases.py.
//
// Invariant I5 (rules are a fresh slate) means the ONLY behaviour worth
// pinning here is the evaluation semantics; there are no built-in rules to
// test, and there must never be.
#include <cassert>
#include <cstdio>
#include <string>
#include <vector>

#include "tb/rules_engine.hpp"

using namespace tb;

static Object analysis() {
  Object a;
  a["event_type"] = "ORDER_WIN";
  a["sentiment"] = "positive";
  a["sentiment_score"] = 70.0;
  a["confidence"] = 0.8;
  a["recommendation"] = "BUY";
  a["deal_value_inr_crore"] = 450.0;
  a["stake_change_pct"] = Value{};  // None
  return a;
}

static Rule rule(int id, Conditions c, std::string action = "BUY", int priority = 100) {
  Rule r;
  r.id = id;
  r.name = "r";
  r.priority = priority;
  r.action = std::move(action);
  r.conditions = std::move(c);
  return r;
}

static Conditions all_of(std::vector<Condition> cs) {
  Conditions c;
  c.all_of = std::move(cs);
  return c;
}
static Conditions any_of(std::vector<Condition> cs) {
  Conditions c;
  c.any_of = std::move(cs);
  return c;
}

static bool matched(const RuleMatch& m) { return m.rule_id.has_value(); }

static void expect_hold(const RuleMatch& m) {
  assert(!m.rule_id.has_value());
  assert(m.action == "HOLD");
  assert(m.action_params.at("reason").str() == "no_matching_rule");
  assert(m.rationale == "No signal rule matched the analysis; defaulting to HOLD.");
}

int main() {
  const Object a = analysis();

  // An empty rule is permissive: no conditions at all -> match.
  {
    auto m = evaluate(a, {rule(1, {})});
    assert(matched(m) && m.action == "BUY");
    assert(m.rationale == "Rule 'r' matched (action=BUY).");
  }

  // -- all_of / any_of ------------------------------------------------------
  // `==` on strings is case-insensitive and strips.
  assert(matched(evaluate(a, {rule(1, all_of({{"event_type", "==", "order_win"}}))})));
  expect_hold(evaluate(a, {rule(1, all_of({{"event_type", "==", "MERGER"}}))}));
  assert(matched(evaluate(
      a, {rule(1, any_of({{"event_type", "==", "MERGER"}, {"confidence", ">=", 0.7}}))})));
  expect_hold(evaluate(a, {rule(1, any_of({{"event_type", "==", "MERGER"}}))}));

  // -- operators ------------------------------------------------------------
  assert(matched(evaluate(
      a, {rule(1, all_of({{"event_type", "in", Array{Value("ORDER_WIN"), Value("MERGER")}}}))})));
  assert(matched(
      evaluate(a, {rule(1, all_of({{"event_type", "not_in", Array{Value("MERGER")}}}))})));
  assert(matched(evaluate(
      a, {rule(1, all_of({{"deal_value_inr_crore", "between", Array{Value(100.0), Value(500.0)}}}))})));
  // Bounds are swapped when given the wrong way round.
  assert(matched(evaluate(
      a, {rule(1, all_of({{"deal_value_inr_crore", "between", Array{Value(500.0), Value(100.0)}}}))})));

  // -- fail-safe: a missing number is never "less than" something -----------
  expect_hold(evaluate(a, {rule(1, all_of({{"stake_change_pct", ">=", 0.0}}))}));
  expect_hold(evaluate(
      a, {rule(1, all_of({{"stake_change_pct", "between", Array{Value(0.0), Value(1.0)}}}))}));
  // An ABSENT field behaves the same as a null one -- non-match, not error.
  expect_hold(evaluate(a, {rule(1, all_of({{"atr_pct", ">=", 1.0}}))}));

  // -- malformed rules are logged and SKIPPED, never fatal ------------------
  expect_hold(evaluate(a, {rule(1, all_of({{"nope", "==", 1.0}}))}));       // bad field
  expect_hold(evaluate(a, {rule(1, all_of({{"confidence", "~=", 1.0}}))}));  // bad op
  expect_hold(evaluate(a, {rule(1, all_of({}))}));                          // present but empty
  {
    // A malformed rule must not stop a later rule from matching.
    auto m = evaluate(a, {rule(1, all_of({{"bad", "==", 1.0}}), "BUY", 1),
                          rule(2, {}, "SELL", 2)});
    assert(m.rule_id == 2 && m.action == "SELL");
  }

  // -- ordering -------------------------------------------------------------
  {
    Rule disabled = rule(1, {});
    disabled.enabled = false;
    expect_hold(evaluate(a, {disabled}));
  }
  {
    // priority ASC wins regardless of input order.
    auto m = evaluate(a, {rule(2, {}, "SELL", 50), rule(1, {}, "HOLD", 10)});
    assert(m.rule_id == 1 && m.action == "HOLD");
  }
  {
    // Ties keep input order (Python's sorted() is stable).
    auto m = evaluate(a, {rule(7, {}, "BUY", 10), rule(8, {}, "SELL", 10)});
    assert(m.rule_id == 7);
  }
  expect_hold(evaluate(a, {}));

  std::puts("test_rules_engine OK");
  return 0;
}
