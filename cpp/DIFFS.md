# DIFFS — approved differences between the Python and C++ stacks

Required by c++.text §10.1: *"the C++ system must produce the same decision as
the Python system, **or the difference must be explained and approved in
writing**."*

Nothing lands here because it was inconvenient to fix. Each entry says what
differs, why it is acceptable, and what would make it stop being acceptable.

---

## D1 — ASCII whitespace instead of Unicode whitespace

**Where:** `tb::strip`, `tb::blank`, `tb::collapse_ws` (`cpp/include/tb/str.hpp`)

Python's `str.strip()` / `str.split()` split on every Unicode whitespace code
point (U+00A0, U+2028, the U+2000 quad family…). The C++ splits on the six
ASCII ones.

**Why approved:** checked against all 28,381 rows of `announcements.headline` —
zero contain a non-ASCII whitespace character. The exchange feeds emit ASCII.

**Stops being acceptable when:** a filing arrives with a non-breaking space
inside a value phrase (`Rs 450 crore`). The Python would find the value and
the C++ would not — a *missed* fast track, so it fails toward the LLM path
rather than toward a wrong trade. Re-check with the corpus sweep before Phase 8
ports PDF extraction, where non-ASCII whitespace is far more likely.

---

## D2 — ASCII case folding instead of Unicode case folding

**Where:** `tb::lower`, `tb::upper` (`cpp/include/tb/str.hpp`)

Python's `str.lower()` is full Unicode and can change a string's **length**
(U+0130 `İ` lowercases to two code points). The C++ folds ASCII only, which is
length-preserving by construction.

**Why approved:** this is a deliberate *improvement*, not just a shortcut. The
Python's `_order_value_near_context` computes match offsets on `normalized.lower()`
and then slices `normalized` with them — which is only correct while lowering
preserves length. On a filing containing `İ`, the Python's own window would
shift. Every token being matched (`crore`, `order`, `resign`, the role names) is
ASCII, so ASCII folding matches on exactly the same inputs without the offset
hazard.

**Stops being acceptable when:** a pattern gains a non-ASCII letter that needs
case-insensitive matching. None do today.

---

## D3 — `\d` and `\b` are ASCII in RE2, Unicode-aware in Python

**Where:** `inr_value_re()`, `order_context_re()` (`cpp/src/fast_track.cpp`)

Python `re` on `str` patterns treats `\d` as any Unicode decimal digit
(Devanagari `०१२…` match, and `float('०')` even parses) and `\b` as a
Unicode-aware boundary. RE2 is ASCII for both.

**Why approved:** an amount written in Devanagari digits inside an exchange
filing is not a case the fast track should be auto-trading on. ASCII-only is the
more conservative reading, and the failure direction is a missed fast track →
the LLM decides.

**Stops being acceptable when:** never, realistically — but if it changes,
the fix is `(?i)` + an explicit character class, not switching regex engines.

---

## D4 — `key_numbers` is flattened, not nested

**Where:** `tb::validate_analysis` (`cpp/src/schemas.cpp`), `flatten_key_numbers`
(`cpp/tools/replay_cpp.cpp`)

`tb::Value` models string / number / bool / null / array — the shapes the rules
engine actually compares — but not a nested object. The four `key_numbers`
fields are therefore flattened to `kn.*` keys before validation.

**Why approved:** no behaviour depends on the nesting. `analysis_to_dict()` on
the Python side already flattens `key_numbers` onto the top level for exactly
the same reason: that is the shape a rule's `field=` name resolves against. The
nested object is reconstructed on output by `AnalysisResponse::to_dict()`.

**Stops being acceptable when:** a rule needs to gate on a nested path. The
rules engine's `SUPPORTED_FIELDS` is flat by design, so this would be a spec
change, not a port bug.

**Note on the OUTPUT shape:** `analysis_to_dict()` emits the flat columns *and*
a nested `key_numbers` object. The flat internal representation above is an
implementation detail; `replay_cpp` re-attaches the nested copy so the emitted
JSON matches the Python's byte-for-byte. This was caught by the first parity run
against the live corpus — it failed all 200 smoke cases — not by review.

---

## D5 — `in` / `not_in` with a string value raises instead of iterating characters

**Where:** `apply_op` (`cpp/src/rules_engine.cpp`)

Python's guard is `hasattr(value, "__iter__")`, and a *string* satisfies it — so
`{"op": "in", "value": "ORDER_WIN"}` iterates the characters of that string and
compares each one. The C++ requires an array and raises `RuleError` otherwise.

**Why approved:** both outcomes are "this rule does not match". Python compares
against 9 single characters and fails; C++ logs `rules_engine.rule_malformed`
and skips the rule. The *decision* is identical; only the log line differs, and
the C++ one is the more useful of the two.

**Stops being acceptable when:** a rule is written that genuinely wants
per-character matching. That is not a thing anyone wants.

---

## D6 — the backfilled corpus cannot exercise the schema validators

**Where:** `scripts/build_corpus.py`

§10.2 wants `llm_response.json` to be DeepSeek's recorded reply. That reply is
**not stored anywhere**. `analyses.raw_response` keeps only `event_type`,
`summary`, `key_numbers`, `model`, `tokens`, `latency_ms`, `cost_usd`; the
sentiment, score, confidence, recommendation and rationale live in dedicated
`analyses` columns — all of them **post-validation**.

So the corpus is a *reconstruction*, and replaying it exercises the rules engine
but **not** the coercion fixes (`NEUTRAL`→`HOLD`, the `key_numbers` list/null
shapes, the 0..1 sentiment rescale). Those already ran before the values were
written.

Second-order consequence: a stored `sentiment_score` whose magnitude is ≤ 1 gets
rescaled a **second** time by `_normalise_score` on replay. Both stacks do it
identically, so parity stays green — but the replayed number is not the original.

**Why approved (for now):** the past cannot be recovered, and this still gives
1,889 replayable rules-engine cases today.

**Stops being acceptable at Phase 8**, which ports the analyzer. Before then,
Phase 0's forward instrumentation must persist the raw reply bytes — otherwise
the analyzer port has no reference to be verified against. Tracked in
`cpp/MIGRATION.md`.

---

## D7 — `_order_value_near_context` keeps the windowed algorithm

**Where:** `order_value_near_context` (`cpp/src/fast_track.cpp`)

Not a difference — a **rejected** one, recorded so it does not get re-proposed.

§9 PHASE 5 says to hand this function to Hyperscan for a 10–50× win. A
two-linear-pass rewrite (scan all order mentions, scan all INR values, join by
position) was written, proved exactly equivalent against 20,000 fuzz cases and
all 28,381 real headlines (`scripts/verify_single_pass.py`) — and then thrown
away, because profiling the Python on synthetic 11.6k-char filings showed it was
**slower**:

| stage                             | time    |
|-----------------------------------|---------|
| `ORDER_CONTEXT` scan, whole text  | 1664 µs |
| INR scan, whole text (one-pass)   |  813 µs |
| INR scan, 12 windows only         |  264 µs |
| normalize + lower                 |  289 µs |

The ±400-char window is a **pruning step** that keeps the expensive INR pattern
away from ~97% of the document. Replacing it with a full-text scan made the
function ~14% slower.

**What this means for the plan:** §9 PHASE 5 points at the right function but the
wrong pattern. 68% of the cost is the 19-alternative `_ORDER_CONTEXT_RE` scan,
which *both* algorithms pay in full — that is the pattern worth handing to
Hyperscan, and it is a genuine multi-pattern scan, exactly what Hyperscan is for.
Deferred to Phase 12 (the optimisation pass), where §12's rule applies: anything
that does not measurably help gets reverted.

---

## D8 — the `crorex` match is preserved, not fixed

**Where:** `inr_value_re()` (`cpp/src/fast_track.cpp`), pinned in
`cpp/tests/test_fast_track.cpp`

Only `cr`, `mn` and `bn` carry a `\b` in the unit alternation, so `crores?`
matches inside `crorex` — `"Rs 450 crorex"` parses as 450 crore in **both**
stacks.

**Why approved:** it is the Python's behaviour, and parity is the contract. It
is pinned by a test specifically so that a well-meaning "tidy-up" adding a
trailing `\b` shows up as a deliberate behaviour change with a parity diff,
rather than as a silent one on the money path.

**Stops being acceptable when:** the operator decides it is a bug. Then it gets
fixed in the **Python first**, and the C++ follows — never the other way round,
or the parity harness stops meaning anything.

---

## D9 — landmine: RE2 submatch count must equal the pattern's capture-group count

**Where:** `order_context_re()` (`cpp/src/fast_track.cpp`)

Not a Python/C++ difference — a **silent failure mode** recorded so nobody
reintroduces it.

`RE2::FindAndConsume(&input, re, &m)` matches **nothing** — returns false
immediately, no error, no exception, no compiler warning — when the pattern has
fewer capture groups than the submatch arguments passed. Writing the
order-context alternation as `(?:…)` instead of `(…)` therefore turns
`order_value_near_context()` into a function that unconditionally returns
`nullopt`: the hybrid PDF fast track would never fire, and nothing anywhere
would say so.

This shipped in the first version of the port. It was caught by the **first
compile-and-run on the server**, by the one unit test that exercised the hybrid
path — not by review, and not by anything static.

**Guard:** `cpp/tests/test_fast_track.cpp` asserts a non-null hybrid match and
three window cases. Keep them. Any new `FindAndConsume` call gets a test that
asserts a *positive* match, because the failure mode is silence.
