# market-proof

> **Every AI that reads a financial document reports a confidence score. Almost nobody checks whether that number means anything. We built the check — and then a model that survives it.**

<p align="left">
<img alt="Python 3.11" src="https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white">
<img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white">
<img alt="React 18" src="https://img.shields.io/badge/React-18-61DAFB?logo=react&logoColor=black">
<img alt="tests" src="https://img.shields.io/badge/tests-823%20backend%20%2B%2065%20frontend-success">
<img alt="model" src="https://img.shields.io/badge/%F0%9F%A4%97-tradebot--slm--v1-yellow">
<img alt="license" src="https://img.shields.io/badge/license-MIT-blue">
</p>

Material corporate news moves NSE/BSE stocks within seconds. This system reads
each new exchange filing, decides in seconds whether it is worth acting on, and
then — the part that matters — **records what the market actually did, for every
filing, including the ones it declined.** That declined set is the only way to
measure the mistakes an AI never tells you about.

Measured on **17,298 declined filings**, the LLM's confidence ran *backwards*:

| model confidence | filings that actually moved | n |
|---|---|---|
| 0.0 – 0.3 | 13.7% | 10,145 |
| 0.3 – 0.5 | 17.4% | 2,838 |
| 0.5 – 0.7 | 14.3% | 1,577 |
| 0.7 – 0.9 | 13.0% | 1,436 |
| **0.9 – 1.0** | **8.6%** | 1,366 |

It is *least* accurate where it is *most* certain. Any gate thresholded on that
score is a gate on noise — and thresholding on `confidence` is exactly how these
systems ship. So we trained a replacement supervised by **measured market
reaction** instead of by another LLM's opinion, and put both behind one switch.

---

## Contents

1. [The problem](#1-the-problem)
2. [What this is](#2-what-this-is)
3. [Architecture](#3-architecture)
4. [The analyzer — three tracks](#4-the-analyzer--three-tracks)
5. [The rules engine](#5-the-rules-engine)
6. [The risk engine](#6-the-risk-engine)
7. [Execution — the entry state machine](#7-execution--the-entry-state-machine)
8. [Exit management](#8-exit-management)
9. [The measurement layer](#9-the-measurement-layer--the-actual-contribution)
10. [`tradebot-slm-v1`](#10-tradebot-slm-v1--our-own-model)
11. [Running the SLM yourself](#11-running-the-slm-yourself)
12. [Quickstart](#12-quickstart)
13. [Configuration](#13-configuration)
14. [Frontend](#14-frontend)
15. [Testing](#15-testing)
16. [Deployment](#16-deployment)
17. [Engineering war stories](#17-engineering-war-stories)
18. [What does not work](#18-what-does-not-work-honestly)
19. [Project structure](#19-project-structure)
20. [Team, licence, disclaimer](#20-team-licence-disclaimer)

---

## 1. The problem

An Indian listed company files a disclosure with NSE or BSE. Within seconds,
the stock moves. Retail traders reacting to a Telegram forward enter halfway
through the move; the alpha is gone in minutes.

Automating the "first read" is the obvious idea. The non-obvious part is that
**you cannot tell whether your automation works.** A trading system logs the
trades it takes. It does not log the trades it *declined* — and those are where
the errors live. If the AI says HOLD on a filing that then moves 6%, nothing
anywhere records that this happened.

So this project does three things, in order of how much we think they matter:

1. **Measures the decisions nobody measures.** Every analyzed filing — taken or
   declined — gets its forward price reaction recorded and joined back to the
   AI's verdict. That produced the confidence-inversion table above, and this:

   | event type the bot declined | actually moved ≥1.5% in 15 min | n |
   |---|---|---|
   | **Q1_RESULTS** | **56.7%** | 224 |
   | ANNUAL_RESULTS | 36.2% | 174 |
   | BOARD_MEETING | 23.9% | 159 |
   | ACQUISITION | 17.2% | 58 |
   | DIVIDEND | 14.0% | 214 |
   | OTHER | 13.0% | 16,474 |
   | *overall declined* | *14.6%* | *17,298* |

   The system was skipping the single category it should have been most
   aggressive on. **That finding was invisible for months behind a labelling
   bug** — see [§17](#17-engineering-war-stories).

2. **Trains a model on that measurement.** Not on sentiment labels. On what the
   price did. [§10](#10-tradebot-slm-v1--our-own-model).

3. **Wraps both in controls that cannot be bypassed**, because a system that
   acts on a language model's output needs the language model to be the least
   dangerous component. [§6](#6-the-risk-engine).

---

## 2. What this is

A local-first, **intraday** equity news-trading system for Indian markets
(NSE / BSE, cash MIS only), plus the measurement infrastructure and the
fine-tuned model described above.

| | |
|---|---|
| **Backend** | Python 3.11, FastAPI, SQLAlchemy 2, SQLite (WAL), structlog, httpx |
| **Realtime** | Fyers API v3 WebSockets (data + order sockets), in-memory async event bus |
| **AI** | DeepSeek **or** `tradebot-slm-v1` (Qwen2.5-1.5B, fully fine-tuned) — one toggle |
| **PDF** | PyMuPDF, table-aware extraction, non-Latin script filtering, OCR fallback |
| **Frontend** | React 18, TypeScript 5, Vite 6, TanStack Query, lightweight-charts |
| **Scale** | 26 API routers · 18 tables · 14 pages · 823 backend + 65 frontend tests |
| **Corpus** | 787,267 filings collected · 243,533 outcome-labelled · 98.0% text coverage |
| **Status** | Running 24/7 on AWS Lightsail (Mumbai) behind Caddy + basic auth |

**Design constraints that are never violated** (each is enforced in code and
covered by tests):

- **Latency is the product.** A late entry is worse than no entry. Never fix
  whipsaw by *delaying* entry — fix the stop, the sizing, the slippage cap.
- **Intraday only.** Every position is MIS. Time-exit and EOD square-off always
  flatten. There is no overnight path, deliberately.
- **Non-bypassable risk.** Every signal routes through the risk engine. There is
  no `force=True`, no `if is_test` branch, no override flag anywhere.
- **Never retry blind.** A broker call that times out *may* have placed the
  order. Entries are terminal on `BROKER_TIMEOUT` and are never re-fired.
- **Fail-safe defaults.** Missing market data makes a filter skip or block —
  never fake a pass. A cold price feed blocks the trade rather than filling at a
  synthetic price.
- **Non-destructive evolution.** Every new capability ships behind a toggle that
  defaults to the *existing* behaviour. Nothing new can silently change strategy.
- **Frontend-only control.** Every operational switch is in the browser. No file
  sentinels, no CLI gates. Going live requires typing `LIVE` into the UI.

---

## 3. Architecture

One FastAPI process. All internal messaging goes over an in-memory async event
bus — publish/subscribe, no external broker. Six logical tracks run as asyncio
tasks under the app lifespan.

```
[NSE / BSE filings]
      │  three racing monitors, anti-phase polling
      ▼
T2 Monitors ── SHA-256 dedupe + cross-source dedupe, PDF prefetch ──► announcements.new
      │
      ▼
T3 Analyzer   staleness gate → AI master switch → pre-LLM noise filter →
      │       ┌── FAST TRACK  (headline only, deterministic, ~0 ms)
      │       ├── HYBRID      (headline + PDF value parse, ~1 s, no LLM)
      │       └── LLM  ──  DeepSeek  ⇄  tradebot-slm-v1   ← one toggle
      │       → schema validate → rules engine → pipeline deadline
      ▼
   signals.new   (BUY / SELL / HOLD / BLOCK + entry / SL / target)
      │
      ▼
T4 Execution  circuit-breaker check → risk engine (R0–R14 + sizing) →
      │       Entry state machine:
      │       INITIALIZED → AWAITING_QUOTE → DRIFT_CHECK → ORDER_ROUTING →
      │       FILL_MONITORING → RECONCILING → PARTIAL_HANDLING → ACTIVE_POSITION
      ▼
 [Paper simulator]  or  [Fyers live]  ──► trade.executed
      │
      ▼
   Trade Manager   a tick listener per symbol on the Fyers WS — exit rules run
      │            on EVERY tick, not a poll. 0.5 s sweep is only a backstop.
      │            STOP / TARGET / BREAKEVEN / TRAIL / SCALE-OUT /
      │            CONSOLIDATION / STALL / TIME-EXIT / EOD square-off
      ▼
T5 Notifications + webhooks   │   T6 REST + WebSocket control plane (the UI)
      │
      ▼
  Outcome logger + Dataset builder  ── the measurement layer (§9)
```

### Detection: three sources racing

Detection lag (exchange publish → we see it) is **85–90% of end-to-end latency**
and is dominated by the exchange's own publish delay — measured p50 ≈ 30 s.
Polling faster cannot remove it. But a filing appears on *several* independent
channels with different lags, so detection = `min(lag of enabled sources)`:

| source | what it is | note |
|---|---|---|
| `NSE-API` | corporate-announcements API | authoritative symbols |
| `BSE-API` | BSE announcements API | **measured fastest at p50** — the opposite of folklore |
| `NSE-RSS` | `nsearchives` XML on a plain CDN | no Akamai, no cookies, ~250 ms; observed publishing *before* the API |

Monitors poll in **anti-phase** (BSE offset half an interval, RSS at ¾) so a
dual-listed filing surfaces within ~interval/2 instead of a full interval. The
RSS fetcher holds one persistent HTTP/2 keep-alive client and uses conditional
GET, so a steady-state poll is a header-only round-trip on a warm connection.

More sources can never mean a double trade: dedupe is two-layer — a
`SHA-256(exchange|symbol|filed_at|pdf_url)` content hash catches exact repeats,
and a **cross-source** rule collapses the same exchange+symbol+normalised
headline within 30 minutes into one row (RSS and API timestamps differ by ~1 s,
so the hash alone would miss it).

---

## 4. The analyzer — three tracks

Every filing enters the same funnel and exits as exactly one signal.

**0. Gates before any spend.** Idempotency (skip if already analysed) → AI
master switch → **staleness gate** (older than `MAX_NEWS_AGE_SECONDS` is
dropped; a startup sweep pre-marks the whole backlog so a restart never fires
the LLM on stale news) → **pre-LLM noise filter** (trading-window notices,
compliance certificates, newspaper-publication copies never reach a paid model).

**1. Fast track** — deterministic, no LLM, signal in milliseconds. Three tight,
high-conviction headline shapes only:
- `ORDER_WIN` with an explicit INR value; confidence scales with size.
  Handles ₹/Rs/INR × crore/cr/lakh/lac/million/mn/billion/bn → normalised to crore.
- KMP resignation (MD / CEO / CFO / whole-time director / chairman), guarded
  against routine succession ("resignation *and* appointment").
- `BUYBACK` with an explicit value.

**2. Hybrid** — an order-context headline with *no* value (the typical NSE
phrasing) triggers PDF extraction and a deterministic value parse near the order
mention. ~1 s, still no LLM. Cancellation wording anywhere in the document kills
the signal.

**3. LLM track** — everything else. Prompt templates live in the database, are
fully operator-editable with version history and one-click restore, and carry
their own model, temperature, token cap and inference controls.

**Extracted-text mode** (off by default) sends the real filing text instead of
metadata: first two pages plus keyword/digit-scored pages, non-Latin lines
dropped, ~24k-char budget, and a **no-trade-by-default footer** requiring any
BUY/SELL to cite a fact from the document. Any failure — scanned PDF,
encryption, timeout, missing wheel — falls back to the metadata prompt. *This
mode can degrade, but it can never block a signal.*

Off-spec model output is **coerced rather than discarded**: novel event
categories → `OTHER`, no-trade synonyms (`NEUTRAL`/`AVOID`) → `HOLD`,
`key_numbers` arriving as a list → empty object. We were losing ~130 analyses a
day to strict validation. Genuinely garbled values still fail loudly.

---

## 5. The rules engine

**There are no hard-coded trading triggers.** Startup does not seed rules. The
operator builds them in the UI, and a restart never resurrects a deleted rule.

Evaluation runs every enabled strategy's enabled rules together, ordered by
`(priority ASC, id ASC)`, **first match wins**; no match → `HOLD` (persisted for
observability, marked blocked so nothing trades).

Conditions are JSON with `all_of` (AND) and `any_of` (OR) groups of
`{field, op, value}`. Operators: `==`, `!=`, `in`, `not_in`, `>=`, `<=`, `>`,
`<`, `between`. String comparison is case-insensitive; a numeric operator on a
missing value is a **non-match** (fail-safe), and a malformed rule is skipped
with a warning rather than crashing the pipeline.

Fields available: `event_type`, `sentiment`, `sentiment_score`, `confidence`,
`recommendation`, `deal_value_inr_crore`, `stake_change_pct`,
`dividend_per_share`, `buyback_value_inr_crore`, plus live-enriched `sector`,
`price`, `change_pct`, `adv_crore`, `atr_pct`, `india_vix`, `spread_pct`.

Rules can be dry-run against a sample analysis before being enabled.

---

## 6. The risk engine

`RiskEngine.evaluate(signal) → RiskDecision{approved, violations[], sizing, context}`.
Every signal goes through it. There is no bypass.

**Sizing**

```
qty = floor( (equity × risk_pct / 100) / |entry − stop_loss| )
```

then clamped to the notional single-name cap — the *smaller* of risk-based and
notional-based quantity wins. `risk_pct` is tiered by the performance-weighted
sizer (an event type's realised win rate and average R over recent closed trades
promote or demote it), then reduced by a graduated new-account ramp, any active
throttle, and a fail-safe VIX multiplier. Notional caps use buying power
(`equity × INTRADAY_LEVERAGE`); **real-money limits always use raw equity —
leverage multiplies exposure, never the money at risk.**

**Hard gates** — each blocks the signal outright:

| gate | check |
|---|---|
| R1 | action is BUY/SELL (HOLD/BLOCK never trades) |
| R2 / R2b | confidence > 0, and ≥ `MIN_SENTIMENT_CONFIDENCE` |
| R0 | stop-loss present and > 0 — no SL means it cannot be sized safely |
| R0b | **direction-aware stop sanity**: stop == entry → invalid distance; BUY stop above entry, or SELL stop below entry → rejected (a wrong-side stop triggers instantly or never protects) |
| R3 | quantity ≥ 1 after rounding |
| R4 | trade risk ≤ `MAX_CAPITAL_RISK_PCT` of equity |
| R5 | daily realised loss < `DAILY_MAX_LOSS_PCT` |
| R6 | open positions < `MAX_CONCURRENT_POSITIONS` (adding to a held name is exempt) |
| R7 | single-name value ≤ `MAX_SINGLE_POSITION_PCT` of buying power |
| R8 | liquidity ≥ `MIN_LIQUIDITY_CRORE`; **unknown ADV blocks in live mode**, never silently passes |
| R9 | sector exposure ≤ `SECTOR_CONCENTRATION_PCT` |
| R10 | symbol not on the strategy blocklist |
| R11 | a SELL that *opens* a short requires `SHORTING_ENABLED` |
| R12 | bid/ask spread ≤ `MAX_SPREAD_PCT` |
| R13 | names per sector ≤ `MAX_POSITIONS_PER_SECTOR`, **and** a one-name-per-event window blocking a second same-sector entry within `SECTOR_CLUSTER_WINDOW_SECONDS` (correlated news cluster) |
| R14 | opt-in model gate — a low P(mover) score may veto; fails open on thin coverage |
| — | `NO_LIVE_PRICE` — a cold feed blocks rather than filling at a synthetic price |

**Circuit breakers** sit *above* the per-trade engine, backed by a singleton row
that survives restarts. A background monitor evaluates them every ~10 s:

| breaker | action |
|---|---|
| daily loss ≥ `DAILY_MAX_LOSS_PCT` | halt + flatten (clears next day) |
| monthly drawdown ≥ `MONTHLY_MAX_DRAWDOWN_PCT` | halt + flatten + **force paper mode** |
| weekly loss ≥ `WEEKLY_MAX_LOSS_PCT` | throttle risk % |
| consecutive losers ≥ `MAX_CONSECUTIVE_LOSERS` | cooldown pause; a high-conviction signal can resume early |
| trades today ≥ `MAX_TRADES_PER_DAY` | no new entries |
| manual kill | clears only via explicit resume |

---

## 7. Execution — the entry state machine

Every approved signal runs exactly **one** attempt through explicit states, with
off-ramps `RETRACEMENT_WATCH`, `EXPIRED`, `REJECTED`, `SYMBOL_LOCKED`,
`BROKER_TIMEOUT`, `SLIPPAGE_BREACH`, `FLASH_CRASH_EXIT`.

- **Symbol mutex** — a second signal for a symbol already routing waits briefly,
  then rejects. NSE and BSE publishing the same filing cannot double-enter.
- **Anti-chase drift gate** — if price has run beyond `ENTRY_MAX_DRIFT_PCT`
  *against* the analysis-time price, the entry is blocked. Favourable drift
  always passes. Blocked signals park in a passive **retracement watch** holding
  no lock: pull back into the band within the window and the signal re-arms
  **once** with its original anchor and deadline; otherwise it expires untraded.
  This is the one allowed brake, and it never chases.
- **IOC marketable-limit routing** at live price ± `ENTRY_BUFFER_PCT`, tagged
  with a `client_order_id` for cross-path deduplication.
- **Dual-confirmation fill watch** — the order-WebSocket event is the fast path;
  REST `/orders` is polled as a backstop. First confirmation wins; the DB layer
  dedupes by `broker_order_id`, and a filled row is never downgraded by a late
  cancel echo.
- **Partial fills** hand over the *exact* filled quantity with the original stop
  (rupee risk scales down with size); the remainder is cancelled. Zero fill →
  cancelled and expired, never retried.
- **Post-fill safety** — a fill *through* the limit triggers an immediate market
  flatten (`SLIPPAGE_BREACH`); if the market is already through the stop at fill
  time, likewise (`FLASH_CRASH_EXIT`). Both persist entry and exit and raise a
  critical risk event; nothing is handed to the trade manager.

---

## 8. Exit management

Exits evaluate on **every Fyers WebSocket tick** via a per-symbol tick listener —
the 0.5 s sweep is only a backstop for time-based exits and hydration. Per-symbol
locks make tick and sweep evaluation atomic, so a tick storm cannot double-exit.

Rule order is deliberate:

```
TIME_EXIT (runs even on a stale tick, settles at last real price)
  → freshness gate (no price-triggered exit on a tick older than STALE_QUOTE_SECONDS)
  → BREAKEVEN lock (tighten-only)
  → scale-out + trailing  (open-ended positions ONLY — an explicit target
                           always means a full exit; partial-exiting a targeted
                           trade would silently change the strategy's expectancy)
  → hard STOP checked BEFORE target   (a gap through both fires the protective exit)
  → CONSOLIDATION exit (profit 1–2.5% pinned in a tight range → free the capital)
  → STALL exit        (profit 3–6% with low rate-of-change → capture before reversion)
```

Live exits route **through the broker first** — marketable limit, then cancelled
and chased at market if unfilled. Protective exits must complete; scale-outs are
never chased. Rejections retry with backoff, then escalate once to a critical
alert — **and keep retrying on a cooldown. A position is never silently
abandoned.** A 60-second reconciliation loop compares the broker book with the
managed book: broker-flat → `CLOSED_EXTERNAL` settled locally with no order
placed; broker-reduced → local quantity resizes to the broker's truth.

---

## 9. The measurement layer — the actual contribution

This is the part that produced every number at the top of this README, and the
part most trading systems do not have.

**Outcome logger.** For *every* signal — approved **and blocked** — it records
the price at signal time, +5 min and +30 min, with a data-quality note.

**Dataset builder.** A periodic job enriches those rows with 1-minute-candle
reaction features and horizon targets. Critically, it also writes **shadow
rows**: every filing that was analysed but never produced a signal. Those are
the negatives — typically a 10–100× row multiplier — and they are what makes
"of the filings we declined, how many moved?" answerable at all.

Every column carries a **FEATURE / TARGET / META** badge in the UI. Minutes 1–5
are decision-time features; minutes 6–15 are look-ahead targets. A column-health
panel reports null rates, distributions, target correlation and explicit
**⚠ LEAK** warnings, because the easiest way to build a model that looks
brilliant is to accidentally feed it the future.

Exports are chronological train/val splits — **never random**. The same company
files dozens of times; a random split puts near-duplicates of training rows into
test and inflates everything.

The **HOLD calibration report** joins declined filings to their measured
outcomes and answers the question directly, bucketed by event type, confidence
band and sentiment, with an automatic verdict line — `tracks reality` /
`INVERTED` / `flat`. That verdict is what caught the confidence inversion.

**One caveat we keep attached to these numbers:** the *declined* side is large
and reliable (n=17,298). The *taken* side is thin — 97 trades. Selection looks
~2.2× better than random, but treat that ratio as early, not settled.

---

## 10. `tradebot-slm-v1` — our own model

A **Qwen2.5-1.5B, fully fine-tuned** (no LoRA) on 146,500 NSE filings. Targets
are not sentiment labels — they are the **measured, market-adjusted price move
in the 15 minutes after each filing was published**. One epoch, 2,290 steps,
~19 h on one A10G, **$33**. `eval_loss 0.4102`, eval below train throughout.

We could find no published model doing this: FinBERT, FinGPT and FinMA are all
trained on human- or LLM-assigned sentiment, never on measured market reaction.

### The result, stated the way we would want to read it

| same 600 unseen filings | tradebot-slm-v1 | DeepSeek | always-predict-FLAT |
|---|---|---|---|
| direction accuracy | 0.5733 | 0.5617 | **0.5950** |
| balanced accuracy | **0.3317** | 0.3236 | 0.3333 |
| mover ROC-AUC | **0.5071** | **0.4234** | 0.500 |

It beats DeepSeek on every metric — and DeepSeek's confidence is
*anti*-predictive at 0.4234, **below a coin flip**, independently reproducing the
production inversion on held-out data. But read the third column: **both models
lose to a constant.** They fail identically — DeepSeek says HOLD 94.0% of the
time, ours says FLAT 94.2%.

As a feature extractor it is no better than a bag of words. On the frozen test
split (n=17,723), a paired bootstrap over 2,000 resamples:

| comparison | Δ ROC-AUC | 95% CI | verdict |
|---|---|---|---|
| fine-tune + market vs market | +0.0108 | [+0.0074, +0.0145] | significant |
| TF-IDF + market vs market | +0.0136 | [+0.0095, +0.0177] | significant |
| **TF-IDF vs fine-tune** | +0.0028 | **[−0.0001, +0.0057]** | **spans 0 — indistinguishable** |

**A $33 fine-tune matched TF-IDF + SVD, which costs ten minutes of laptop CPU.**
Not beaten. Not lost. Matched. Text *does* carry signal beyond market state —
both clear the baseline with intervals well clear of zero — but this model is not
a better way to extract it than a bag of words.

Why, in hindsight: the SFT objective is next-token prediction over a JSON
schema. That optimises *format compliance* — eval loss fell 74% — and nothing in
it asks the hidden states to encode what makes a filing move.

**What it is genuinely good at is extraction.** On a live run it read a ₹412
crore railway order, classified `ORDER_WIN`, pulled the amount and computed
`amount_to_mcap = 0.175` — then predicted FLAT. The document understanding
works; the movement prediction reverts to the base rate. Both halves of that
sentence are the finding.

### Never quote raw accuracy on this task

Direction accuracy at ±3% reads **0.9638**. That is the base rate — 96.4% of
filings do not move 3%, and a model that says FLAT every single time scores
0.9643. Balanced accuracy is the honest column and it says **0.3333**: chance.
Direction is not learnable at ±1/2/3% on this data. `mover` is the only
learnable target.

Full numbers, vocabularies, training config, baselines and limitations:
**[model card](AIdataset/model/MODEL_CARD.md)**.

---

## 11. Running the SLM yourself

Weights live on **[huggingface.co/pravz/slm_v1](https://huggingface.co/pravz/slm_v1)**,
not in this repo — a 2.9 GB checkpoint has no business in git history.

```bash
# 1. A SEPARATE venv. The bot's own venv deliberately carries no torch —
#    it runs on a 2 GB server the kernel has OOM-killed before.
python -m venv .venv-slm
.venv-slm/bin/pip install torch transformers accelerate huggingface_hub

# 2. Pull the weights (~2.9 GB)
.venv-slm/bin/hf download pravz/slm_v1 --local-dir ./slm_v1

# 3. Serve on an OpenAI-compatible endpoint (stdlib HTTP, works on any OS)
.venv-slm/bin/python AIdataset/model/serve_slm.py --model ./slm_v1 --port 8001
#    Linux + GPU? `vllm serve ./slm_v1 --served-model-name tradebot-slm-v1`
#    is 20-50x faster and speaks the identical protocol.
```

Then in the dashboard, **Settings → AI analysis**:

| field | value |
|---|---|
| AI model | `slm` |
| SLM endpoint | `http://127.0.0.1:8001/v1/chat/completions` |
| SLM model name | `tradebot-slm-v1` |

Verify end to end — this runs the real analyzer path and validates against the
live schema:

```bash
python scripts/smoke_slm.py
```

### Why this is not just an endpoint swap

The two models **do not speak the same language.** DeepSeek gets the operator's
prompt template and returns the bot's schema. The SLM gets the exact prompt
format it was fine-tuned on and returns its own:

```
{"event_type":"ORDER_WIN","materiality":"HIGH","surprise":"MEDIUM",
 "facts":{"amount_inr_cr":412.0,"amount_basis":"OTHER","amount_to_mcap":0.175},
 "direction":"FLAT","mover":false,"shape":"DELAYED","price_path":[...17]}
        ↓  slm_adapter.to_analysis()
{"event_type":"ORDER_WIN","recommendation":"HOLD","confidence":0.5, ...}
PASS — this response would reach the rules engine.
```

[`app/analyzer/slm_adapter.py`](app/analyzer/slm_adapter.py) owns both
directions — prompt in, mapping out — so everything downstream is byte-identical
and the two models are directly comparable on the Outcomes and Dataset pages.
Without it, `LLM_PROVIDER=slm` would fail schema validation on every filing.

Three mapping decisions worth knowing:

- **`confidence` is derived, not predicted.** The SFT targets carried no
  confidence field. It is computed from `mover` and `materiality`, and given this
  project's headline finding, it is explicitly labelled unproven in the code
  until it has been through the calibration report.
- **`mover: false` → HOLD regardless of direction.** A predicted drift is not a
  reason to take a position.
- The model's single `RESULTS` class is split back into Q1/Q2/Q3/Q4/ANNUAL by
  the bot's own headline detector, because the rules engine keys on the quarter.

The untouched model JSON is preserved at `analyses.raw_response["slm"]`, so the
price and volume paths — which have no slot in the bot's schema — survive for
analysis.

Flip the dropdown back to `deepseek` at any time; nothing else changes. A blank
or unreachable endpoint falls back to DeepSeek automatically. **The switch can
degrade; it can never block a signal.**

> **Speed reality check, measured.** On a CPU laptop (bf16, no GPU) one filing
> takes **~36 s** — 142 tokens at ~4 tok/s — against a ~2.5 s DeepSeek call and
> the pipeline's 12 s `LLM_TIMEOUT_SECONDS`. The live analyzer will time out and
> fall back, by design. This path is for **comparison and research**, not
> production. On a GPU with vLLM it sits comfortably inside the budget.

---

## 12. Quickstart

Prerequisites: **Python 3.11**, **Node.js 20+**.

```powershell
git clone https://github.com/thenameispravardhan/market-proof.git
cd market-proof

python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

copy .env.example .env      # then set DEEPSEEK_API_KEY (and FYERS_* for live)

.\scripts\run.ps1           # activates venv, seeds prompts, builds UI, runs uvicorn
#  → http://127.0.0.1:8000/
```

**Developer mode** (two terminals, hot reload):

```powershell
.\scripts\dev.ps1                       # backend: uvicorn --reload on :8000
cd frontend; npm install; npm run dev   # frontend: Vite HMR on :5173
```

POSIX equivalents: `scripts/run.sh`, `scripts/dev.sh`.

**It runs in paper mode out of the box** — a built-in simulator with slippage
and latency modelling, no broker account needed. You only need a DeepSeek key to
see the full pipeline work.

> The app binds to `127.0.0.1` with **no authentication** — designed for a
> single local operator, not public hosting. Going live requires connecting
> Fyers *and* typing `LIVE` in the UI.

---

## 13. Configuration

Precedence: process env / `.env` → **overridden by UI settings**, which persist
in the database and are re-applied into the environment on startup, so saved
tweaks survive a restart.

The settings surface is **derived, not hand-maintained**. `Settings.model_fields`
*is* the registry: pydantic already knows every key, its type and default, so
adding a knob is one edit in `app/config.py` and it appears in the UI with a
derived label, correct widget and sane bounds. A test fails the build if a new
field is neither exposed nor deliberately excluded. This replaced a system where
every knob was declared in four places — which is how 67 settings had become
unreachable from the frontend, and how a hand-written mirror had silently
drifted from the real defaults on seven values.

> **Config drift note:** `app/config.py` defaults are authoritative and
> intentionally differ from `.env.example` on a few keys
> (`POLL_INTERVAL_SECONDS`, `MAX_NEWS_AGE_SECONDS`, `LLM_MAX_TOKENS`,
> `MAX_CONCURRENT_POSITIONS`). The code default is what runs when a key is
> absent. Pin anything you care about explicitly.

Never read `os.environ` directly — always depend on `get_settings()`.

---

## 14. Frontend

A Bloomberg-terminal-styled SPA: hash router, lazy-loaded pages, collapsible
grouped sidebar, and a status bar carrying WebSocket state, trading mode, capital,
a live NSE index ticker and an IST clock. Theme-aware; server state via TanStack
Query; realtime via WebSocket hooks.

| page | what it is for |
|---|---|
| **Dashboard** | live pipeline (scrape → analyse → signal), positions, P&L, risk, resources |
| **Trade** | manual order ticket, TradingView-style charts, option chain, positions |
| **Exits** | every exit-engine knob as a bounded, plain-English control with a live worked example |
| **Timing** | per-layer latency waterfall (detection / analysis / signal→order / order→fill) + the detection race |
| **Outcomes** | what every signal's stock actually did — the feedback loop |
| **Dataset** | the ML training-set builder: column picker with leak badges, filters, exports |
| **Model** | mover-model variants, holdout AUC/lift, and threshold replay before any gate is armed |
| **Prompts** | LLM template editor with version history and restore |
| **Rules** | the operator's rule builder + dry-run |
| **Strategies / Accounts / Notifications / Webhooks / Trade History / Settings** | configuration and audit |

The chart panel is a full TradingView-style implementation on lightweight-charts:
7 timeframes, 4 chart types, 9 overlays (SMA, EMA, WMA, VWAP, Bollinger,
Supertrend, Parabolic SAR, Ichimoku, Donchian), 9 oscillators (RSI, MACD,
Stochastic, ADX/DI, ATR, OBV, CCI, MFI, Williams %R), drawing tools persisted per
symbol, price alerts firing on live tick cross, compare symbols, and infinite
scroll-back.

---

## 15. Testing

```powershell
$env:TESTING=1; pytest          # 823 tests, 63 modules, in-memory SQLite
cd frontend; npm test           # 65 tests, 9 suites
```

Two suites carry most of the weight and should be run on any change to
execution:

- `test_entry_state_machine.py` (E-01…E-15) — happy path, drift + retracement
  re-arm and expiry, partial and zero fill, slippage breach, broker rejection and
  timeout, symbol-lock concurrency, flash crash inside the fill window, and
  WebSocket/REST fill deduplication.
- `test_exit_management.py` (T-01…T-30) — marketable-limit exits, breakeven,
  time/consolidation/stall exits, trailing activate/ratchet/trigger, gap
  fallback, exit partial fills, rejection escalation, stop-over-target
  precedence, restart recovery, external-close reconciliation, stale-feed guard.

---

## 16. Deployment

Runs 24/7 on a 2 GB AWS Lightsail instance in **ap-south-1 (Mumbai)** — closest
to NSE/BSE and Fyers, because latency is the product — behind Caddy for HTTPS and
basic auth, with the app itself never leaving `127.0.0.1`.

Full runbook: **[docs/DEPLOY_AWS.md](docs/DEPLOY_AWS.md)**. Server artifacts
(bootstrap, systemd unit, Caddyfile, update/backup/restore/repair scripts) live
in [`deploy/`](deploy/). Redeploy is one command: `bash deploy/push.sh`.

Four decisions there are load-bearing and each cost a production incident to
learn — see below.

---

## 17. Engineering war stories

The problems that actually consumed the time, and what they taught.

**1. The measurement was lying, and it looked fine.**
The calibration report bucketed all 17,298 filings under one event type. It read
a database column the ingest layer writes as a *constant*; the real detected type
lived inside the analysis JSON. The chart rendered, the numbers summed, nothing
errored. Fixing one helper function surfaced the 56.7% Q1-results finding that had
been invisible for months. **A dashboard that renders is not a dashboard that is
right.**

**2. A production "crash" that was not a crash.**
A corrupted single-row table made a monitoring loop retry every 10 seconds,
logging a 4 KB traceback each time — **2,096 failures in 6 hours**. The log
flood, not the corruption, pushed the 2 GB box into a swap storm; health checks
stopped answering while `systemctl` still reported the unit active. Fixed with
exponential backoff and a full traceback only on the *first* failure of a streak:
2,160 → 75 failures over the same window.

**3. A silent no-trade day.**
Fyers tokens expire daily. With an expired token every entry blocked on
`NO_LIVE_PRICE` while REST quotes, the LLM and the rules engine all looked
perfectly healthy — the failure was invisible until the close. Now a 09:05
pre-market preflight probes a live quote and the AI toggle and alerts *only* on a
problem. No all-clear ping: an alarm that fires daily gets muted.

**4. A dead WebSocket that looked alive.**
A restart before the 09:15 open left the Fyers data socket connected but silent —
it never began ticking. REST quotes kept working, so the dashboard looked
half-alive while live price ticking was dead all day. Now a silence watchdog
force-reconnects a subscribed socket with no frames for 60 s during market hours,
rate-limited so a genuinely dead feed cannot storm.

**5. Extraction scrambled the tables it most needed.**
On a penalty filing, flat text extraction spread a fine table across many lines;
a small model asked to re-pair labels with numbers swapped two regulation rows and
produced a total that did not match its own line items. We added grid-aware table
extraction — then measured that 58% of filings "had tables" because every filing
opens with a two-column postal addressee block. A filter requiring ≥3 numeric
cells cut it to a true 32% and stopped ~30% of the character budget going to
envelopes.

**6. Silent corpus loss.**
Transient network errors were being cached as *permanent* failures, and the
resume logic skips anything already recorded — so a network blip dropped that
filing from the corpus forever. Measured 12.5% transient rate on the first real
batch. Adding retries with backoff and re-attempting past transient failures took
yield from 85% → 97%.

**7. Rate limits are not monotonic.**
Downloading filings at concurrency 8 gave 78% success at 0.8 files/s; at
concurrency **3** it gave 99–100% at **5.0 files/s** — six times faster by asking
for less. The opposite tuning was correct for the other exchange, so the constant
could not be shared.

**8. The first fine-tune answered the wrong question.**
v1 predicted "no move" for 94.2% of filings — the loss-minimising answer when
training data is 71.5% "no move". Every mechanism built to prevent that (44,739
flagged hard examples, 114,694 contrastive pairs) had been left switched off. It
never tested *"can this be learned"*, only *"what does an unweighted objective do
on an imbalanced corpus"*. Caught by looking at the prediction distribution, not
the loss curve — eval loss fell 74% throughout.

**9. An index that was never created.**
One endpoint took 4.1 s against milliseconds for everything else. SQLite was
building an automatic partial index over 81,820 rows on *every* request. Root
cause, general rather than one-off: `create_all` only builds a table's indexes
when it builds the *table*, so any index declared on a column added later by
migration is silently never created. Fixed by reading index declarations off the
metadata and creating whatever is missing — 9.25 s → 1.24 s.

---

## 18. What does not work, honestly

A section we think should be mandatory.

- **Neither model beats a constant at direction prediction.** See §10. We report
  balanced accuracy, not the 96% number that is really the base rate.
- **The fine-tune did not beat TF-IDF** as a representation. The confidence
  interval spans zero. We call that *matched*, not *beaten*.
- **Two ML phases were killed after measurement, not abandoned.** A materiality
  pre-filter model topped out at 90.13% against an 89.31% "always no move"
  baseline — under 1 percentage point of headroom — and four pure-volatility
  features with zero news content reached 0.73 of the best model's AUC. It was a
  volatility predictor in a news-model costume. Dropping every AI-derived column
  cost 0.002 AUC and *improved* PR-AUC. They stay documented as killed so nobody
  rebuilds them.
- **The taken-trade sample is 97 rows.** Every claim about selection quality is
  early. We do not claim profitability, and the caveat travels with the number.
- **A self-learning conviction layer was deleted, not shipped.** After 5,158
  logged outcomes it had 165 usable BUY samples and 0 SELL. The engine was sound;
  the corpus never arrived. Deleting it was the correct outcome.
- **The live model gate is off, and its own replay says keep it off.** On 3,000
  real outcomes the blocked set moved *more* often than the allowed set. That is
  the tooling working as designed.

---

## 19. Project structure

```
app/
├── analyzer/          fast track · hybrid · DeepSeek client · slm_adapter ·
│                      rules engine · PDF extract + cache · prompts · schemas
├── api/               26 REST + WebSocket routers (the control plane)
├── backtest/          sandboxed replay through the SAME analyzer→rules→risk path
├── db/                18 tables, session/engine, migrations, index guard
├── execution/         paper + Fyers backends, entry state machine, trade manager,
│                      market-data bus, quote feed, order reconciliation
├── monitors/          NSE API · BSE API · NSE RSS racer · manager · base loop
├── notifications/     telegram · discord · email · webhook channels
├── risk/              engine (R0–R14) · position sizer · perf sizer ·
│                      circuit breakers · volatility · market clock
├── services/          event bus · instrument master · outcome logger ·
│                      dataset builder · health report · mover model · audit
├── webhooks/          inbound + outbound dispatch, HMAC signing
└── tests/             63 modules / 823 tests
AIdataset/model/       corpus build · baselines · training · eval · serve_slm · model card
frontend/src/          14 pages, components, hooks, indicators (9 suites / 65 tests)
deploy/                Lightsail bootstrap, systemd, Caddy, backup/restore/repair
docs/                  AWS deployment runbook
scripts/               run · dev · smoke_slm · seeders · maintenance
```

---

## 20. Team, licence, disclaimer

Built by **Manchala Nitya Pravardhan** —
B.E. Artificial Intelligence & Data Science, Chaitanya Bharathi Institute of
Technology (CBIT), Hyderabad.

- Code: **MIT**
- Model `tradebot-slm-v1`: **Apache 2.0** (inherited from Qwen2.5-1.5B-Instruct)

> **Disclaimer.** This is a personal engineering and research project. It is
> **not investment advice** and comes with **no warranty**. Automated trading
> carries substantial financial risk. Use paper mode; never deploy capital you
> cannot afford to lose. You are responsible for compliance with your broker's
> terms and all applicable regulations, including SEBI's algorithmic-trading
> framework. The results in §10 explicitly say the model does not beat a bag of
> words at the task it was trained for — do not put it in front of money.

*Built with the Fyers API and DeepSeek ·
[GitHub](https://github.com/thenameispravardhan) ·
[tradebot-slm-v1 on Hugging Face](https://huggingface.co/pravz/slm_v1)*
