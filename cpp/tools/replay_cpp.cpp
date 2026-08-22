// Parity harness driver -- c++.text §10.
//
//     replay_cpp < case.json > actual.json
//
// One corpus case in, this stack's decision out. scripts/parity_check.py feeds
// the SAME case to the Python and diffs the two. A phase is not done when the
// code compiles; it is done when this reports zero diffs (§10.6).
//
// Only the Phase 5 leaves are wired: fast track, schema validation, the rules
// engine and the market clock. Risk sizing, entry state and order intent join
// as Phase 9 lands -- the output object gains keys, it never changes existing
// ones, so recorded expectations stay valid.
#include <glaze/glaze.hpp>

#include <chrono>
#include <cstdlib>
#include <format>
#include <iostream>
#include <iterator>
#include <optional>
#include <string>
#include <vector>

#include "tb/config.hpp"
#include "tb/fast_track.hpp"
#include "tb/hist.hpp"
#include "tb/market_clock.hpp"
#include "tb/rules_engine.hpp"
#include "tb/schemas.hpp"

namespace {

using glz::json_t;

tb::Value to_value(const json_t& j) {
  if (j.is_null()) return tb::Value{};
  if (j.is_boolean()) return tb::Value{j.get<bool>()};
  if (j.is_number()) return tb::Value{j.get<double>()};
  if (j.is_string()) return tb::Value{j.get<std::string>()};
  if (j.is_array()) {
    tb::Array a;
    for (const auto& e : j.get_array()) a.push_back(to_value(e));
    return tb::Value{std::move(a)};
  }
  return tb::Value{};  // an object has no Value representation -- see below
}

const json_t* at(const json_t& o, std::string_view k) {
  if (!o.is_object()) return nullptr;
  const auto& m = o.get_object();
  const auto it = m.find(std::string(k));
  return it == m.end() ? nullptr : &it->second;
}

// The analysis dict the rules engine evaluates is FLAT, so key_numbers is
// flattened onto it -- exactly what analysis_to_dict() produces on the Python
// side. The `kn.` prefix is what validate_analysis() reads.
void flatten_key_numbers(const json_t& raw, tb::Object& out) {
  const json_t* kn = at(raw, "key_numbers");
  if (kn == nullptr) return;
  // _key_numbers_shape: null and [] both mean "no numbers"; a list of objects
  // is merged left-to-right; anything else degrades to "no numbers". Never
  // discard an otherwise-valid analysis over this (100+/day in production).
  std::vector<const json_t*> objects;
  if (kn->is_object()) {
    objects.push_back(kn);
  } else if (kn->is_array()) {
    for (const auto& e : kn->get_array()) {
      if (!e.is_object()) {  // a list of non-objects -> {}
        objects.clear();
        break;
      }
      objects.push_back(&e);
    }
  }
  for (const json_t* o : objects)
    for (const auto& [k, v] : o->get_object()) out["kn." + k] = to_value(v);
}

tb::Condition to_condition(const json_t& j, std::string& err) {
  tb::Condition c;
  if (!j.is_object()) {
    err = "condition must be a dict";
    return c;
  }
  if (const json_t* f = at(j, "field"); f != nullptr && f->is_string()) c.field = f->get<std::string>();
  if (const json_t* o = at(j, "op"); o != nullptr && o->is_string()) c.op = o->get<std::string>();
  if (const json_t* v = at(j, "value"); v != nullptr) c.value = to_value(*v);
  return c;
}

tb::Conditions to_conditions(const json_t& j) {
  tb::Conditions out;
  if (!j.is_object()) return out;
  const auto group = [&](const char* key, std::optional<std::vector<tb::Condition>>& dst) {
    const json_t* g = at(j, key);
    if (g == nullptr || g->is_null()) return;
    if (!g->is_array()) {
      out.parse_error = std::string("`") + key + "` must be a non-empty list of conditions";
      return;
    }
    std::vector<tb::Condition> cs;
    for (const auto& e : g->get_array()) cs.push_back(to_condition(e, out.parse_error));
    dst = std::move(cs);
  };
  group("all_of", out.all_of);
  group("any_of", out.any_of);
  return out;
}

std::vector<tb::Rule> to_rules(const json_t& j) {
  std::vector<tb::Rule> rules;
  if (!j.is_array()) return rules;
  for (const auto& e : j.get_array()) {
    tb::Rule r;
    if (const json_t* v = at(e, "id"); v != nullptr && v->is_number())
      r.id = static_cast<int>(v->get<double>());
    if (const json_t* v = at(e, "name"); v != nullptr && v->is_string()) r.name = v->get<std::string>();
    if (const json_t* v = at(e, "priority"); v != nullptr && v->is_number())
      r.priority = static_cast<int>(v->get<double>());
    if (const json_t* v = at(e, "enabled"); v != nullptr && v->is_boolean()) r.enabled = v->get<bool>();
    if (const json_t* v = at(e, "action"); v != nullptr && v->is_string()) r.action = v->get<std::string>();
    if (const json_t* v = at(e, "action_params"); v != nullptr && v->is_object())
      for (const auto& [k, pv] : v->get_object()) r.action_params[k] = to_value(pv);
    if (const json_t* v = at(e, "conditions"); v != nullptr) r.conditions = to_conditions(*v);
    rules.push_back(std::move(r));
  }
  return rules;
}

std::string quote(std::string_view s) {
  std::string out = "\"";
  for (char c : s) {
    switch (c) {
      case '"': out += "\\\""; break;
      case '\\': out += "\\\\"; break;
      case '\n': out += "\\n"; break;
      case '\r': out += "\\r"; break;
      case '\t': out += "\\t"; break;
      default:
        if (static_cast<unsigned char>(c) < 0x20)
          out += std::format("\\u{:04x}", static_cast<unsigned>(c));
        else
          out += c;
    }
  }
  return out + "\"";
}

// Numbers are emitted at full round-trip precision so the Python side can
// apply its own 1e-9 relative tolerance (§10.4) instead of inheriting a
// formatting artefact from this writer.
std::string num(double d) { return std::format("{}", d); }

std::string dump(const tb::Value& v) {
  if (v.is_null()) return "null";
  if (v.is_bool()) return std::get<bool>(v.v) ? "true" : "false";
  if (v.is_num()) return num(v.num());
  if (v.is_str()) return quote(v.str());
  std::string out = "[";
  bool first = true;
  for (const auto& e : v.arr()) {
    if (!first) out += ",";
    first = false;
    out += dump(e);
  }
  return out + "]";
}

std::string dump(const tb::Object& o) {
  std::string out = "{";
  bool first = true;
  for (const auto& [k, v] : o) {
    if (!first) out += ",";
    first = false;
    out += quote(k) + ":" + dump(v);
  }
  return out + "}";
}

std::string dump(const tb::AnalysisResponse& a) {
  // analysis_to_dict() is to_db_columns() PLUS a nested `key_numbers` object:
  //
  //     cols = a.to_db_columns()
  //     cols["key_numbers"] = a.key_numbers.model_dump()
  //
  // The flat columns are what the rules engine gates on (tb::Object carries
  // only those, see DIFFS D4), but the nested copy is part of the stored shape
  // and therefore part of the parity contract, so it is re-attached on output.
  std::string out = dump(a.to_dict());
  const auto n = [](const std::optional<double>& d) {
    return d ? num(*d) : std::string("null");
  };
  const std::string nested =
      "\"key_numbers\":{\"deal_value_inr_crore\":" + n(a.key_numbers.deal_value_inr_crore) +
      ",\"stake_change_pct\":" + n(a.key_numbers.stake_change_pct) +
      ",\"dividend_per_share\":" + n(a.key_numbers.dividend_per_share) +
      ",\"buyback_value_inr_crore\":" + n(a.key_numbers.buyback_value_inr_crore) + "}";
  out.insert(out.size() - 1, (out.size() > 2 ? "," : "") + nested);
  return out;
}

void apply_settings(const json_t& j) {
  tb::Settings& s = tb::mutable_settings();
  const auto str = [&](const char* k, std::string& dst) {
    if (const json_t* v = at(j, k); v != nullptr && v->is_string()) dst = v->get<std::string>();
  };
  const auto dbl = [&](const char* k, double& dst) {
    if (const json_t* v = at(j, k); v != nullptr && v->is_number()) dst = v->get<double>();
  };
  str("MARKET_OPEN_IST", s.MARKET_OPEN_IST);
  str("MARKET_CLOSE_IST", s.MARKET_CLOSE_IST);
  str("ENTRY_WINDOW_START_IST", s.ENTRY_WINDOW_START_IST);
  str("ENTRY_WINDOW_END_IST", s.ENTRY_WINDOW_END_IST);
  str("SQUARE_OFF_TIME_IST", s.SQUARE_OFF_TIME_IST);
  if (const json_t* v = at(j, "ENFORCE_MARKET_HOURS"); v != nullptr && v->is_boolean())
    s.ENFORCE_MARKET_HOURS = v->get<bool>();
  dbl("MIN_SENTIMENT_CONFIDENCE", s.MIN_SENTIMENT_CONFIDENCE);
  dbl("PORTFOLIO_VALUE", s.PORTFOLIO_VALUE);
  dbl("MAX_SINGLE_POSITION_PCT", s.MAX_SINGLE_POSITION_PCT);
}

}  // namespace

// TB_BENCH=N re-runs the fast-track decision N times over the same case and
// reports the distribution instead of a decision. §9 PHASE 2 exists so that
// every performance claim in this migration is measured on the target box
// rather than asserted; this is the smallest thing that does that for Phase 5.
int bench(const std::string& headline, const std::string& extracted, long iters) {
  tb::Histogram h;
  volatile double sink = 0;
  for (long i = 0; i < iters; ++i) {
    const auto t0 = std::chrono::steady_clock::now();
    auto m = tb::evaluate_fast_track(headline);
    if (!m && !extracted.empty()) m = tb::evaluate_fast_track_text(headline, extracted);
    const auto t1 = std::chrono::steady_clock::now();
    if (m) sink = sink + m->response.confidence;
    h.record(static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count()));
  }
  std::cout << std::format(
      "{{\"iters\":{},\"ns_p50\":{},\"ns_p99\":{},\"ns_p999\":{},\"ns_max\":{},\"ns_mean\":{:.0f}}}\n",
      h.count(), h.percentile(0.5), h.percentile(0.99), h.percentile(0.999), h.max(), h.mean());
  return 0;
}

int main() {
  const std::string input{std::istreambuf_iterator<char>(std::cin), std::istreambuf_iterator<char>()};
  json_t root;
  if (const auto ec = glz::read_json(root, input)) {
    std::cout << R"({"error":"unparseable case json"})" << "\n";
    return 2;
  }

  if (const json_t* s = at(root, "settings"); s != nullptr) apply_settings(*s);

  const auto get_str = [&](const char* k) -> std::string {
    const json_t* v = at(root, k);
    return v != nullptr && v->is_string() ? v->get<std::string>() : std::string{};
  };
  const std::string headline = get_str("headline");
  const std::string extracted = get_str("extracted_text");

  if (const char* n = std::getenv("TB_BENCH"); n != nullptr && std::atol(n) > 0)
    return bench(headline, extracted, std::atol(n));

  std::string out = "{";

  // -- fast track -----------------------------------------------------------
  auto ft = tb::evaluate_fast_track(headline);
  if (!ft && !extracted.empty()) ft = tb::evaluate_fast_track_text(headline, extracted);
  out += "\"fast_track\":";
  out += ft ? "{\"pattern\":" + quote(ft->pattern) + ",\"response\":" + dump(ft->response) + "}"
            : "null";

  // -- analysis: the fast track's own, or the recorded LLM reply ------------
  std::optional<tb::AnalysisResponse> analysis;
  std::string analysis_error;
  if (ft) {
    analysis = ft->response;
  } else if (const json_t* llm = at(root, "llm_response"); llm != nullptr && llm->is_object()) {
    tb::Object raw;
    for (const auto& [k, v] : llm->get_object())
      if (k != "key_numbers") raw[k] = to_value(v);
    flatten_key_numbers(*llm, raw);
    auto r = tb::validate_analysis(raw);
    if (r)
      analysis = *r;
    else
      analysis_error = r.error().what;
  }
  out += ",\"analysis\":";
  if (analysis)
    out += dump(*analysis);
  else if (!analysis_error.empty())
    out += "{\"error\":" + quote(analysis_error) + "}";
  else
    out += "null";

  // -- rules ----------------------------------------------------------------
  tb::Object analysis_dict;
  if (analysis) analysis_dict = analysis->to_dict();
  // Market-context fields the operator's rules may gate on are supplied by the
  // case, mirroring enrich_analysis_context() on the Python side.
  if (const json_t* ctx = at(root, "context"); ctx != nullptr && ctx->is_object())
    for (const auto& [k, v] : ctx->get_object())
      if (analysis_dict.find(k) == analysis_dict.end()) analysis_dict[k] = to_value(v);

  const json_t* rules_json = at(root, "rules");
  const auto match = tb::evaluate(analysis_dict, rules_json != nullptr ? to_rules(*rules_json)
                                                                       : std::vector<tb::Rule>{});
  out += ",\"rule\":{\"rule_id\":";
  out += match.rule_id ? std::to_string(*match.rule_id) : "null";
  out += ",\"action\":" + quote(match.action);
  out += ",\"action_params\":" + dump(match.action_params);
  out += ",\"rationale\":" + quote(match.rationale) + "}";

  // -- market clock ---------------------------------------------------------
  if (const json_t* now = at(root, "now_epoch"); now != nullptr && now->is_number()) {
    const tb::SysTime t{std::chrono::seconds{static_cast<long long>(now->get<double>())}};
    const auto reason = tb::entry_block_reason(t);
    out += ",\"entry_block_reason\":";
    out += reason ? quote(*reason) : "null";
    out += ",\"is_market_open\":";
    out += tb::is_market_open(t) ? "true" : "false";
    out += ",\"square_off_due\":";
    out += tb::square_off_due(t) ? "true" : "false";
  }

  out += "}";
  std::cout << out << "\n";
  return 0;
}
