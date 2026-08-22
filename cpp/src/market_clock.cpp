#include "tb/market_clock.hpp"

#include "tb/config.hpp"

namespace tb {
namespace {

// Settings already validates the "HH:MM" format, so this is a thin split.
int parse_hhmm(const std::string& v) {
  const auto colon = v.find(':');
  if (colon == std::string::npos) return 0;
  return std::stoi(v.substr(0, colon)) * 60 + std::stoi(v.substr(colon + 1));
}

bool within(int t, const std::string& lo, const std::string& hi) {
  return parse_hhmm(lo) <= t && t <= parse_hhmm(hi);  // inclusive, as in Python
}

}  // namespace

IstTime to_ist(SysTime now) {
  const auto ist = now + kIstOffset;
  const auto day = std::chrono::floor<std::chrono::days>(ist);
  const std::chrono::weekday wd{day};
  IstTime out;
  out.day = day;
  out.minutes = static_cast<int>(std::chrono::duration_cast<std::chrono::minutes>(ist - day).count());
  // Python's weekday(): Monday=0 .. Friday=4 are trading days.
  out.trading_day = wd.iso_encoding() <= 5;
  return out;
}

bool is_market_open(SysTime now) {
  const IstTime t = to_ist(now);
  if (!t.trading_day) return false;
  const Settings& s = settings();
  return within(t.minutes, s.MARKET_OPEN_IST, s.MARKET_CLOSE_IST);
}

bool is_entry_window(SysTime now) {
  const Settings& s = settings();
  // Disabled gate: paper testing / backtests must not be blocked by the clock.
  if (!s.ENFORCE_MARKET_HOURS) return true;
  const IstTime t = to_ist(now);
  if (!t.trading_day) return false;
  return within(t.minutes, s.ENTRY_WINDOW_START_IST, s.ENTRY_WINDOW_END_IST);
}

bool square_off_due(SysTime now) {
  // Deliberately NOT gated by ENFORCE_MARKET_HOURS: if you square off, it
  // happens on the real clock regardless of the entry gate (invariant I3).
  const IstTime t = to_ist(now);
  if (!t.trading_day) return false;
  const Settings& s = settings();
  return within(t.minutes, s.SQUARE_OFF_TIME_IST, s.MARKET_CLOSE_IST);
}

std::optional<std::string> entry_block_reason(SysTime now) {
  if (!settings().ENFORCE_MARKET_HOURS) return std::nullopt;
  if (is_entry_window(now)) return std::nullopt;
  if (!is_market_open(now)) return "market_closed";
  if (square_off_due(now)) return "post_squareoff";
  // Open, but inside an excluded edge (first 15 / last 30 minutes).
  return "outside_entry_window";
}

}  // namespace tb
