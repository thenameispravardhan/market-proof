// The PURE half of app/risk/position_sizer.py (193 lines) -- §9 PHASE 5.
//
// Only notional_cap_qty() is side-effect free. compute_equity(),
// filled_trade_count() and resolve_risk_pct() all read the DB session (the
// equity ledger, the trade count, RiskState) and belong to Phase 3/9 -- they
// are NOT stubbed here, because a stub on the sizing path is exactly the kind
// of thing that silently sizes a real order wrong.
#pragma once

#include <cmath>

namespace tb {

// Largest qty whose notional (qty * entry) stays within the single-name cap.
// Returns 0 on non-positive inputs.
//
// This is the cap half of "order qty = min(risk-based qty, notional cap qty)";
// it exists so that a very tight stop can never blow past the single-name
// notional limit.
inline int notional_cap_qty(double entry, double equity, double max_single_position_pct) {
  if (entry <= 0 || equity <= 0 || max_single_position_pct <= 0) return 0;
  const double cap_value = equity * (max_single_position_pct / 100.0);
  return static_cast<int>(std::floor(cap_value / entry));
}

}  // namespace tb
