#include "tb/schemas.hpp"

#include <algorithm>

#include "tb/str.hpp"

namespace tb {
namespace {

bool is_event_type(std::string_view s) {
  return std::find(kEventTypes.begin(), kEventTypes.end(), s) != kEventTypes.end();
}

bool is_hold_synonym(std::string_view s) {
  return std::find(kHoldSynonyms.begin(), kHoldSynonyms.end(), s) != kHoldSynonyms.end();
}

// Mirrors pydantic's `Field(..., min_length=1)` + the `_non_blank` validator:
// strip, then reject an empty result.
Result<std::string> non_blank(const Object& o, const char* key) {
  const Value* v = find(o, key);
  if (v == nullptr || !v->is_str())
    return unexpected(ParseError{std::string(key) + ": field required"});
  const std::string_view s = strip(v->str());
  if (s.empty())
    return unexpected(ParseError{std::string(key) + ": must be a non-empty string"});
  return std::string(s);
}

std::optional<double> opt_num(const Object& o, const char* key) {
  const Value* v = find(o, key);
  if (v == nullptr || v->is_null()) return std::nullopt;
  return v->as_double();
}

}  // namespace

std::string normalise_event_type(std::string_view raw) {
  const std::string up = upper(strip(raw));
  return is_event_type(up) ? up : std::string("OTHER");
}

std::optional<std::string> normalise_recommendation(std::string_view raw) {
  const std::string up = upper(strip(raw));
  if (is_hold_synonym(up)) return std::string("HOLD");
  if (up == "BUY" || up == "SELL" || up == "HOLD") return up;
  return std::nullopt;
}

std::optional<std::string> normalise_sentiment(std::string_view raw) {
  const std::string lo = lower(strip(raw));
  if (lo == "positive" || lo == "neutral" || lo == "negative") return lo;
  return std::nullopt;
}

double normalise_sentiment_score(double raw) {
  if (raw != 0.0 && raw >= -1.0 && raw <= 1.0) return raw * 100.0;
  return raw;
}

Result<AnalysisResponse> validate_analysis(const Object& raw) {
  AnalysisResponse a;

  const Value* et = find(raw, "event_type");
  if (et == nullptr) return unexpected(ParseError{"event_type: field required"});
  // A non-string event_type reaches pydantic's enum check unchanged and fails.
  if (!et->is_str()) return unexpected(ParseError{"event_type: not a valid enumeration member"});
  a.event_type = normalise_event_type(et->str());

  auto summary = non_blank(raw, "summary");
  if (!summary) return unexpected(summary.error());
  a.summary = *summary;

  auto reasoning = non_blank(raw, "reasoning");
  if (!reasoning) return unexpected(reasoning.error());
  a.reasoning = *reasoning;

  const Value* sent = find(raw, "sentiment");
  if (sent == nullptr || !sent->is_str())
    return unexpected(ParseError{"sentiment: field required"});
  auto sentiment = normalise_sentiment(sent->str());
  if (!sentiment)
    return unexpected(ParseError{"sentiment: not a valid enumeration member"});
  a.sentiment = *sentiment;

  const Value* rec = find(raw, "recommendation");
  if (rec == nullptr || !rec->is_str())
    return unexpected(ParseError{"recommendation: field required"});
  auto recommendation = normalise_recommendation(rec->str());
  if (!recommendation)
    return unexpected(ParseError{"recommendation: not a valid enumeration member"});
  a.recommendation = *recommendation;

  const Value* score = find(raw, "sentiment_score");
  if (score == nullptr) return unexpected(ParseError{"sentiment_score: field required"});
  // _normalise_score runs in `mode="before"`, so a non-numeric value is passed
  // through untouched and then fails the float check.
  auto sv = score->as_double();
  if (!sv) return unexpected(ParseError{"sentiment_score: not a valid float"});
  a.sentiment_score = normalise_sentiment_score(*sv);
  if (a.sentiment_score < -100.0 || a.sentiment_score > 100.0)
    return unexpected(ParseError{"sentiment_score: out of range [-100, 100]"});

  const Value* conf = find(raw, "confidence");
  if (conf == nullptr) return unexpected(ParseError{"confidence: field required"});
  auto cv = conf->as_double();
  if (!cv) return unexpected(ParseError{"confidence: not a valid float"});
  a.confidence = *cv;
  if (a.confidence < 0.0 || a.confidence > 1.0)
    return unexpected(ParseError{"confidence: out of range [0, 1]"});

  // _key_numbers_shape: DeepSeek emits `[]`, `null` and `[{...}, {...}]` in
  // production. All of them mean "no numbers" or "merge these"; none of them
  // may discard an otherwise-valid analysis. Measured at 100+/day.
  //
  // NOTE: a JSON object is not representable in tb::Value, so the caller
  // flattens key_numbers into the top level before calling (see
  // tools/replay_cpp.cpp) -- an empty list / null simply flattens to nothing.
  a.key_numbers.deal_value_inr_crore = opt_num(raw, "kn.deal_value_inr_crore");
  a.key_numbers.stake_change_pct = opt_num(raw, "kn.stake_change_pct");
  a.key_numbers.dividend_per_share = opt_num(raw, "kn.dividend_per_share");
  a.key_numbers.buyback_value_inr_crore = opt_num(raw, "kn.buyback_value_inr_crore");

  return a;
}

Object AnalysisResponse::to_dict() const {
  // Exactly to_db_columns() + key_numbers, because that is what the rules
  // engine's `field=` names resolve against.
  Object o;
  o["event_type"] = event_type;
  o["sentiment"] = sentiment;
  o["sentiment_score"] = sentiment_score;
  o["confidence"] = confidence;
  o["recommendation"] = recommendation;
  o["rationale"] = reasoning;
  o["summary"] = summary;
  const auto put = [&](const char* k, const std::optional<double>& d) {
    o[k] = d ? Value(*d) : Value{};
  };
  put("deal_value_inr_crore", key_numbers.deal_value_inr_crore);
  put("stake_change_pct", key_numbers.stake_change_pct);
  put("dividend_per_share", key_numbers.dividend_per_share);
  put("buyback_value_inr_crore", key_numbers.buyback_value_inr_crore);
  return o;
}

}  // namespace tb
