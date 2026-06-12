# RUNBOOK — AI News Trading Bot

## Overview

The AI News Trading Bot is a **local-first** algorithmic trading system. It polls
NSE/BSE filings, runs each filing through a DeepSeek LLM classifier
(ORDER_WIN, BUYBACK, EARNINGS, …), evaluates the resulting signal through a
**hard 10-rule risk engine**, and routes the order to either a paper backend or
the Fyers live broker. It is intended for a single operator running on a
laptop or VPS — there is **no authentication** and the app binds to
`127.0.0.1` by default. Bring your own DeepSeek key (signals) and Fyers
credentials (live trading).

## Prerequisites

- **Python 3.11+** (3.11.x recommended; the `tomllib` stdlib import in the
  venv script is the only Python-version dependency).
- **Node.js 20+** and **npm** (only required for the React dashboard).
- A **DeepSeek API key** — `DEEPSEEK_API_KEY`. Optional for paper mode; the
  analyzer will skip LLM calls and fall back to rules.
- **Fyers credentials** for live trading:
  - `FYERS_APP_ID` — application id from https://myapi.fyers.in
  - `FYERS_SECRET_KEY` — app secret
  - `FYERS_ACCESS_TOKEN` — obtained via the OAuth callback flow (see "Going
    live" below); for paper mode it is unused.
- ~500 MB free disk for `.venv`, `frontend/node_modules`, and `./data/`.

## First-time setup

```bash
# 1. Clone
git clone <repo-url> ai-news-trading-bot
cd ai-news-trading-bot

# 2. Python venv + deps
python -m venv .venv
# Windows:  .venv\Scripts\Activate.ps1
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt

# 3. .env
cp .env.example .env
# Edit .env — set DEEPSEEK_API_KEY at minimum.
# Live mode additionally requires FYERS_APP_ID + FYERS_SECRET_KEY.

# 4. Database — auto-created on first start (CREATE TABLE IF NOT EXISTS).
#    No manual migration step is needed for v1.

# 5. Seed default prompt templates (idempotent — 16 rows: 1 DEFAULT + 15 per event_type)
python scripts/seed_default_prompts.py

# 6. (Optional) build the React dashboard
cd frontend && npm install && npm run build && cd ..
```

If you skip step 6 the API still works; the UI is served only when
`frontend/dist/` exists.

## Dev run (live-reload)

Two terminals:

```bash
# Terminal 1 — backend with reload
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

```bash
# Terminal 2 — React dev server (HMR)
cd frontend
npm run dev
# open http://localhost:5173/
```

The Vite dev server proxies `/api/*` and `/ws` to `127.0.0.1:8000`, so the UI
talks to the backend without CORS issues.

## Prod run (single process)

```bash
# 1. Build the dashboard once
cd frontend && npm install && npm run build && cd ..

# 2. Run the backend (no --reload). FastAPI serves frontend/dist/ at /.
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/>. The dashboard and API share the same origin.

The wrapper scripts `scripts/run.sh` and `scripts/run.ps1` automate the
above: venv activation, `.env` bootstrap, prompt seed, optional
`npm run build`, and `uvicorn` start. Use `scripts/dev.*` for the
two-terminal live-reload variant.

## Paper trading workflow

Paper mode is the default (`TRADING_MODE=paper` in `.env.example`). The
backend uses the in-process `PaperBackend`; orders are filled against
the last cached quote and produce a `Trade` row with `status="filled"`.

1. Confirm the current mode:
   ```bash
   curl http://localhost:8000/health
   # "trading_mode": "paper"
   ```

2. (Optional) Flip to paper explicitly. The endpoint requires the literal
   `confirm: true` — anything else returns 422.
   ```bash
   curl -X POST http://localhost:8000/api/settings/trading-mode \
        -H 'Content-Type: application/json' \
        -d '{"mode": "paper", "confirm": true}'
   ```

3. Watch a synthetic announcement flow through the pipeline:
   ```bash
   # The monitors poll NSE/BSE every POLL_INTERVAL_SECONDS (default 5).
   # When an announcement arrives, you should see:
   #   - 1 new row in `announcements`
   #   - 1 new row in `analyses` (LLM output)
   #   - 1 new row in `signals` (if the rules fire)
   #   - 0 or 1 new row in `trades` (after the risk engine approves)
   tail -f logs/app.jsonl
   ```

4. Verify a fill at `/api/positions` — the paper backend updates
   `positions` (avg price, qty, unrealized P&L) on every tick:
   ```bash
   curl http://localhost:8000/health
   # Check the structured logs in ./logs/ for "trade.executed" events.
   ```

## Going live

1. Make sure `.env` has Fyers credentials **other than the access token**:
   ```
   FYERS_APP_ID=YOUR_APP_ID
   FYERS_SECRET_KEY=YOUR_SECRET
   ```

2. Visit the Fyers auth URL (the API returns it on demand):
   ```bash
   curl http://localhost:8000/api/fyers/authorize-url
   ```
   Log in, approve the app, and you'll be redirected to
   `http://localhost:8000/api/fyers/callback?code=...&state=...`.

3. The callback exchanges the code for an access token, persists it in
   `broker_accounts.access_token`, and writes an `audit_log` row. **The
   token is never logged.**

4. Flip to live mode (the irreversible button — note `confirm: true`):
   ```bash
   curl -X POST http://localhost:8000/api/settings/trading-mode \
        -H 'Content-Type: application/json' \
        -d '{"mode": "live", "confirm": true}'
   ```

5. The execution manager hot-reloads — the next signal routes to
   `FyersLiveBackend` automatically. Confirm via `/health` (look for
   `"trading_mode": "live"`).

> **Live trading is irreversible per signal.** All 10 risk rules still
> apply; no override exists. See the Safety section.

## Backtest

The backtest engine replays historical announcements through the
analyzer → risk → paper pipeline.

```bash
# 1. Create a run
curl -X POST http://localhost:8000/api/backtest/runs \
     -H 'Content-Type: application/json' \
     -d '{
           "name": "smoke-2025",
           "start_date": "2025-01-01",
           "end_date":   "2025-03-31",
           "initial_capital": 100000
         }'
# -> 201, returns {"id": 1, "status": "pending", ...}

# 2. Poll
curl http://localhost:8000/api/backtest/runs/1
# status: pending -> running -> done | failed

# 3. Inspect trades + equity curve
curl http://localhost:8000/api/backtest/runs/1/trades
curl http://localhost:8000/api/backtest/runs/1/equity-curve
```

The backtest uses a stubbed DeepSeek transport — no API key is consumed
and the run is deterministic.

## Webhooks

CRUD over `/api/webhooks` (in/out). The inbound endpoint
`POST /api/webhooks/in/{id}` accepts signed JSON; the outbound
dispatcher POSTs to registered URLs on `signals.new`,
`trade.executed`, and friends.

**Signature header** (HMAC-SHA256, hex-encoded):

```
X-Mavis-Signature: hex(HMAC-SHA256(secret, raw_body))
```

Example sender (Python):

```python
import hmac, hashlib, json, requests
body = json.dumps({"symbol": "RELIANCE", "headline": "..."}).encode()
sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
requests.post(url, data=body, headers={
    "Content-Type": "application/json",
    "X-Mavis-Signature": sig,
})
```

A wrong or missing signature → **401**. Replay protection uses a
`timestamp` field in the body (5-minute window).

## Logs

Structured JSON via `structlog` to **stdout** and `./logs/`:

- `./logs/app.jsonl` — application events (startup, trade.executed, …)
- Secret redaction is **recursive** — any dict key matching
  `key|token|secret|password|authorization|cookie|credential` is
  replaced with `"***"` before write.
- Every line carries a `correlation_id` (set by the request middleware).

To change the level: `LOG_LEVEL=DEBUG` in `.env`.

## Troubleshooting

| Symptom                                      | Fix                                                                          |
|----------------------------------------------|------------------------------------------------------------------------------|
| `sqlite3.OperationalError: database is locked` | Another process has `./data/trading.db` open exclusively. Stop it.         |
| `Address already in use` on port 8000        | Another app binds the port. `netstat -ano \| findstr :8000`, kill the PID.  |
| `DEEPSEEK_API_KEY not set` warning           | Expected for paper mode. Set the key in `.env` to enable LLM analysis.      |
| `FYERS_ACCESS_TOKEN expired` on live orders  | Re-run the OAuth flow at `/api/fyers/authorize-url` and re-callback.        |
| `ModuleNotFoundError: playwright`            | `pip install playwright` (optional; only required for NSE/BSE scraping).     |
| Frontend blank after build                   | `cd frontend && npm run build` failed. Check the console; fix and rebuild.   |
| `Cannot find module 'frontend/dist/index.html'` | The build step hasn't run. Run `npm run build` in `frontend/`.           |
| Hot-reload doesn't pick up `.env`            | Restart `uvicorn`; `Settings` is `lru_cache`d at first call.                  |
| Smoke test fails on a fresh clone            | Ensure you ran `pip install -r requirements.txt`; the script uses httpx.     |

## Safety — the 10 risk rules

Every signal runs through `app.risk.engine.RiskEngine.evaluate(...)`. There
is no override flag, no `_internal` namespace, and no test-only branch.
Any single violation blocks the signal before it reaches the execution
manager.

| #  | Code                          | Rule                                                                 |
|----|-------------------------------|----------------------------------------------------------------------|
| R0 | `RISK_INVALID_STOP_LOSS`      | `stop_loss` is missing or `<= 0` — sized with no defined risk → blocked. |
| R1 | `RISK_ACTION_NOT_TRADABLE`    | `action` must be `BUY` or `SELL` (`HOLD`/`BLOCK` → blocked).         |
| R2 | `RISK_CONFIDENCE_NONPOSITIVE` | `confidence <= 0` → blocked.                                          |
| R3 | `RISK_QTY_BELOW_1`            | Position size rounds to 0 → blocked.                                 |
| R4 | `RISK_MAX_CAPITAL_RISK_PCT`   | Trade risk exceeds `max_capital_risk_pct` % of portfolio → blocked.   |
| R5 | `RISK_DAILY_MAX_LOSS`         | Today's realised loss ≥ `daily_max_loss_pct` % of portfolio → blocked. |
| R6 | `RISK_MAX_CONCURRENT_POSITIONS` | Open positions ≥ `max_concurrent_positions` → blocked.              |
| R7 | `RISK_MAX_SINGLE_POSITION_PCT`| Position value (incl. existing symbol exposure) > `max_single_position_pct` % → blocked. |
| R8 | `RISK_MIN_LIQUIDITY`          | Symbol ADV < `min_liquidity_crore` cr → blocked; if ADV unknown, warn + allow. |
| R9 | `RISK_SECTOR_CONCENTRATION`   | Sector exposure would exceed 30% of portfolio → blocked.             |
| R10| `RISK_SYMBOL_BLOCKLISTED`     | Symbol is on the strategy's `symbol_blocklist` → blocked.             |

Position sizing math (per trade):

```
qty = floor( (portfolio_value * max_capital_risk_pct / 100) / |entry - stop_loss| )
```

If `qty < 1`, the signal is rejected with `RISK_QTY_BELOW_1`. The
defaults are overridable per strategy in the `strategies.config` JSON
column; the values shown in the runbook are the `Settings` defaults
from `app/config.py`.

**Paper vs live:** the rules are identical. The only thing that
changes between modes is **which backend fills the order** (in-process
paper simulator vs Fyers HTTP). The risk engine never sees the
trading-mode flag.

**Going live does not weaken any rule.** The execution manager routes
every approved signal to the live backend, but it cannot route a
blocked signal — there is no code path for it.
