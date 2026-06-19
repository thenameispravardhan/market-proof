# 🤖 AI News Trading Bot

An AI-powered, fully automated news trading system for Indian equity markets (NSE & BSE). The bot monitors corporate announcements in real time, runs them through a DeepSeek LLM analyzer, generates signals, sizes positions, manages risk, and executes orders through the Fyers broker API — all from a single-machine setup with a React dashboard.

---

## ✨ Features

| Layer | What it does |
|---|---|
| **Monitor (T2)** | Playwright-based scrapers poll NSE & BSE for corporate filings every few seconds, deduplicating via SHA-256 content hash |
| **Analyzer (T3)** | DeepSeek LLM evaluates each announcement via a configurable rules engine and prompt templates; produces BUY / SELL / HOLD signals with confidence scores |
| **Execution (T4)** | Paper trading engine and live Fyers integration; binary WebSocket feeds for real-time prices & fill updates; automatic order reconciliation |
| **Risk (T5)** | Real-time circuit breakers: daily loss limits, max concurrent positions, single-position caps, liquidity filters, market-hours gate |
| **Dashboard** | React + Vite SPA with dark/light themes; live trade feed, P&L, signal rules editor, prompt manager, backtest runner, broker account manager |
| **Infrastructure** | FastAPI + SQLite (14 tables), Pydantic-settings config, structured JSON logging with correlation IDs, in-process event bus, WebSocket multiplexer |

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    React Frontend (Vite)                     │
│  Dashboard · Trade Feed · Rules · Prompts · Backtest · Logs  │
└─────────────────────┬───────────────────────────────────────┘
                      │  REST + WebSocket
┌─────────────────────▼───────────────────────────────────────┐
│                   FastAPI Backend                            │
│                                                             │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌──────────┐  │
│  │ Monitor  │  │ Analyzer │  │ Execution │  │   Risk   │  │
│  │ NSE/BSE  │─▶│ DeepSeek │─▶│  Manager  │─▶│  Engine  │  │
│  │ Scraper  │  │   LLM    │  │ (T4)      │  │ (T5)     │  │
│  └──────────┘  └──────────┘  └─────┬─────┘  └──────────┘  │
│                                    │                        │
│  ┌─────────────────────────────────▼────────────────────┐  │
│  │          In-Process Event Bus (asyncio.Queue)        │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              SQLite Database (14 tables)             │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                      │
          ┌───────────▼───────────┐
          │   Fyers Broker API    │
          │  Orders · Quotes ·    │
          │  WebSocket Streams    │
          └───────────────────────┘
```

---

## 📁 Project Layout

```
AI News Trading Bot/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, router mount
│   ├── config.py                # Pydantic-settings (all env vars)
│   ├── logging_config.py        # Structured JSON logging + redaction
│   ├── api/
│   │   ├── health.py            # GET /health
│   │   ├── settings_api.py      # GET/PUT /api/settings
│   │   ├── ws.py                # WebSocket /ws (pub/sub multiplexer)
│   │   ├── orders.py            # Order management endpoints
│   │   ├── positions.py         # Open positions
│   │   ├── market.py            # Market data & quotes
│   │   ├── options.py           # Options chain
│   │   ├── rules.py             # Signal rules CRUD
│   │   ├── prompts.py           # Prompt template CRUD
│   │   ├── strategies.py        # Strategy CRUD
│   │   ├── broker_accounts.py   # Broker account management
│   │   ├── backtest.py          # Backtest runner
│   │   ├── audit_log.py         # Audit log reader
│   │   ├── notifications.py     # Notification channels
│   │   ├── webhooks.py          # Inbound/outbound webhook config
│   │   ├── risk.py              # Risk state & circuit-breaker status
│   │   ├── trading_mode.py      # Toggle paper ↔ live
│   │   ├── search.py            # Full-text search across announcements
│   │   ├── fyers_callback.py    # OAuth2 callback for Fyers login
│   │   └── fyers_postback.py    # HMAC-verified order postbacks
│   ├── db/
│   │   ├── models.py            # All 14 SQLAlchemy ORM models
│   │   ├── session.py           # SessionLocal factory
│   │   └── init.py              # init_db() — creates tables
│   ├── analyzer/
│   │   ├── service.py           # Orchestrates LLM calls + signal creation
│   │   ├── deepseek_client.py   # DeepSeek API client (async, retries)
│   │   ├── rules_engine.py      # Configurable signal-rule evaluator
│   │   ├── prompts.py           # Prompt builder (uses DB templates)
│   │   ├── default_rules.py     # Seed rules for fresh installs
│   │   └── schemas.py           # Pydantic schemas for LLM I/O
│   ├── execution/
│   │   ├── manager.py           # Central execution manager (paper + live)
│   │   ├── trade_manager.py     # Open-trade lifecycle, TIME_EXIT, stops
│   │   ├── paper.py             # Paper trading simulation engine
│   │   ├── fyers_live.py        # Fyers REST order placement (httpx)
│   │   ├── fyers_stream.py      # Fyers binary WebSocket (prices + fills)
│   │   ├── fyers_auth.py        # Token refresh & TOTP auth
│   │   ├── quote_feed.py        # Unified price feed abstraction
│   │   ├── market_data.py       # Instrument info, LTP lookups
│   │   ├── order_reconcile.py   # Reconcile local orders vs. broker
│   │   └── symbols.py           # NSE/BSE symbol normalization
│   ├── monitors/
│   │   ├── base.py              # Abstract monitor (Playwright base class)
│   │   ├── nse.py               # NSE corporate announcements scraper
│   │   ├── bse.py               # BSE corporate filings scraper
│   │   └── manager.py           # Starts/stops both monitors
│   ├── risk/
│   │   ├── engine.py            # Real-time risk evaluation
│   │   ├── circuit_breakers.py  # Halt conditions (loss, position count…)
│   │   ├── position_sizer.py    # Kelly / fixed-fraction sizing
│   │   └── market_clock.py      # NSE market hours gate
│   ├── services/
│   │   ├── event_bus.py         # In-process pub/sub (asyncio.Queue)
│   │   ├── instrument_master.py # Fyers instrument master downloader
│   │   └── webhook_service.py   # Outbound webhook dispatcher
│   └── tests/
│       ├── conftest.py
│       ├── test_db.py
│       ├── test_health.py
│       └── test_settings_api.py
├── frontend/                    # React + Vite + TypeScript SPA
│   ├── src/
│   │   ├── pages/               # Dashboard, Trade, Rules, Prompts, …
│   │   ├── components/          # Reusable UI components
│   │   ├── hooks/               # Custom React hooks
│   │   ├── api/                 # API client functions
│   │   └── types.ts             # Shared TypeScript types
│   └── vite.config.ts
├── scripts/
│   ├── dev.ps1 / dev.sh         # One-command dev startup
│   ├── run.ps1 / run.sh         # Production run scripts
│   ├── setup_fyers.py           # Interactive Fyers OAuth setup wizard
│   ├── seed_demo_data.py        # Populate DB with demo trades
│   ├── seed_default_rules.py    # Seed default signal rules
│   ├── seed_default_prompts.py  # Seed default LLM prompt templates
│   ├── backtest_seed.py         # Generate backtest fixtures
│   ├── smoke_e2e.py             # End-to-end smoke test
│   ├── smoke_t2_monitor.py      # Monitor layer smoke test
│   ├── smoke_t3_analyzer.py     # Analyzer layer smoke test
│   └── smoke_t4_execution.py    # Execution layer smoke test
├── data/                        # SQLite DB files (git-ignored)
├── logs/                        # Runtime log files (git-ignored)
├── docs/                        # Additional documentation
├── .env.example                 # Template for environment variables
├── requirements.txt             # Python dependencies
├── RISK.md                      # Risk management documentation
└── RUNBOOK.md                   # Operational runbook
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Node.js 18+ and npm
- A [Fyers](https://myapi.fyers.in/) API account (for live trading; paper mode works without it)
- A [DeepSeek](https://platform.deepseek.com/) API key

### 1. Clone & set up Python environment

```bash
git clone https://github.com/thenameispravardhan/AI-News-Trading-Bot.git
cd "AI News Trading Bot"

python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. Install Playwright browsers

```bash
playwright install chromium
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in your API keys
```

Key variables in `.env`:

| Variable | Description |
|---|---|
| `TRADING_MODE` | `paper` (default) or `live` |
| `DEEPSEEK_API_KEY` | Your DeepSeek API key |
| `FYERS_APP_ID` | Fyers App ID (live trading only) |
| `FYERS_SECRET_KEY` | Fyers secret key |
| `FYERS_ACCESS_TOKEN` | Fyers access token (refreshed automatically) |
| `DATABASE_URL` | `sqlite:///./data/trading.db` |
| `MAX_CAPITAL_RISK_PCT` | Risk per trade (default: `1.0`%) |
| `DAILY_MAX_LOSS_PCT` | Daily loss circuit breaker (default: `2.0`%) |
| `MAX_NEWS_AGE_SECONDS` | Ignore news older than this (default: `90`s) |
| `MAX_HOLD_SECONDS` | Force-close positions after this (default: `1800`s) |

### 4. Set up Fyers OAuth (live trading only)

```bash
python scripts/setup_fyers.py
```

This walks you through the TOTP-based Fyers login flow and saves the access token to `.env`.

### 5. Seed default data (optional)

```bash
python scripts/seed_default_rules.py
python scripts/seed_default_prompts.py
python scripts/seed_demo_data.py   # populates dummy trades for the UI
```

### 6. Start the backend

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir app
```

Open <http://127.0.0.1:8000/health> and <http://127.0.0.1:8000/docs> to verify.

### 7. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

Open <http://localhost:5173> for the dashboard.

### One-command dev startup (Windows)

```powershell
.\scripts\dev.ps1
```

---

## 🧪 Testing

```bash
# Python unit tests (in-memory SQLite)
pytest -q

# Individual smoke tests
python scripts/smoke_e2e.py
python scripts/smoke_t2_monitor.py
python scripts/smoke_t3_analyzer.py
python scripts/smoke_t4_execution.py

# Frontend tests
cd frontend && npm test
```

---

## 📡 API Reference

### Health

```http
GET /health
→ 200 {"status": "ok", "ts": "...", "version": "..."}
```

### Settings

```http
GET  /api/settings
→ 200 {"global": {TRADING_MODE, MAX_CAPITAL_RISK_PCT, ...}, "sections": {...}}

PUT  /api/settings
Body: {"global": {...}}   # partial updates OK
→ 200 updated settings
→ 422 invalid value
→ 409 cross-validation failure (e.g. live mode without Fyers token)
```

Side effect: writes to `audit_log` and broadcasts `settings.updated` on the event bus.

### WebSocket — `GET /ws`

```text
Server frames (JSON):
  {"type": "connected", "ts": "..."}
  {"type": "event",     "channel": "...", "payload": {...}, "ts": "..."}
  {"type": "ping",      "ts": "..."}

Client frames:
  {"type": "subscribe",   "channels": ["signal.created", "trade.executed"]}
  {"type": "unsubscribe", "channels": ["..."]}
  {"type": "ping"}
```

Event channels: `settings.updated`, `signal.created`, `trade.executed`, `risk.halt`, `log`.

### Interactive Docs

Full Swagger UI at <http://127.0.0.1:8000/docs> — all 40+ endpoints are documented.

---

## 🗃️ Database Schema

14 SQLAlchemy ORM models in `app/db/models.py`:

**Core trading tables:**
`Announcement` · `Analysis` · `Signal` · `Trade` · `Position` · `RiskEvent`

**Advanced tables:**
`Strategy` · `BrokerAccount` · `SignalRule` · `PromptTemplate` · `PromptHistory` · `Webhook` · `WebhookDelivery` · `NotificationChannel` · `NotificationLog` · `BacktestRun` · `AuditLog`

**Announcement deduplication:** the `announcements` table uses a `UNIQUE(content_hash)` constraint (SHA-256 of `exchange|symbol|filed_at|pdf_url`) to prevent duplicate filings from triggering multiple trades.

---

## 🔌 Internal Event Bus

```python
from app.services.event_bus import event_bus

# Publish (fire-and-forget; drops if subscriber queue is full)
await event_bus.publish("signal.created", {"signal_id": 42})

# Subscribe
async def worker():
    sub = event_bus.subscribe("signal.created")
    try:
        while True:
            event = await sub.get()
            # event.channel, event.payload, event.event_id, event.ts
    finally:
        event_bus.unsubscribe("signal.created", sub)
```

- `event_bus.subscribe(channel) → Queue`
- `event_bus.unsubscribe(channel, queue)`
- `await event_bus.publish(channel, payload)` — backpressure: logs warning & drops on full queues
- `await event_bus.publish_blocking(channel, payload)` — awaits drain; use sparingly

---

## 🔒 Security

> **This app is designed for local, single-operator use only.**

- Binds to `127.0.0.1` by default — **no authentication required**
- WebSocket connections from non-localhost IPs are rejected
- Secrets in `.env` are redacted from all log output (any key containing `KEY`, `TOKEN`, `SECRET`, or `PASSWORD` is replaced with `***`)
- If you set `BIND_HOST=0.0.0.0`, the server logs a loud warning at startup

> ⚠️ **Do not expose to untrusted networks.** Every endpoint — including `/api/settings` which can switch `TRADING_MODE` to `live` — is unauthenticated. Use a reverse proxy with mTLS / SSO for remote access.

---

## 📊 Risk Management

Configured via `.env` and the Settings API. See [RISK.md](RISK.md) for full details.

| Control | Default | Description |
|---|---|---|
| `MAX_CAPITAL_RISK_PCT` | 1.0% | Max risk per trade |
| `DAILY_MAX_LOSS_PCT` | 2.0% | Daily loss circuit breaker (halts all trading) |
| `MAX_CONCURRENT_POSITIONS` | 5 | Max open positions at once |
| `MAX_SINGLE_POSITION_PCT` | 20% | Max capital in one position |
| `MIN_LIQUIDITY_CRORE` | 5 Cr | Minimum stock liquidity filter |
| `MAX_SIGNALS_PER_DAY` | 20 | Daily signal cap |
| `MAX_NEWS_AGE_SECONDS` | 90s | Ignore stale announcements |
| `MAX_HOLD_SECONDS` | 1800s | Force-close timer (30 min) |

---

## 🔧 Tech Stack

**Backend**
- [FastAPI](https://fastapi.tiangolo.com/) 0.115 + [Uvicorn](https://www.uvicorn.org/) 0.32
- [SQLAlchemy](https://www.sqlalchemy.org/) 2.0 + [Alembic](https://alembic.sqlalchemy.org/) migrations
- [Pydantic](https://docs.pydantic.dev/) v2 + pydantic-settings
- [structlog](https://www.structlog.org/) for JSON structured logging
- [httpx](https://www.python-httpx.org/) for async HTTP
- [Playwright](https://playwright.dev/python/) for browser-based scraping
- [fyers-apiv3](https://pypi.org/project/fyers-apiv3/) for broker integration

**Frontend**
- [React](https://react.dev/) 18 + [TypeScript](https://www.typescriptlang.org/)
- [Vite](https://vitejs.dev/) 6
- [TanStack Query](https://tanstack.com/query) for server state
- [Recharts](https://recharts.org/) for P&L charts
- [@dnd-kit](https://dndkit.com/) for drag-and-drop rule ordering

**Testing**
- [pytest](https://pytest.org/) + [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [Vitest](https://vitest.dev/) + [@testing-library/react](https://testing-library.com/)

---

## 📖 Additional Docs

- [RUNBOOK.md](RUNBOOK.md) — Day-to-day operational procedures
- [RISK.md](RISK.md) — Risk management philosophy and controls
- [run.md](run.md) — Detailed run instructions and troubleshooting

---

## ⚠️ Disclaimer

This software is for **educational and research purposes only**. Automated trading involves significant financial risk. Past performance does not guarantee future results. Always test thoroughly in paper mode before risking real capital. The authors are not responsible for any financial losses.

---


