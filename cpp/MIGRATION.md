# Migration ledger

Live status of the Python → C++23 migration described in `c++.text`. Update
this file as phases complete; §4.3 requires table ownership to be documented
here and kept current.

**Nothing in this branch touches production.** The C++ owns no responsibility,
places no orders, and writes to no table. Every phase below is either done,
partially done with the gap named, or not started.

**Verified on the live Lightsail box (13.200.57.14), 2026-08-23, market closed
(Sunday IST):** toolchain installed, everything compiled with GCC 14.2, all 5
unit tests pass, and the parity harness is **GREEN over 28,226 cases** built
from the live 74,419-row DB. The trading service was never restarted and its
`/health` still answers 200.

---

## Phase status

| # | Phase | Status | Note |
|---|-------|--------|------|
| 0 | Baseline + golden corpus | **exit criterion met** | 28,226 replayable cases from the live DB (§9 PHASE 0 wants ≥ 5,000). Forward instrumentation is written but **not deployed**; the Fyers tick recording is **not** done — see *Phase 0's outstanding debt*. |
| 1 | Toolchain + skeleton | **EXIT CRITERION MET** | `curl :8001/health` returns ok on the server with both stacks running. cpp-httplib, not Drogon — see *The Drogon detour*. RSS 8.5 MB. |
| 2 | Measurement infrastructure | **partial** | `tb::Histogram` + a live `/metrics` endpoint; `scripts/bench_fast_track.py` gives real numbers on the target box. TSC calibration, the lock-free telemetry ring and the `perf` recipes are **not** written — there is no hot path to measure yet, so building them now would be scaffolding. |
| 3 | Config, logging, DB layer | **not started** | Only the seven Settings keys the Phase 5 leaves read are ported (`cpp/include/tb/config.hpp`), deliberately. |
| 4 | Hot-path primitives | **not started** | Consumed only by Phase 9. §9 PHASE 4 already advises taking a proven SPSC queue rather than hand-rolling one — do that when Phase 9 needs it, not before. |
| 5 | Pure logic leaves | **Parity met; 2 files unported** | Zero diffs over 28,226 live-DB cases, 6/6 unit tests, ASan/UBSan clean on real data. 9.0× faster on target hardware. `perf_sizer.py` and `symbols.py` are **not** pure leaves and were misfiled by the plan — see below. |
| 13 | Native Fyers data socket | **de-risked, not started** | §7.6's protobuf escape hatch is proven end-to-end (schema → protoc → compiling C++ that round-trips a tick). Entitlement still unverified — needs a login. |
| 6–12, 14 | — | **not started** | Each needs the live server, the live DB, Fyers credentials, or a recorded tick day. |

### What Phase 5 actually ported

| Python | lines | C++ | notes |
|--------|-------|-----|-------|
| `analyzer/fast_track.py` | 358 | `src/fast_track.cpp` | RE2. Windowed algorithm kept — see DIFFS D7. |
| `analyzer/schemas.py` | 252 | `src/schemas.cpp` | All coercion fixes preserved (D4, D6). |
| `analyzer/rules_engine.py` | 479 | `src/rules_engine.cpp` | Evaluation half only; the loader is Phase 3. |
| `risk/event_profiles.py` | 129 | `src/event_profiles.cpp` | constexpr table, as specified. |
| `risk/market_clock.py` | 125 | `src/market_clock.cpp` | Fixed +5:30, no tzdb. |
| `risk/position_sizer.py` | 193 | `include/tb/sizing.hpp` | **Only `notional_cap_qty`.** The rest reads the DB — Phase 3/9. Deliberately not stubbed: a stub on the sizing path sizes real orders wrong. |
| `risk/volatility.py` | 317 | `include/tb/volatility.hpp` | Pure half: `compute_atr`, `stop_distance`, `vix_risk_multiplier`. The Fyers candle/VIX providers wrap a live feed — Phase 9. |
| `risk/perf_sizer.py` | 140 | — | **Not portable in Phase 5.** It imports SQLAlchemy and queries Analysis/Announcement/Signal/Trade, so it is not a pure leaf at all — the plan misfiled it. Belongs to Phase 3/9. |
| `execution/symbols.py` | 80 | — | **Not portable in Phase 5** either: it calls the instrument master. §9's compile-time perfect hash needs a fixed symbol set, which this does not have. |

---

## Table ownership (§4.3)

One writer per table. The seam is the shared SQLite file in WAL mode.

| Table | Writer | Reader |
|-------|--------|--------|
| *all 18* | **Python** | Python |

The C++ neither reads nor writes any table today. `scripts/build_corpus.py`
opens the DB **read-only** (`mode=ro`) and is a developer tool, not part of
either stack.

When this changes: use `BEGIN IMMEDIATE` for write transactions, set
`busy_timeout = 30000` in both stacks, and never hold a write transaction
across a network call. (The Python already violates the last one in places —
§4.3 says fix it during the port, do not replicate it.)

---

## Phase 0's outstanding debt

The backfill covers what the DB already holds. These gaps are **measured, not
assumed**, and two of them cannot be closed retroactively:

1. **Extracted PDF text is never persisted.** `announcements.body` is NULL for
   all 74,419 rows on the live server. So the corpus exercises the headline path only — the hybrid
   PDF fast track (`evaluate_fast_track_text`) and all of Phase 8 have no
   reference data. Fix forward: dump extracted text at analysis time.
2. **The raw DeepSeek reply is never persisted.** See DIFFS D6. Without it the
   schema validators cannot be verified, which blocks Phase 8's exit criterion.
   **Written, not deployed:** `CORPUS_CAPTURE_ENABLED` (default OFF) now stores
   the raw reply and the extracted text in `analyses.raw_response`. It needs a
   deploy AND the operator to turn it on — until then items 1 and 2 stay open.
3. **No recorded Fyers tick day.** §9 PHASE 0 is explicit: *"You cannot capture
   the past. Without this, Phase 13 is blocked."* Still true.
4. **The `tbt_ws` question is HALF ANSWERED — see the section below.** The
   technical half is done and the answer is yes; the entitlement half needs a
   live Fyers login.

---

## §7.6 — the tbt_ws escape hatch: technical half PROVEN

§7.6 calls this "the one-day task with the highest leverage in the plan… One
day of checking can save three months of work and the largest risk in the
project." The offline half is now done, on the server, end to end:

1. **Schema recovered.** `scripts/recover_tbt_proto.py` pulls the
   `FileDescriptorProto` out of the `msg_pb2` shipped inside `fyers-apiv3` and
   reconstructs `cpp/proto/msg.proto`. The 9 messages match §7.6's documented
   shape exactly — `SocketMessage`, `MarketFeed`, `Quote(ltp, ltt, ltq, vtt,
   vtt_diff, oi, ltpc)`, `ExtendedQuote`, `DailyQuote`, `OHLCV`, `Depth`,
   `MarketLevel`, `SymDetail`. Every scalar is a `google.protobuf` wrapper
   type, so the file needs `import "google/protobuf/wrappers.proto"`.
2. **`protoc --cpp_out=.` generates clean bindings** (msg.pb.h/.cc).
3. **They compile with GCC 14 and round-trip a realistic tick** — LTP plus a
   depth level, serialise and parse back, values intact.

**What this means:** if the account is entitled, §9 PHASE 13 is *generated*
protobuf decoding, not a hand-written stateful HSM binary decoder — no delta
accumulation, no positional field ordering, no scale-factor guessing. That is
the difference between the 8-week and 16-week branch, and it removes the
single largest technical risk in the plan.

**What is still open (needs a live session, not engineering):** §7.6 items 1–3
— is the account entitled, does the feed carry LTP for NSE *cash equities*
rather than derivatives-only depth, and how does its latency compare.
`scripts/probe_tbt_entitlement.py` answers all three and prints a verdict; it
needs a valid daily Fyers token and must run **during market hours** (it says
so rather than reporting a false negative on a closed market).

`cpp/proto/` is checked in but wired into nothing — no protobuf dependency is
added to the build until Phase 13 actually needs it.

---

## The Drogon detour (§9 PHASE 1's `/health`)

**Resolved: `/health` ships on cpp-httplib. Drogon returns at Phase 6.**

Ubuntu 24.04 packages Drogon 1.8.7, but its shipped `DrogonConfig.cmake` calls
`find_dependency` unconditionally for jsoncpp, **PostgreSQL**, MySQL, sqlite3,
brotli and hiredis. Satisfying it meant installing `libpq-dev`,
`libmysqlclient-dev`, `libhiredis-dev` and friends onto a live trading server —
and after all of them, `FindMySQL.cmake` still fails, because it searches
hardcoded paths that do not match Ubuntu's layout.

At that point the cost/benefit is not close for a process that owns nothing.
The answer was to stop climbing: **cpp-httplib** is one header (Ubuntu ships it
split, so link `libcpp-httplib.so` via pkg-config), no dependency chain, and it
serves the two routes Phase 1 needs. `src/main.cpp` is ~100 lines and is not
what will make Phase 6 hard.

When §9 PHASE 6 actually needs 89 routes and a WebSocket, revisit Drogon then —
vcpkg's port, or a `MySQL_DIR` shim. Do not pay that cost 28 weeks early.

**Cleanup available.** These dev packages were installed on the server chasing
Drogon and nothing uses them now. They are inert headers plus one shared library —
no daemon runs — so they were left in place rather than risking `apt remove` on a
live box. To remove them during a maintenance window:

```bash
sudo apt-get remove --purge libdrogon-dev libpq-dev libmysqlclient-dev libhiredis-dev libbrotli-dev
```

Still needed, do NOT remove: `g++-14 cmake ninja-build libre2-dev libspdlog-dev pkg-config libcpp-httplib-dev` (plus `clang-18 libclang-rt-18-dev protobuf-compiler libprotobuf-dev` for the sanitiser and §7.6 work).

---

## Findings that contradict the plan

- **§11.1 says Ubuntu 24.04 needs the `ubuntu-toolchain-r/test` PPA for GCC 14.**
  It does not — `g++-14` (14.2.0) is in the base noble repos. One less PPA on a
  production box.
- **Ubuntu's `libre2-dev` ships no CMake config**, only `re2.pc`. `CMakeLists.txt`
  falls back to `pkg_check_modules` so the server does not need vcpkg.
- **§9 PHASE 5's Hyperscan note points at the wrong pattern** — see DIFFS D7. The
  plain RE2 port already measures **9.0× faster** than the Python
  (1633 µs → 182 µs mean, 11.6k-char document, on the live Cascade Lake box).
  That is below the 10–50× the plan projected, and consistent with D7: what is
  left on the table is the order-context scan, not the INR scan.

---

## How to run what exists

Build and unit-test (needs GCC 14, CMake ≥ 3.25, `libre2-dev libspdlog-dev pkg-config libcpp-httplib-dev`):

```bash
cmake -S cpp -B cpp/build -G Ninja -DCMAKE_BUILD_TYPE=RelWithDebInfo && cmake --build cpp/build && ctest --test-dir cpp/build --output-on-failure
```

Prove the fast-track rewrite is exact (pure Python, no toolchain needed):

```bash
PYTHONPATH=. TESTING=1 python scripts/verify_single_pass.py
```

Build the corpus from the live DB, then diff both stacks over it:

```bash
PYTHONPATH=. TESTING=1 python scripts/build_corpus.py --out corpus/ --limit 30000 --analysed-only
```

```bash
PYTHONPATH=. TESTING=1 python scripts/parity_check.py corpus/ --replay cpp/build/replay_cpp
```

Benchmark both stacks on the same text (this is where the 9.0× comes from):

```bash
PYTHONPATH=. TESTING=1 python scripts/bench_fast_track.py --replay cpp/build/replay_cpp
```

Regenerate the recorded answers the C++ unit tests assert against:

```bash
PYTHONPATH=. TESTING=1 python scripts/gen_fast_track_cases.py
```

---

## Abort criteria (§17) — current reading

`c++.text` §17 exists because §16 puts this at ~101–109 weeks full-time, or
roughly **8 years at 15 h/week**. Two of the five criteria are worth checking
early rather than late:

- **A1** — Phase 5 parity cannot reach zero diffs in 3 weeks. **Cleared.** Zero
  diffs over 28,226 cases on the first green run.
- **A2** — Phase 9 tick→dispatch p99 does not beat 200 µs. Not yet testable, and
  §17 notes the memory win alone (1.36 GB → <150 MB, which is the *live* OOM
  risk given 127 MB free) is achievable in Python for a fraction of the effort.

And the honest note from §17 has not changed: signal latency is 93% exchange lag
and 6% DeepSeek. This rewrite buys determinism on the exit path, a ~10× memory
reduction, and deep C++ expertise. It does not buy faster signals.

---

## Phase 0's remaining tooling (written, waiting on a session)

| Script | Answers | Needs |
|--------|---------|-------|
| `scripts/record_ticks.py` | §9 PHASE 0's "record a full trading day of raw WS frames" — writes `.raw` (bytes off the wire = the decoder's input) paired with `.jsonl` (the SDK's decoded output = expected), which is the only shape Phase 13 can be verified against | a valid daily token **and** market hours |
| `scripts/probe_tbt_entitlement.py` | §7.6 items 1–3 | same |
| `scripts/recover_tbt_proto.py` | §7.6's schema half | nothing — already run, output checked in |

The daily Fyers token expires; both live scripts stop with a clear message
rather than recording an empty file.

---

## Running services on the box

| Unit | Port | Owns | State |
|------|------|------|-------|
| `tradebot` (Python) | 8000 | **everything** | active |
| `tradebot-cpp` | 8001 | nothing | active, localhost-only, `MemoryMax=200M`, cgroup RSS ~1.6 MB |

`tradebot-cpp` serves `/health` and `/metrics` and is deliberately not routed
through Caddy. To remove it entirely:

```bash
sudo systemctl disable --now tradebot-cpp && sudo rm /etc/systemd/system/tradebot-cpp.service && sudo systemctl daemon-reload
```
