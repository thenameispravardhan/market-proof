// Port of app/risk/market_clock.py (125 lines) -- c++.text §9 PHASE 5.
//
// India Standard Time is a FIXED UTC+5:30 offset with no DST, which the Python
// hardcodes too -- so this needs no tz database, and cannot be broken by a
// stale one on the server.
//
// SEAM (carried over verbatim): there is no exchange-holiday calendar. These
// only exclude weekends. Extend is_trading_day() when a holiday feed lands;
// until then the EOD square-off and the broker's own rejection on a closed
// exchange are the backstops.
#pragma once

#include <chrono>
#include <optional>
#include <string>

namespace tb {

using SysTime = std::chrono::sys_seconds;

inline constexpr std::chrono::minutes kIstOffset{5 * 60 + 30};

// Minutes since IST midnight, and the IST calendar day.
struct IstTime {
  std::chrono::sys_days day;
  int minutes{};  // 0..1439
  bool trading_day{};
};

IstTime to_ist(SysTime now);

// Invariant I3: intraday only. These gate entries and force the square-off.
bool is_market_open(SysTime now);
bool is_entry_window(SysTime now);
bool square_off_due(SysTime now);

// Human-readable reason a new entry is blocked by the clock, or nullopt when
// the window is open. Goes straight into the risk log / audit trail, so the
// strings are part of the parity contract (§10.3 "block reason string").
std::optional<std::string> entry_block_reason(SysTime now);

}  // namespace tb
