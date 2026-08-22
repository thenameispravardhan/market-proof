#include <cassert>
#include <cstdio>

#include "tb/config.hpp"
#include "tb/event_profiles.hpp"
#include "tb/sizing.hpp"

using namespace tb;

int main() {
  // -- notional cap: floor(equity * pct/100 / entry) ------------------------
  assert(notional_cap_qty(100.0, 1'000'000.0, 20.0) == 2000);
  assert(notional_cap_qty(333.0, 1'000'000.0, 20.0) == 600);  // floor, not round
  assert(notional_cap_qty(0.0, 1'000'000.0, 20.0) == 0);
  assert(notional_cap_qty(100.0, 0.0, 20.0) == 0);
  assert(notional_cap_qty(100.0, 1'000'000.0, 0.0) == 0);
  assert(notional_cap_qty(-5.0, 1'000'000.0, 20.0) == 0);

  // -- event profiles -------------------------------------------------------
  const Settings s;
  {
    const auto p = profile_for("ORDER_WIN").resolved(s);
    assert(p.sl_atr_mult == 2.0 && p.target_rr == 3.5 && p.max_hold_seconds == 1500);
    assert(p.sl_default_pct == s.DEFAULT_SL_PCT);  // unset -> global
  }
  {
    // Unknown / empty / DEFAULT all fall through to pure Settings.
    for (const char* k : {"", "FUND_RAISING", "DEFAULT"}) {
      const auto p = profile_for(k).resolved(s);
      assert(p.sl_atr_mult == s.ATR_STOP_MULT);
      assert(p.target_rr == s.DEFAULT_TARGET_RR);
      assert(p.max_hold_seconds == s.MAX_HOLD_SECONDS);
      assert(p.min_confidence == s.MIN_SENTIMENT_CONFIDENCE);
    }
  }
  // Case-insensitive, and tolerates surrounding whitespace.
  assert(profile_for(" dividend ").resolved(s).target_rr == 2.0);

  // A per-event floor can only RAISE the global bar, never lower it.
  {
    Settings strict;
    strict.MIN_SENTIMENT_CONFIDENCE = 0.90;
    assert(profile_for("ORDER_WIN").resolved(strict).min_confidence == 0.90);  // 0.70 ignored
    Settings loose;
    loose.MIN_SENTIMENT_CONFIDENCE = 0.50;
    assert(profile_for("BONUS").resolved(loose).min_confidence == 0.78);       // event wins
  }

  std::puts("test_sizing OK");
  return 0;
}
