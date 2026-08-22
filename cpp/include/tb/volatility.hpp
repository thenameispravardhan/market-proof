// The PURE half of app/risk/volatility.py (317 lines) -- c++.text §9 PHASE 5.
//
// Ported: compute_atr, stop_distance, vix_risk_multiplier -- side-effect-free
// math on the money path.
//
// NOT ported, deliberately: VolatilityProvider / FyersCandleVolatilityProvider
// / FyersVolatilityRegime all wrap a live candle+VIX feed, so they are Phase 9
// (execution core), not a pure leaf. `resolve_atr` is only a try/except around
// a provider call and has nothing to port without one.
#pragma once

#include <algorithm>
#include <cmath>
#include <optional>
#include <span>

namespace tb {

// One candle reduced to what ATR needs. The Python accepts four shapes
// (tuple, Fyers [ts,o,h,l,c,v] row, dict, object); shape-sniffing is a
// JSON-decoding concern, so the C++ takes the reduced form and leaves the
// sniffing to whoever owns the feed.
struct Hlc {
  double high{};
  double low{};
  double close{};
};

// Wilder's ATR over `candles` (oldest -> newest).
//
// Returns nullopt when there are not enough candles (needs > period, so the
// first true range has a previous close) or when the result is non-positive.
inline std::optional<double> compute_atr(std::span<const Hlc> candles, int period = 14) {
  if (period < 1) return std::nullopt;
  const auto n = static_cast<int>(candles.size());
  if (n < period + 1) return std::nullopt;

  // true_ranges has n-1 entries; the Python re-checks len < period after
  // building it, which cannot fail given the guard above -- kept implicit.
  double prev_close = candles[0].close;
  double sum = 0.0;
  int seeded = 0;
  double atr = 0.0;
  for (int i = 1; i < n; ++i) {
    const Hlc& c = candles[static_cast<std::size_t>(i)];
    const double tr = std::max({c.high - c.low, std::fabs(c.high - prev_close),
                                std::fabs(c.low - prev_close)});
    prev_close = c.close;
    if (seeded < period) {
      // Seed with the simple average of the first `period` true ranges...
      sum += tr;
      if (++seeded == period) atr = sum / period;
      continue;
    }
    // ...then smooth. Same recurrence as the Python, same order, so the
    // floating-point result is bit-identical.
    atr = (atr * (period - 1) + tr) / period;
  }
  return atr > 0 ? std::optional<double>{atr} : std::nullopt;
}

// Stop distance in price units for `entry`.
//
//   ATR path : atr * mult, clamped to [floor, max_pct% of entry]
//   fallback : default_pct% of entry (when atr is absent or <= 0)
//
// The floor is min_pct% of entry, or smallcap_pct% for sub-smallcap_price
// names, which are noisier in percentage terms. Always positive so the caller
// can size against it -- a zero stop would divide by zero in the sizer.
inline double stop_distance(double entry, std::optional<double> atr, double mult,
                            double default_pct, double min_pct, double max_pct,
                            double smallcap_price, double smallcap_pct) {
  if (entry <= 0) return 0.0;
  const double floor_pct = entry < smallcap_price ? smallcap_pct : min_pct;
  const double floor = entry * (floor_pct / 100.0);
  const double ceil = entry * (max_pct / 100.0);
  const double dist =
      (atr && *atr > 0) ? (*atr * mult) : entry * (default_pct / 100.0);
  // Guard an inverted band: an operator can set min_pct above max_pct.
  const double lo = std::min(floor, ceil);
  const double hi = std::max(floor, ceil);
  return std::max(lo, std::min(dist, hi));
}

// VIX level -> per-trade risk multiplier. Size down as expected volatility
// rises so the constant dollar-risk assumption holds.
//
// Adjusts RISK SIZING only. It does not touch the LLM's conviction -- sizing
// stays decoupled from conviction by design (same rule as event_profiles).
inline double vix_risk_multiplier(std::optional<double> vix, double calm_below = 18.0,
                                  double elevated_below = 25.0, double elevated_mult = 0.75,
                                  double high_mult = 0.5) {
  if (!vix) return 1.0;  // no data -> no throttle
  if (*vix < calm_below) return 1.0;
  if (*vix < elevated_below) return elevated_mult;
  return high_mult;
}

}  // namespace tb
