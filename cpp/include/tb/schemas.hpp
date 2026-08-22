// Port of app/analyzer/schemas.py (252 lines) -- c++.text §9 PHASE 5.
//
// Every validator below is a deliberate transliteration of a pydantic
// `field_validator`, including the production coercion fixes the migration
// plan calls out by name (§9 PHASE 8: "preserving the NEUTRAL->HOLD and
// key_numbers coercion fixes -- do not regress them"). Each of those was
// worth 100+ analyses/day that would otherwise be thrown away, so they are
// load-bearing, not defensive noise.
#pragma once

#include <array>
#include <expected>
#include <optional>
#include <string>
#include <string_view>

#include "tb/value.hpp"

namespace tb {

// The 15 event categories, in schemas.py declaration order.
inline constexpr std::array<std::string_view, 15> kEventTypes{
    "ORDER_WIN", "ACQUISITION", "MERGER",        "DIVIDEND",       "BUYBACK",
    "STOCK_SPLIT", "BONUS",     "RIGHTS_ISSUE",  "Q1_RESULTS",     "Q2_RESULTS",
    "Q3_RESULTS",  "Q4_RESULTS", "ANNUAL_RESULTS", "BOARD_MEETING", "OTHER"};

inline constexpr std::string_view kDefaultEventType = "DEFAULT";

// LLM-invented "don't trade" synonyms that all MEAN hold.
inline constexpr std::array<std::string_view, 6> kHoldSynonyms{
    "NEUTRAL", "AVOID", "NONE", "WAIT", "WATCH", "NO_TRADE"};

struct KeyNumbers {
  std::optional<double> deal_value_inr_crore;
  std::optional<double> stake_change_pct;
  std::optional<double> dividend_per_share;
  std::optional<double> buyback_value_inr_crore;
};

struct AnalysisResponse {
  std::string event_type;       // one of kEventTypes
  std::string summary;          // non-blank, stripped
  std::string sentiment;        // positive | neutral | negative
  double sentiment_score{};     // -100..100
  double confidence{};          // 0..1
  std::string recommendation;   // BUY | SELL | HOLD
  std::string reasoning;        // non-blank, stripped
  KeyNumbers key_numbers{};

  // analysis_to_dict(): the exact shape the rules engine evaluates against.
  Object to_dict() const;
};

// -- the individual validators, exposed so the parity harness can pin them ---

// _event_type_or_other: uppercase, and map any label outside the 15 onto
// OTHER rather than discarding an otherwise-usable analysis.
std::string normalise_event_type(std::string_view raw);

// _upper_enum: uppercase, fold the hold synonyms. Returns nullopt for a value
// that is still not BUY/SELL/HOLD -- a garbled response must fail loudly
// rather than be silently traded on.
std::optional<std::string> normalise_recommendation(std::string_view raw);

// _lower_enum + enum validation.
std::optional<std::string> normalise_sentiment(std::string_view raw);

// _normalise_score: DeepSeek often answers on a 0..1 (or -1..1) scale despite
// the prompt. A magnitude <= 1 (and non-zero) is treated as a fraction and
// scaled by 100; a genuine small integer score like 5 is outside [-1,1] and
// passes through. Exactly 0 stays neutral.
double normalise_sentiment_score(double raw);

// Validation failure carries the reason so the harness can diff block reasons
// against Python's ValidationError text.
struct ParseError {
  std::string what;
};

// Validate an already-parsed analysis object (the LLM's JSON, or the fast
// track's construction) into an AnalysisResponse.
std::expected<AnalysisResponse, ParseError> validate_analysis(const Object& raw);

}  // namespace tb
