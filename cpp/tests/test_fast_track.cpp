// Pinned against the Python's behaviour. Every expected value here is the
// PYTHON's recorded answer, printed by scripts/gen_fast_track_cases.py -- not
// read off the source. Two of them contradicted a careful reading of the code
// (the 450-crore confidence tier, and "crorex" matching), which is exactly why
// they are recorded rather than reasoned about.
#include <cassert>
#include <cstdio>
#include <string>

#include "tb/fast_track.hpp"

using namespace tb;

static void near(double a, double b) { assert(a > b - 1e-9 && a < b + 1e-9); }

int main() {
  // -- parse_inr_crore: units, separators, and "largest wins" ---------------
  near(*parse_inr_crore("Rs. 1,234.56 crore"), 1234.56);
  near(*parse_inr_crore("₹450 cr"), 450.0);
  near(*parse_inr_crore("INR 89.5 crores"), 89.5);
  near(*parse_inr_crore("Rs 20 lakh"), 0.20);
  near(*parse_inr_crore("Rs 1.2 billion"), 120.0);
  near(*parse_inr_crore("Rs 50 million"), 5.0);
  // Largest wins: filings list components next to the total.
  near(*parse_inr_crore("orders of Rs 120 crore and Rs 330 crore, aggregating to Rs 450 crore"),
       450.0);
  assert(!parse_inr_crore("USD 500 million"));      // INR only
  assert(!parse_inr_crore("Rs 450"));               // no unit
  assert(!parse_inr_crore("450 crore"));            // no currency token
  assert(!parse_inr_crore("Rs , crore"));           // bare comma -> ValueError
  // NOT a typo: only `cr`/`mn`/`bn` carry a \b, so `crores?` happily matches
  // inside "crorex". Python does this too -- pinned so a "tidy-up" of the
  // pattern that adds a trailing \b shows up as a parity diff, not a silent
  // behaviour change on the money path.
  near(*parse_inr_crore("Rs 450 crorex"), 450.0);
  near(*parse_inr_crore("Rs 0.5 crore"), 0.5);

  // -- order win ------------------------------------------------------------
  {
    auto m = evaluate_fast_track("ACME bags order worth Rs 450 crore from NHAI");
    assert(m && m->pattern == "order_win_value");
    assert(m->response.event_type == "ORDER_WIN");
    assert(m->response.recommendation == "BUY");
    near(m->response.confidence, 0.80);  // 100 <= 450 < 500
    near(m->response.sentiment_score, 70.0);
    assert(m->response.key_numbers.deal_value_inr_crore.has_value());
    near(*m->response.key_numbers.deal_value_inr_crore, 450.0);
  }
  {
    auto m = evaluate_fast_track("ACME wins order worth Rs 600 crore");
    assert(m);
    near(m->response.confidence, 0.88);
    near(m->response.sentiment_score, 80.0);
  }
  {
    auto m = evaluate_fast_track("ACME wins order worth Rs 100 crore");
    assert(m);
    near(m->response.confidence, 0.80);
  }
  // Below the 25 crore floor -> LLM track.
  assert(!evaluate_fast_track("ACME bags order worth Rs 24 crore"));
  // Negative guards: the order is going away, or is still hypothetical.
  assert(!evaluate_fast_track("ACME order worth Rs 450 crore cancelled"));
  assert(!evaluate_fast_track("ACME submits bid for order worth Rs 450 crore"));
  assert(!evaluate_fast_track("ACME order worth Rs 450 crore terminated"));
  // Word boundary: "won" must not match inside "wonder".
  assert(!evaluate_fast_track("ACME wonders about Rs 450 crore"));

  // -- buyback --------------------------------------------------------------
  {
    auto m = evaluate_fast_track("Board to consider buyback of Rs 900 crore");
    assert(m && m->pattern == "buyback_value");
    assert(m->response.event_type == "BUYBACK");
    near(m->response.confidence, 0.78);
    assert(m->response.key_numbers.buyback_value_inr_crore.has_value());
    near(*m->response.key_numbers.buyback_value_inr_crore, 900.0);
  }
  assert(!evaluate_fast_track("Buyback of Rs 900 crore completed"));  // no approval word

  // -- KMP resignation ------------------------------------------------------
  {
    auto m = evaluate_fast_track("Resignation of Managing Director");
    assert(m && m->pattern == "kmp_resignation");
    assert(m->response.recommendation == "SELL");
    near(m->response.sentiment_score, -65.0);
    assert(m->response.event_type == "OTHER");
  }
  // Routine succession and independent-director churn go to the LLM.
  assert(!evaluate_fast_track("Resignation and appointment of Managing Director"));
  assert(!evaluate_fast_track("Resignation of Independent Director"));
  assert(!evaluate_fast_track("Resignation of Company Secretary"));

  // -- empty / blank --------------------------------------------------------
  assert(!evaluate_fast_track(""));
  assert(!evaluate_fast_track("   "));

  // -- hybrid: order context in the headline, value in the filing -----------
  assert(is_hybrid_order_candidate("Company informs regarding bagging of order"));
  assert(!is_hybrid_order_candidate("Company bags order worth Rs 450 crore"));  // has value
  assert(!is_hybrid_order_candidate("Board meeting intimation"));              // no context
  {
    auto m = evaluate_fast_track_text(
        "Company informs the Exchange regarding Bagging of order",
        "The Company has received a Letter of Award for a work order valued at "
        "Rs 750 crore from the client.");
    assert(m && m->pattern == "order_win_pdf_value");
    near(m->response.confidence, 0.85);
    near(*m->response.key_numbers.deal_value_inr_crore, 750.0);
  }
  // A cancellation ANYWHERE in the filing kills it.
  assert(!evaluate_fast_track_text("Company informs regarding bagging of order",
                                   "Order of Rs 750 crore. A prior contract was terminated."));

  // -- the window is what makes this not a plain "largest value" scan -------
  {
    // A results table far from any order mention must NOT be picked up.
    std::string doc = "Bagged an order. ";
    doc += std::string(600, 'x');
    doc += " total income Rs 5,000 crore";
    auto v = order_value_near_context(doc);
    assert(!v);  // 5,000 cr is outside the +250 window
  }
  {
    std::string doc = "The company bagged a work order valued at Rs 450 crore.";
    near(*order_value_near_context(doc), 450.0);
  }
  {
    // Multi-byte characters must not shift the window: the rupee sign is 3
    // bytes but ONE code point, so byte arithmetic would move the boundary.
    std::string doc = "order ";
    doc += std::string(240, 'y');
    doc += " ₹ 300 crore";  // ends just inside +250 code points
    auto v = order_value_near_context(doc);
    assert(v && *v > 299.0);
  }

  std::puts("test_fast_track OK");
  return 0;
}
