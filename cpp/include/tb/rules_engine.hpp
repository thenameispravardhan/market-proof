// Port of app/analyzer/rules_engine.py (479 lines) -- c++.text §9 PHASE 5.
//
// Pure function over (loaded rules, analysis dict). No DB, no event bus: the
// Python keeps the loader beside the engine and so does this, but only the
// evaluation half lives here, which is the half the parity harness pins.
//
// Invariant I5 (rules are a fresh slate) means there are NO hard-coded
// triggers in this file and there must never be: every rule comes from the
// operator's signal_rules rows.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "tb/value.hpp"

namespace tb {

// Supported operators. `between` takes an inclusive [low, high] pair.
bool is_supported_op(std::string_view op);

// Fields the engine understands. Anything else in a rule is a configuration
// error. The market-context group (sector/price/change_pct/adv_crore) is
// enriched onto the analysis before evaluation; the feed-backed group
// (atr_pct/india_vix/spread_pct) is schema-ready but usually absent, and an
// absent field is a NON-MATCH, not an error -- the same fail-safe convention
// the rest of the engine uses.
bool is_supported_field(std::string_view field);

struct Condition {
  std::string field;
  std::string op;
  Value value;
};

struct Conditions {
  // nullopt = key absent (satisfied); an empty vector = present but empty,
  // which the Python rejects as malformed.
  std::optional<std::vector<Condition>> all_of;
  std::optional<std::vector<Condition>> any_of;
  // Set when the JSON could not be shaped into the above (e.g. `all_of` was
  // not a list). Evaluating such a rule raises RuleError in the Python, which
  // logs and skips the rule.
  std::string parse_error;
};

struct Rule {
  std::optional<int> id;
  std::string name;
  int priority{100};
  bool enabled{true};
  std::string action{"HOLD"};
  Object action_params;
  Conditions conditions;
};

struct RuleMatch {
  std::optional<int> rule_id;  // nullopt for the default HOLD
  std::string action;          // BUY | SELL | HOLD | BLOCK
  Object action_params;
  std::string rationale;
};

// First matching rule wins, priority ASC (stable for ties). A malformed rule
// is logged and skipped, never fatal. No match -> HOLD with reason
// "no_matching_rule".
RuleMatch evaluate(const Object& analysis, std::vector<Rule> rules);

}  // namespace tb
