#include "tb/event_profiles.hpp"

#include <algorithm>
#include <utility>

#include "tb/str.hpp"

namespace tb {
namespace {

struct Entry {
  std::string_view key;
  EventProfile profile;
};

// Rough taxonomy, straight from event_profiles.py:
//   discrete high-impact catalysts -> let winners run (wider RR, longer hold)
//   earnings                       -> two-sided and whippy (tighter stop)
//   mostly priced-in / cosmetic    -> lower edge (tighter, shorter, higher bar)
//   ambiguous                      -> conservative
//
// Research-informed starting points, not gospel: every field is nullable and
// falls back to the global Settings, so an operator can still tune globally
// and only the deliberate per-event deviations stick.
const std::array<Entry, 15> kProfiles{{
    {"ORDER_WIN", {2.0, std::nullopt, 3.5, 1500, 0.70}},
    {"ACQUISITION", {2.0, std::nullopt, 3.5, 1500, 0.70}},
    {"MERGER", {2.0, std::nullopt, 3.0, 1500, 0.70}},
    {"Q1_RESULTS", {1.8, std::nullopt, 3.0, 900, 0.70}},
    {"Q2_RESULTS", {1.8, std::nullopt, 3.0, 900, 0.70}},
    {"Q3_RESULTS", {1.8, std::nullopt, 3.0, 900, 0.70}},
    {"Q4_RESULTS", {1.8, std::nullopt, 3.0, 900, 0.70}},
    {"ANNUAL_RESULTS", {1.8, std::nullopt, 3.0, 900, 0.70}},
    {"BUYBACK", {1.8, 4.0, 2.5, 900, 0.72}},
    {"DIVIDEND", {1.5, 3.0, 2.0, 600, 0.75}},
    {"BONUS", {1.5, 3.0, 2.0, 600, 0.78}},
    {"STOCK_SPLIT", {1.5, 3.0, 2.0, 600, 0.78}},
    {"RIGHTS_ISSUE", {1.8, 4.0, 2.0, 720, 0.75}},
    {"BOARD_MEETING", {1.8, std::nullopt, 2.0, 600, 0.75}},
    {"OTHER", {2.0, std::nullopt, 2.5, std::nullopt, 0.70}},
}};

}  // namespace

EventProfile profile_for(std::string_view event_type) {
  if (event_type.empty()) return kDefaultProfile;
  const std::string key = upper(strip(event_type));
  const auto it = std::find_if(kProfiles.begin(), kProfiles.end(),
                               [&](const Entry& e) { return e.key == key; });
  return it == kProfiles.end() ? kDefaultProfile : it->profile;
}

}  // namespace tb
