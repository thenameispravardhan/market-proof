// Port of app/analyzer/fast_track.py (358 lines) -- c++.text §9 PHASE 5.
//
// This is the module the migration plan singles out: _order_value_near_context
// measured 987 us on the live box, which is 97% of all Python CPU in the
// pipeline (§1.2). See src/fast_track.cpp for what actually made it fast --
// it was not the language.
//
// Coverage is deliberately TIGHT, exactly as in the Python: a wrong
// fast-track BUY costs real money, a missed one just means the LLM decides a
// few seconds later. Returning nullopt means "not my trade".
#pragma once

#include <optional>
#include <string>
#include <string_view>

#include "tb/schemas.hpp"

namespace tb {

// Largest INR amount in the text, normalised to crore. Largest wins because
// filings mention component values next to the total ("orders of Rs 120 crore
// and Rs 330 crore, aggregating to Rs 450 crore").
std::optional<double> parse_inr_crore(std::string_view text);

struct FastTrackMatch {
  std::string pattern;        // machine tag, e.g. "order_win_value"
  AnalysisResponse response;  // same shape the LLM track produces
};

// Headline-only parsers, tried in the Python's order (most specific first).
std::optional<FastTrackMatch> evaluate_fast_track(std::string_view headline);

// Headline has order-win context but NO parseable value -- worth extracting
// the PDF to find the value deterministically.
bool is_hybrid_order_candidate(std::string_view headline);

// Hybrid parse: order-context headline + value from the filing's extracted
// text. Confidence is capped lower than the headline path because the value
// was inferred from document context rather than stated by the exchange.
std::optional<FastTrackMatch> evaluate_fast_track_text(std::string_view headline,
                                                       std::string_view extracted_text);

// Exposed for the parity harness and the benchmark: largest INR value that
// appears NEAR an order-context keyword.
std::optional<double> order_value_near_context(std::string_view text);

}  // namespace tb
