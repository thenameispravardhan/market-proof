// The Settings keys the Phase 5 leaves read, with config.py's defaults.
//
// ponytail: this is NOT the config port. app/config.py is 617 lines with .env
// loading, DB-backed app_settings overrides and precedence rules, and that is
// Phase 3's job (§9 PHASE 3: "Settings struct via glaze reflection; same keys
// as config.py"). Porting it now would be scaffolding for a phase that has not
// started. These are the seven values the pure logic actually reads.
//
// The defaults below are config.py's, which the memory note "config drift: env
// vs code" flags as the AUTHORITATIVE ones -- .env.example disagrees and is
// wrong.
#pragma once

#include <string>

namespace tb {

struct Settings {
  // -- market clock (config.py L441-448) --
  std::string MARKET_OPEN_IST{"09:15"};
  std::string MARKET_CLOSE_IST{"15:30"};
  std::string ENTRY_WINDOW_START_IST{"09:30"};
  std::string ENTRY_WINDOW_END_IST{"15:00"};
  std::string SQUARE_OFF_TIME_IST{"15:10"};
  bool ENFORCE_MARKET_HOURS{true};

  // -- trade management (config.py L104-211) --
  double PORTFOLIO_VALUE{1'000'000.0};
  double MAX_SINGLE_POSITION_PCT{20.0};
  double DEFAULT_SL_PCT{6.0};
  double DEFAULT_TARGET_RR{3.0};
  double ATR_STOP_MULT{2.0};
  int MAX_HOLD_SECONDS{1080};
  double MIN_SENTIMENT_CONFIDENCE{0.7};

  // -- graduated risk ramp (config.py L185-186) --
  int RISK_RAMP_TRADES{100};
  double RISK_RAMP_START_PCT{0.5};
};

// Process-wide settings. Phase 3 replaces the body with the real loader; every
// caller already goes through this seam so nothing above has to change.
const Settings& settings();
Settings& mutable_settings();  // tests and the replay harness only

}  // namespace tb
