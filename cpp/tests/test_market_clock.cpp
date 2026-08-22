// IST is UTC+5:30 with no DST, so every expected value here is arithmetic on
// a fixed offset -- no tz database involved, deliberately (§9 PHASE 5).
#include <cassert>
#include <cstdio>

#include "tb/config.hpp"
#include "tb/market_clock.hpp"

using namespace tb;
using namespace std::chrono;

// 2026-08-19 is a Wednesday; 2026-08-22 a Saturday.
static SysTime utc(int y, int m, int d, int hh, int mm) {
  return sys_days{year{y} / month{static_cast<unsigned>(m)} / day{static_cast<unsigned>(d)}} +
         hours{hh} + minutes{mm};
}
// IST wall-clock time on a trading Wednesday.
static SysTime ist_wed(int hh, int mm) {
  return utc(2026, 8, 19, hh, mm) - kIstOffset;
}
static SysTime ist_sat(int hh, int mm) {
  return utc(2026, 8, 22, hh, mm) - kIstOffset;
}

int main() {
  mutable_settings() = Settings{};  // defaults: 09:15-15:30, entry 09:30-15:00

  assert(to_ist(ist_wed(9, 30)).minutes == 9 * 60 + 30);
  assert(to_ist(ist_wed(9, 30)).trading_day);
  assert(!to_ist(ist_sat(9, 30)).trading_day);

  // -- market open: inclusive on both edges ---------------------------------
  assert(!is_market_open(ist_wed(9, 14)));
  assert(is_market_open(ist_wed(9, 15)));
  assert(is_market_open(ist_wed(15, 30)));
  assert(!is_market_open(ist_wed(15, 31)));
  assert(!is_market_open(ist_sat(11, 0)));  // weekend

  // -- entry window: session minus the noisy first 15 / last 30 min ---------
  assert(!is_entry_window(ist_wed(9, 29)));
  assert(is_entry_window(ist_wed(9, 30)));
  assert(is_entry_window(ist_wed(15, 0)));
  assert(!is_entry_window(ist_wed(15, 1)));
  assert(!is_entry_window(ist_sat(11, 0)));

  // -- square-off: NOT disabled by ENFORCE_MARKET_HOURS (invariant I3) ------
  assert(!square_off_due(ist_wed(15, 9)));
  assert(square_off_due(ist_wed(15, 10)));
  assert(square_off_due(ist_wed(15, 30)));
  assert(!square_off_due(ist_wed(15, 31)));

  // -- block reasons are part of the parity contract (§10.3) ----------------
  assert(!entry_block_reason(ist_wed(10, 0)).has_value());
  assert(*entry_block_reason(ist_wed(8, 0)) == "market_closed");
  assert(*entry_block_reason(ist_sat(11, 0)) == "market_closed");
  assert(*entry_block_reason(ist_wed(9, 20)) == "outside_entry_window");
  assert(*entry_block_reason(ist_wed(15, 20)) == "post_squareoff");

  // -- the gate switches off for paper/backtest, the square-off does not ----
  mutable_settings().ENFORCE_MARKET_HOURS = false;
  assert(is_entry_window(ist_sat(3, 0)));
  assert(!entry_block_reason(ist_sat(3, 0)).has_value());
  assert(!square_off_due(ist_sat(15, 20)));      // still a weekend
  assert(square_off_due(ist_wed(15, 20)));       // still fires on a trading day
  mutable_settings() = Settings{};

  std::puts("test_market_clock OK");
  return 0;
}
