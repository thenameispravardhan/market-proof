// Expected values recorded from app/risk/volatility.py -- see
// scripts/gen_volatility_cases.py. ATR is a floating-point recurrence, so the
// numbers are asserted to 1e-12 relative rather than eyeballed: a reordered
// accumulation would drift and this catches it.
#include <cassert>
#include <cmath>
#include <cstdio>
#include <vector>

#include "tb/volatility.hpp"

using namespace tb;

static void close_to(double got, double want) {
  assert(std::fabs(got - want) <= 1e-12 * std::fabs(want));
}

int main() {
  const std::vector<Hlc> c{
      {101.6098, 99.293, 100.4916},  {103.4052, 100.8552, 102.2701},
      {101.0442, 98.9235, 100.3434}, {102.3054, 100.7699, 100.9413},
      {101.1893, 99.9941, 100.8164}, {101.1345, 100.7859, 101.1146},
      {101.6003, 99.0715, 100.2227}, {100.0401, 98.6523, 98.8581},
      {99.5113, 99.3199, 99.3225},   {101.1148, 100.4723, 100.7981},
      {104.0877, 102.2973, 102.7432}, {105.4861, 103.5758, 104.6397},
      {104.8636, 102.3328, 103.404}, {106.7459, 104.8617, 105.3338},
      {105.0097, 104.52, 104.749},   {103.3922, 101.9958, 102.9269},
      {101.9082, 100.371, 100.8823}, {101.3446, 99.3935, 100.1154},
      {100.0951, 98.3273, 99.3778},  {99.0446, 97.5833, 97.6168},
      {99.8416, 98.5654, 98.5922},   {100.2747, 98.8615, 99.7269},
      {97.8371, 97.5033, 97.7686},   {99.8421, 98.4202, 99.5487},
      {102.6904, 100.7365, 101.2595}, {101.4637, 99.5002, 100.6714},
      {100.2055, 97.9081, 99.0931},  {100.574, 99.0927, 100.5188},
      {99.3804, 97.9691, 98.875},    {101.0412, 99.135, 100.5286}};

  // -- Wilder's ATR: seed with a simple mean, then smooth ------------------
  close_to(*compute_atr(c, 14), 2.166262717737017);
  close_to(*compute_atr(c, 5), 2.222214576625464);
  close_to(*compute_atr(c, 1), 2.1662000000000035);

  assert(!compute_atr(c, 0));                       // period < 1
  assert(!compute_atr(std::span(c).first(5), 14));  // needs > period candles
  assert(!compute_atr(std::span<const Hlc>{}, 14)); // empty

  // Exactly period+1 candles is the boundary that must WORK, not fail.
  assert(compute_atr(std::span(c).first(15), 14).has_value());
  assert(!compute_atr(std::span(c).first(14), 14));

  // -- stop distance: the money path ---------------------------------------
  const double dflt = 6.0, mn = 1.0, mx = 10.0, sc_price = 50.0, sc_pct = 2.0;
  close_to(stop_distance(100.0, 2.0, 2.0, dflt, mn, mx, sc_price, sc_pct), 4.0);
  // No ATR -> the %-stop fallback, never a zero stop.
  close_to(stop_distance(100.0, std::nullopt, 2.0, dflt, mn, mx, sc_price, sc_pct), 6.0);
  // A huge ATR is clamped to max_pct, so one wild candle cannot size the book out.
  close_to(stop_distance(100.0, 50.0, 2.0, dflt, mn, mx, sc_price, sc_pct), 10.0);
  // A tiny ATR is floored, so a quiet name cannot produce a hair-trigger stop.
  close_to(stop_distance(100.0, 0.01, 2.0, dflt, mn, mx, sc_price, sc_pct), 1.0);
  // Sub-smallcap_price names get the wider 2% floor: 30 * 2% = 0.6.
  close_to(stop_distance(30.0, 0.01, 2.0, dflt, mn, mx, sc_price, sc_pct), 0.6);
  // Non-positive entry -> 0.0 (the caller must not divide by it).
  close_to(stop_distance(0.0, 2.0, 2.0, dflt, mn, mx, sc_price, sc_pct), 0.0);
  // Operator inverted the band (min > max): clamp still behaves.
  close_to(stop_distance(100.0, 2.0, 2.0, dflt, 10.0, 1.0, sc_price, sc_pct), 4.0);
  // A negative ATR is treated as absent, not as a negative distance.
  close_to(stop_distance(100.0, -5.0, 2.0, dflt, mn, mx, sc_price, sc_pct), 6.0);

  // -- VIX throttle: boundaries are inclusive-below ------------------------
  close_to(vix_risk_multiplier(std::nullopt), 1.0);  // no data -> no throttle
  close_to(vix_risk_multiplier(0.0), 1.0);
  close_to(vix_risk_multiplier(17.9), 1.0);
  close_to(vix_risk_multiplier(18.0), 0.75);
  close_to(vix_risk_multiplier(24.9), 0.75);
  close_to(vix_risk_multiplier(25.0), 0.5);
  close_to(vix_risk_multiplier(100.0), 0.5);

  std::puts("test_volatility OK");
  return 0;
}
