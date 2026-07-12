# TRADEBOT — AI News Trading Bot

> **Material corporate news moves NSE/BSE stocks within seconds. This system automates the "first read" and the "first trade" — then wraps it in strict, non-bypassable risk controls so bad data can't blow up the account.**

A local-first, AI-driven, **intraday** equity news-trading system for Indian markets (NSE / BSE, cash MIS). It continuously watches exchange announcement feeds, classifies each fresh filing (deterministically *or* with an LLM), evaluates it against operator-defined rules, sizes and gates the trade through a hard risk engine, and executes on **Fyers** (live) or a built-in **paper** simulator — with realtime, tick-driven exit management.

Built on the **Fyers API** · **DeepSeek** LLM · **FastAPI** + **React**.

<!-- Add a 60–90 sec demo here before sharing: -->
<!-- ![Demo](docs/demo.gif) -->

---

## Why it exists — the edge

Retail traders reacting to Telegram/Twitter alerts enter a news move *halfway through*. News alpha decays in minutes. The advantage is being **early and disciplined**:

- **Latency is the product** — a late entry is worse than no entry.
- **Early entry is the edge** — never fix whipsaw by delaying entry; fix it in the stop, the sizing, and the slippage caps.
- **Intraday only** — every position is MIS; time-exit and end-of-day square-off always flatten. No overnight gap risk.
- **Non-bypassable risk** — every signal passes through the risk engine and portfolio circuit breakers. There is no `force=True`.

---

## What it does

```
[NSE / BSE filings]
      │  poll every N seconds
      ▼
 Monitors ── dedupe (SHA-256), prefetch PDF, drop noise ──► announcements.new
      ▼
 Analyzer   staleness gate → noise filter →
      │     FAST TRACK (headline) | HYBRID (headline + PDF) | LLM (DeepSeek)
      │     → schema-validate → rules engine → pipeline deadline
      ▼
 signals.new   (BUY / SELL / HOLD / BLOCK, with entry / SL / target)
      ▼
 Execution  circuit-breaker check → Risk engine (hard gates + sizing) →
      │     Entry state machine: drift check → IOC routing → dual-confirmation
      │     fill watch → partial-fill handling → handover
      ▼
 [Paper]  or  [Fyers Live]  ──► trade.executed
      ▼
 Trade Manager  realtime on every Fyers WS tick:
                STOP / TARGET / BREAKEVEN / TRAIL / SCALE-OUT /
                CONSOLIDATION / STALL / TIME-EXIT / EOD square-off
      ▼
 Dashboard (WebSocket) · Notifications (Telegram/Discord/Email) · Webhooks
```

Everything is controlled from a browser dashboard — no CLI-only gates, no config-file toggles.

---

## Key features

**News → signal pipeline**
- NSE + BSE announcement monitors with content-hash de-duplication and background PDF prefetch.
- **Two-track analyzer**: a deterministic **fast track** (unambiguous headline shapes → signal in milliseconds, no LLM), a **hybrid** path (headline + PDF value parse), and a **DeepSeek LLM** track for everything else. Both feed the same rules + risk path.
- Optional **extracted-text mode**: real filing PDF text (PyMuPDF, Hindi-line filtering, keyword/digit page scoring) sent to the LLM with a *no-trade-by-default* stance — degrades gracefully to a metadata prompt, never blocks a signal.
- Operator-curated **rules engine** (`all_of` / `any_of` conditions, priority-ordered, first match wins) — no hard-coded triggers.

**Risk & execution**
- Risk engine with hard gates (confidence floors, stop sanity, concurrency, single-name & sector caps, liquidity, spread, shorting) plus position sizing on a real equity ledger.
- Portfolio **circuit breakers** + kill switch (daily / weekly / monthly loss, consecutive-loser cooldown, trade caps) that survive restarts.
- Explicit **entry state machine**: anti-chase drift gate with passive retracement watch, per-symbol mutex, IOC marketable-limit routing, dual-confirmation fill watch, partial-fill handling, and flash-crash / slippage-breach emergency flattens.
- **Realtime exit management** on every Fyers WebSocket tick — protective stop checked before target; live exits route through the broker with market fallback; a 60s reconciliation loop syncs externally-closed positions.

**Observability & data**
- Pipeline-latency metrics (p50/p95/p99), signal-outcome tracking, daily health report, performance-weighted sizer, circuit-breaker history.
- **ML dataset builder**: enriches every signal (and every analyzed-but-unsignaled filing) with 1-minute-candle reaction features + horizon targets for future model training — CSV/JSONL export with look-ahead-leak checks.

**Frontend (Bloomberg-terminal-style SPA)**
- Live dashboard, TradingView-style interactive charts (lightweight-charts, 9 overlays + 9 oscillators, drawing tools, price alerts), manual order ticket, option chain, positions with live P&L, backtest viewer.

---

## Tech stack

| Layer        | Tech |
|--------------|------|
| Backend      | Python 3.11, FastAPI, Uvicorn, SQLAlchemy 2, SQLite (WAL), structlog, httpx |
| Realtime     | Fyers API v3 (WebSocket data + order sockets), in-memory async event bus |
| AI / parsing | DeepSeek (news classification), PyMuPDF (PDF text), Playwright (scrape fallback) |
| Frontend     | React 18, TypeScript 5, Vite 6, TanStack Query, Recharts, lightweight-charts |
| Testing      | pytest (~900 tests) + vitest (56 tests) |

**Pricing is Fyers-only** — all quotes, candles, and India-VIX come from Fyers; when the live feed is cold the system **blocks** rather than filling at a synthetic price.

---

## Quickstart (paper mode)

Prerequisites: **Python 3.11**, **Node.js 20+**.

```powershell
# 1. Clone and enter
cd "AI News Trading Bot"

# 2. Create venv + install
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt

# 3. Configure
copy .env.example .env      # then set DEEPSEEK_API_KEY (and FYERS_* for live)

# 4. Run (activates venv, seeds prompts, builds the UI, starts uvicorn)
.\scripts\run.ps1

# 5. Open the dashboard
#    http://127.0.0.1:8000/
```

**Developer mode** (two terminals, hot reload):

```powershell
.\scripts\dev.ps1                       # backend: uvicorn --reload on :8000
cd frontend; npm install; npm run dev   # frontend: Vite HMR on :5173
```

POSIX equivalents: `scripts/run.sh`, `scripts/dev.sh`.

> The system binds to `127.0.0.1` with **no authentication** — it is designed for a single local operator, not for public hosting. Going live requires connecting Fyers and a typed `LIVE` confirmation in the UI.

---

## Project structure

```
app/                 FastAPI backend
├── analyzer/         fast-track + DeepSeek + rules engine + PDF extraction
├── api/              REST + WebSocket routers
├── execution/        paper/live backends, entry state machine, trade manager
├── monitors/         NSE / BSE scrapers
├── risk/             risk engine, position sizer, circuit breakers, volatility
├── services/         event bus, instrument master, outcome logger, dataset builder
├── notifications/    telegram / discord / email / webhook channels
└── webhooks/         inbound / outbound webhook dispatch + HMAC signing
frontend/            React + Vite dashboard
scripts/             run / dev, Fyers setup, seeders, smoke tests
PROJECT.txt          full internal system specification
plan.txt             improvement roadmap
```

---

## Testing

```powershell
# Backend (in-memory SQLite)
$env:TESTING=1; pytest

# Frontend
cd frontend; npm test
```

---

## Design philosophy

- **No black boxes** — the operator curates the logic (prompts + rules); the bot provides the framework and the guardrails.
- **Fail-safe defaults** — missing market data makes a filter skip or block, never fake a pass.
- **Never retry blind** — a broker call that times out may have placed the order; entries are never re-fired.
- **The broker is the source of truth** — live fills and exits settle only on broker confirmation.
- **Non-destructive evolution** — every new feature ships behind a toggle that defaults to the existing behavior.

---

## Disclaimer

This is a personal engineering project for research and education. It is **not** investment advice and comes with **no** warranty. Automated trading carries substantial financial risk; use paper mode, and never deploy capital you cannot afford to lose. You are responsible for compliance with your broker's terms and all applicable regulations (including SEBI's algorithmic-trading framework).

---

*Built with the Fyers API. Author: [your name] · [GitHub] · [LinkedIn]*
