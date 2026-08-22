#include "tb/fast_track.hpp"

#include <re2/re2.h>

#include <algorithm>
#include <charconv>
#include <format>
#include <vector>

#include "tb/str.hpp"

namespace tb {
namespace {

// Transliterated from fast_track.py. `(?i)` replaces re.IGNORECASE. Group 1 is
// the whole match (so spans are exact), 2 the amount, 3 the unit.
//
// INR only -- a USD/EUR order needs an FX judgement call, which is exactly
// what the LLM track is for.
const RE2& inr_value_re() {
  static const RE2 re{
      R"((?i)((?:rs\.?|₹|inr)\s*([\d,]+(?:\.\d+)?)\s*)"
      R"((crores?|cr\b|lakhs?|lacs?|million|mn\b|billion|bn\b)))"};
  return re;
}

// Word-boundary, not substring: short tokens like "won" / "loa" would
// otherwise match inside unrelated words ("wonder", "upload").
const RE2& order_context_re() {
  static const RE2 re{
      R"((?i)\b(?:orders?|contracts?|work order|purchase order|)"
      R"(letter of award|letter of intent|loa|loi|)"
      R"(bags?|bagged|secures?|secured|wins?|won|awarded)\b)"};
  return re;
}

double unit_to_crore(std::string_view u) {
  if (u == "crore" || u == "crores" || u == "cr") return 1.0;
  if (u == "lakh" || u == "lakhs" || u == "lac" || u == "lacs") return 0.01;
  if (u == "million" || u == "mn") return 0.1;  // 1e6 INR = 0.1 crore
  if (u == "billion" || u == "bn") return 100.0;
  return 0.0;
}

// One INR match: its byte span in the scanned buffer and its crore value.
struct ValueHit {
  std::size_t begin{};
  std::size_t end{};
  double crore{};
};

// Python's float(group(1).replace(",", "")). A capture of bare commas yields
// an empty string, which raises ValueError there and is skipped here.
std::optional<double> parse_amount(std::string_view digits) {
  std::string clean;
  clean.reserve(digits.size());
  for (char c : digits)
    if (c != ',') clean.push_back(c);
  if (clean.empty()) return std::nullopt;
  double out{};
  const char* const end = clean.data() + clean.size();
  auto [p, ec] = std::from_chars(clean.data(), end, out);
  if (ec != std::errc{} || p != end) return std::nullopt;
  return out;
}

// Every INR value in `text`, left to right, non-overlapping -- one pass.
std::vector<ValueHit> scan_inr_values(std::string_view text) {
  std::vector<ValueHit> hits;
  re2::StringPiece input(text.data(), text.size());
  re2::StringPiece whole, amount, unit;
  const char* const base = text.data();
  while (RE2::FindAndConsume(&input, inr_value_re(), &whole, &amount, &unit)) {
    auto amt = parse_amount(std::string_view(amount.data(), amount.size()));
    if (!amt) continue;
    std::string u = lower(std::string_view(unit.data(), unit.size()));
    if (const auto last = u.find_last_not_of('.'); last != std::string::npos) u.resize(last + 1);
    const double value = *amt * unit_to_crore(u);
    if (value <= 0) continue;
    const auto begin = static_cast<std::size_t>(whole.data() - base);
    hits.push_back(ValueHit{begin, begin + whole.size(), value});
  }
  return hits;
}

constexpr double kMinCrore = 25.0;

AnalysisResponse make_response(std::string_view event_type, std::string summary,
                               std::string_view sentiment, double score, double confidence,
                               std::string_view recommendation, std::string reasoning) {
  AnalysisResponse r;
  r.event_type = event_type;
  r.summary = std::move(summary);
  r.sentiment = sentiment;
  r.sentiment_score = score;
  r.confidence = confidence;
  r.recommendation = recommendation;
  r.reasoning = std::move(reasoning);
  return r;
}

// Any of these anywhere in the headline kills the order-win fast track -- the
// "order" is going away, not arriving, or is still hypothetical.
constexpr std::string_view kOrderNegativeGuards[] = {
    "cancel", "terminat", "withdraw",         "suspend", "dispute",
    "bid",    "bidding",  "tender submitted", "participat"};

// Senior-management roles whose exit is a tradeable negative.
constexpr std::string_view kKmpRoles[] = {"managing director",
                                          "chief executive",
                                          "ceo",
                                          "chief financial officer",
                                          "cfo",
                                          "whole-time director",
                                          "whole time director",
                                          "chairman"};

// Independent / non-executive exits are governance churn, and a combined
// "resignation and appointment" is routine succession: both go to the LLM.
constexpr std::string_view kResignationSkipGuards[] = {
    "independent director", "non-executive", "non executive", "appointment",
    "appointed",            "re-appoint",    "reappoint"};

constexpr std::string_view kBuybackContext[] = {"buyback", "buy-back"};
constexpr std::string_view kBuybackApproval[] = {"approve", "consider", "board meeting"};

template <std::size_t N>
bool any_of_in(std::string_view hay, const std::string_view (&needles)[N]) {
  return std::any_of(std::begin(needles), std::end(needles),
                     [&](std::string_view n) { return contains(hay, n); });
}

bool has_order_context(std::string_view lc) {
  return RE2::PartialMatch(re2::StringPiece(lc.data(), lc.size()), order_context_re());
}

// Python's f"{value:g}" -- printf %g semantics, 6 significant digits with
// trailing zeros stripped. std::format's `g` is specified to match it.
std::string fmt_g(double v) { return std::format("{:g}", v); }

}  // namespace

std::optional<double> parse_inr_crore(std::string_view text) {
  std::optional<double> best;
  for (const ValueHit& h : scan_inr_values(text))
    if (!best || h.crore > *best) best = h.crore;
  return best;
}

// -----------------------------------------------------------------------------
// The hot one. §1.2 measured the Python at 987 us over an 11.6k-char body,
// which is 97% of all Python CPU in the pipeline.
//
// This is a FAITHFUL port of the windowed algorithm, not a clever rewrite.
// A two-linear-pass version was written first and then thrown away: profiling
// the Python on synthetic 11.6k-char filings (scripts/verify_single_pass.py)
// showed where the time actually goes --
//
//     ORDER_CONTEXT scan, whole text   1664 us   <- 68% of the function
//     INR scan, whole text              813 us   <- what one-pass would cost
//     INR scan, 12 windows only         264 us   <- what the windows cost
//     normalize + lower                 289 us
//
// -- so the +-400 char window is a PRUNING step that keeps the expensive INR
// pattern away from 97% of the document, and replacing it with a full-text
// scan made the function ~14% SLOWER. The dominant cost is the 19-alternative
// order-context pattern, which both versions pay in full.
//
// The consequence for the plan: §9 PHASE 5 is right that Hyperscan belongs
// here, but the pattern worth handing it is _ORDER_CONTEXT_RE, not the INR
// scan the note points at. Deferred to Phase 12 (the optimisation pass) --
// RE2 over this shape should already recover most of it. See cpp/DIFFS.md.
// -----------------------------------------------------------------------------
std::optional<double> order_value_near_context(std::string_view text) {
  CpMap map;
  const std::string normalized = collapse_ws(text, &map);
  if (normalized.empty()) return std::nullopt;

  std::optional<double> best;
  re2::StringPiece input(normalized.data(), normalized.size());
  re2::StringPiece m;
  const char* const base = normalized.data();
  while (RE2::FindAndConsume(&input, order_context_re(), &m)) {
    const auto mb = static_cast<std::size_t>(m.data() - base);
    // Python slices by code point: normalized[max(0, start-150) : end+250].
    const std::size_t lo = map.byte_at(static_cast<std::int64_t>(map.cp_at(mb)) - 150);
    const std::size_t hi = map.byte_at(static_cast<std::int64_t>(map.cp_at(mb + m.size())) + 250);
    const auto value =
        parse_inr_crore(std::string_view(normalized).substr(lo, hi - lo));
    if (value && (!best || *value > *best)) best = value;
  }
  return best;
}

namespace {

std::optional<FastTrackMatch> match_order_win(std::string_view lc, std::string_view headline) {
  if (!has_order_context(lc)) return std::nullopt;
  if (any_of_in(lc, kOrderNegativeGuards)) return std::nullopt;
  auto value = parse_inr_crore(headline);
  // No value, or too small to be an unambiguous catalyst -> LLM track.
  if (!value || *value < kMinCrore) return std::nullopt;

  double confidence = 0.72, score = 60.0;
  if (*value >= 500.0) {
    confidence = 0.88;
    score = 80.0;
  } else if (*value >= 100.0) {
    confidence = 0.80;
    score = 70.0;
  }
  const std::string g = fmt_g(*value);
  AnalysisResponse r = make_response(
      "ORDER_WIN",
      std::format("Order win with explicit value of Rs {} crore in the exchange headline.", g),
      "positive", score, confidence, "BUY",
      std::format("Deterministic fast track: headline contains an order-win context and an "
                  "explicit value of Rs {} crore. Headline: \"{}\"",
                  g, strip(headline)));
  r.key_numbers.deal_value_inr_crore = *value;
  return FastTrackMatch{"order_win_value", std::move(r)};
}

std::optional<FastTrackMatch> match_buyback(std::string_view lc, std::string_view headline) {
  if (!any_of_in(lc, kBuybackContext)) return std::nullopt;
  if (!any_of_in(lc, kBuybackApproval)) return std::nullopt;
  auto value = parse_inr_crore(headline);
  if (!value || *value < kMinCrore) return std::nullopt;
  const std::string g = fmt_g(*value);
  AnalysisResponse r = make_response(
      "BUYBACK", std::format("Buyback of Rs {} crore per the exchange headline.", g), "positive",
      65.0, 0.78, "BUY",
      std::format("Deterministic fast track: headline reports a buyback with an explicit "
                  "value of Rs {} crore. Headline: \"{}\"",
                  g, strip(headline)));
  r.key_numbers.buyback_value_inr_crore = *value;
  return FastTrackMatch{"buyback_value", std::move(r)};
}

std::optional<FastTrackMatch> match_kmp_resignation(std::string_view lc,
                                                    std::string_view headline) {
  if (!contains(lc, "resign")) return std::nullopt;
  if (any_of_in(lc, kResignationSkipGuards)) return std::nullopt;
  // Python's next((r for r in _KMP_ROLES if r in headline_lc), None): the
  // FIRST role in DECLARATION order, not the earliest one in the headline.
  const std::string_view* role = nullptr;
  for (const std::string_view& r : kKmpRoles)
    if (contains(lc, r)) {
      role = &r;
      break;
    }
  if (role == nullptr) return std::nullopt;
  return FastTrackMatch{
      "kmp_resignation",
      make_response(
          "OTHER",
          std::format("Resignation of a key managerial person ({}) per the exchange headline.",
                      *role),
          "negative", -65.0, 0.75, "SELL",
          std::format("Deterministic fast track: headline reports the resignation of the {} "
                      "with no accompanying appointment. Headline: \"{}\"",
                      *role, strip(headline)))};
}

}  // namespace

std::optional<FastTrackMatch> evaluate_fast_track(std::string_view headline) {
  if (blank(headline)) return std::nullopt;
  const std::string lc = lower(headline);
  // Order matters: most specific / highest-conviction parser first.
  if (auto m = match_order_win(lc, headline)) return m;
  if (auto m = match_buyback(lc, headline)) return m;
  if (auto m = match_kmp_resignation(lc, headline)) return m;
  return std::nullopt;
}

bool is_hybrid_order_candidate(std::string_view headline) {
  if (headline.empty()) return false;
  const std::string lc = lower(headline);
  if (!has_order_context(lc)) return false;
  if (any_of_in(lc, kOrderNegativeGuards)) return false;
  return !parse_inr_crore(headline).has_value();
}

std::optional<FastTrackMatch> evaluate_fast_track_text(std::string_view headline,
                                                       std::string_view extracted_text) {
  if (!is_hybrid_order_candidate(headline)) return std::nullopt;
  if (extracted_text.empty()) return std::nullopt;
  // A cancellation/termination ANYWHERE in the filing kills it -- too risky to
  // auto-trade a document we only pattern-matched.
  if (any_of_in(lower(extracted_text), kOrderNegativeGuards)) return std::nullopt;

  auto value = order_value_near_context(extracted_text);
  if (!value || *value < kMinCrore) return std::nullopt;

  double confidence = 0.72, score = 60.0;
  if (*value >= 500.0) {
    confidence = 0.85;
    score = 75.0;
  } else if (*value >= 100.0) {
    confidence = 0.78;
    score = 68.0;
  }
  const std::string g = fmt_g(*value);
  AnalysisResponse r = make_response(
      "ORDER_WIN",
      std::format("Order win: headline indicates an order and the filing text states a value "
                  "of Rs {} crore.",
                  g),
      "positive", score, confidence, "BUY",
      std::format("Deterministic hybrid fast track: order-win context in the headline; Rs {} "
                  "crore parsed from the filing text near the order mention. Headline: \"{}\"",
                  g, strip(headline)));
  r.key_numbers.deal_value_inr_crore = *value;
  return FastTrackMatch{"order_win_pdf_value", std::move(r)};
}

}  // namespace tb
