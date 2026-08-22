// Port of app/risk/event_profiles.py (129 lines) -- c++.text §9 PHASE 5.
// §9 says "constexpr tables", and the table is genuinely constant, so it is.
//
// A buyback and an order win are not the same trade. This tunes TRADE
// MANAGEMENT per event type -- stop room, reward:risk, hold window, and a
// confidence floor -- and deliberately does NOT touch position size: sizing
// stays risk-math driven and decoupled from the LLM's conviction. That is a
// product decision, not an oversight.
#pragma once

#include <array>
#include <optional>
#include <string_view>

#include "tb/config.hpp"

namespace tb {

struct ResolvedProfile {
  double sl_atr_mult;
  double sl_default_pct;
  double target_rr;
  int max_hold_seconds;
  double min_confidence;
};

// nullopt on a field means "use the global Settings default".
struct EventProfile {
  std::optional<double> sl_atr_mult;
  std::optional<double> sl_default_pct;
  std::optional<double> target_rr;
  std::optional<int> max_hold_seconds;
  std::optional<double> min_confidence;

  // The per-event min_confidence never LOWERS the global floor -- it can only
  // raise the bar (max of the two).
  ResolvedProfile resolved(const Settings& s) const {
    const double global_floor = s.MIN_SENTIMENT_CONFIDENCE;
    const double ev_floor = min_confidence.value_or(global_floor);
    return ResolvedProfile{
        sl_atr_mult.value_or(s.ATR_STOP_MULT),
        sl_default_pct.value_or(s.DEFAULT_SL_PCT),
        target_rr.value_or(s.DEFAULT_TARGET_RR),
        max_hold_seconds.value_or(s.MAX_HOLD_SECONDS),
        global_floor > ev_floor ? global_floor : ev_floor,
    };
  }
};

// DEFAULT (and any event not listed) falls through to the all-nullopt profile
// = pure Settings.
inline constexpr EventProfile kDefaultProfile{};

// Returns the profile for an event type (DEFAULT for empty/unknown).
// Case-insensitive, and tolerates a raw string as well as the enum value.
EventProfile profile_for(std::string_view event_type);

}  // namespace tb
