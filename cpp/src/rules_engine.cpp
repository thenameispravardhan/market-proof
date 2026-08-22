#include "tb/rules_engine.hpp"

#include <spdlog/spdlog.h>

#include <algorithm>
#include <array>
#include <format>
#include <stdexcept>

#include "tb/str.hpp"

namespace tb {
namespace {

constexpr std::array<std::string_view, 9> kOps{"==", "!=", "in", "not_in", ">=",
                                               "<=", ">",  "<",  "between"};

constexpr std::array<std::string_view, 16> kFields{
    "event_type",     "sentiment",   "sentiment_score",        "confidence",
    "recommendation", "deal_value_inr_crore", "stake_change_pct", "dividend_per_share",
    "buyback_value_inr_crore",
    // market-context: `sector` is enriched live; price/change/adv only when a
    // quote was supplied to enrich_analysis_context.
    "sector", "price", "change_pct", "adv_crore",
    // market-context (feed-backed; inert until their feeds are wired)
    "atr_pct", "india_vix", "spread_pct"};

// Mirrors rules_engine.RuleError. Caught per-rule by evaluate(), exactly like
// the Python's try/except around _match_conditions.
struct RuleError : std::runtime_error {
  using std::runtime_error::runtime_error;
};

// Python's `actual.strip().lower() == value.strip().lower()` for two strings,
// plain equality otherwise. Bools compare numerically because Python's
// `True == 1` is true and rules do gate on numeric-ish fields.
bool eq(const Value& actual, const Value& value) {
  if (actual.is_str() && value.is_str())
    return lower(strip(actual.str())) == lower(strip(value.str()));
  if (actual.is_null() || value.is_null()) return actual.is_null() && value.is_null();
  if (actual.is_str() != value.is_str()) return false;
  if (actual.is_arr() || value.is_arr()) return false;
  const auto a = actual.as_double();
  const auto b = value.as_double();
  return a && b && *a == *b;
}

bool in_(const Value& actual, const Array& choices) {
  if (actual.is_str()) {
    const std::string a = lower(strip(actual.str()));
    return std::any_of(choices.begin(), choices.end(), [&](const Value& c) {
      return (c.is_str() && lower(strip(c.str())) == a) || eq(actual, c);
    });
  }
  return std::any_of(choices.begin(), choices.end(),
                     [&](const Value& c) { return eq(actual, c); });
}

double require_number(const Value& v, std::string_view op) {
  const auto d = v.as_double();
  if (!d) throw RuleError(std::format("non-numeric value for op '{}'", op));
  return *d;
}

bool apply_op(std::string_view op, const Value& actual, const Value& value) {
  if (op == "==") return eq(actual, value);
  if (op == "!=") return !eq(actual, value);
  if (op == "in" || op == "not_in") {
    if (!value.is_arr()) throw RuleError(std::format("`{}` requires value to be a list", op));
    const bool hit = in_(actual, value.arr());
    return op == "in" ? hit : !hit;
  }
  if (op == "between") {
    if (!value.is_arr()) throw RuleError("`between` requires value to be a [low, high] list");
    const Array& b = value.arr();
    if (b.size() != 2) throw RuleError("`between` requires exactly two bounds [low, high]");
    if (actual.is_null()) return false;  // missing value never matches (fail-safe)
    const double a = require_number(actual, op);
    double lo = require_number(b[0], op);
    double hi = require_number(b[1], op);
    if (lo > hi) std::swap(lo, hi);
    return lo <= a && a <= hi;
  }
  // Numeric ordering. A missing number is not "less than" something.
  if (actual.is_null()) return false;
  const double a = require_number(actual, op);
  const double v = require_number(value, op);
  if (op == ">=") return a >= v;
  if (op == "<=") return a <= v;
  if (op == ">") return a > v;
  if (op == "<") return a < v;
  throw RuleError(std::format("unsupported op: '{}'", op));
}

bool match_condition(const Condition& c, const Object& analysis) {
  if (c.field.empty()) throw RuleError("condition.field must be a non-empty string");
  if (!is_supported_field(c.field)) throw RuleError(std::format("unsupported field: '{}'", c.field));
  if (!is_supported_op(c.op)) throw RuleError(std::format("unsupported op: '{}'", c.op));
  const Value* actual = find(analysis, c.field);
  static const Value kMissing{};
  return apply_op(c.op, actual != nullptr ? *actual : kMissing, c.value);
}

// A rule matches iff BOTH groups are satisfied; a missing group is satisfied,
// but a group that is present and empty is malformed.
bool match_conditions(const Conditions& conds, const Object& analysis) {
  if (!conds.parse_error.empty()) throw RuleError(conds.parse_error);

  if (conds.all_of) {
    if (conds.all_of->empty()) throw RuleError("`all_of` must be a non-empty list of conditions");
    for (const Condition& c : *conds.all_of)
      if (!match_condition(c, analysis)) return false;
  }
  if (conds.any_of) {
    if (conds.any_of->empty()) throw RuleError("`any_of` must be a non-empty list of conditions");
    const bool hit = std::any_of(conds.any_of->begin(), conds.any_of->end(),
                                 [&](const Condition& c) { return match_condition(c, analysis); });
    if (!hit) return false;
  }
  // No conditions at all -> match (an empty rule is permissive).
  return true;
}

}  // namespace

bool is_supported_op(std::string_view op) {
  return std::find(kOps.begin(), kOps.end(), op) != kOps.end();
}

bool is_supported_field(std::string_view field) {
  return std::find(kFields.begin(), kFields.end(), field) != kFields.end();
}

RuleMatch evaluate(const Object& analysis, std::vector<Rule> rules) {
  // Python's sorted() is stable, so ties keep input order.
  std::stable_sort(rules.begin(), rules.end(),
                   [](const Rule& a, const Rule& b) { return a.priority < b.priority; });

  for (const Rule& r : rules) {
    if (!r.enabled) continue;
    bool ok = false;
    try {
      ok = match_conditions(r.conditions, analysis);
    } catch (const RuleError& e) {
      spdlog::warn(R"({{"event":"rules_engine.rule_malformed","rule_id":{},"error":"{}"}})",
                   r.id ? std::to_string(*r.id) : "null", e.what());
      continue;
    }
    if (ok)
      return RuleMatch{r.id, r.action, r.action_params,
                       std::format("Rule '{}' matched (action={}).", r.name, r.action)};
  }

  RuleMatch hold;
  hold.action = "HOLD";
  hold.action_params["reason"] = std::string("no_matching_rule");
  hold.rationale = "No signal rule matched the analysis; defaulting to HOLD.";
  return hold;
}

}  // namespace tb
