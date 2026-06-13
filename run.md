# How to run the AI News Trading Bot

Local-first trading bot. Single-operator setup. No auth. Binds to
`127.0.0.1` by default.

---

## 0. Prerequisites

| Tool      | Version | Why                                |
| --------- | ------- | ---------------------------------- |
| Python    | 3.11+   | Backend runtime                    |
| Node.js   | 20+     | Frontend build (npm + Vite)        |
| Git       | any     | Clone the repo                     |
| ~500 MB   | disk    | `.venv` + `node_modules` + `./data` |

Optional:
- **DeepSeek API key** for AI classification (paper mode works without it, falls back to rules)
- **Fyers account** (app_id + secret) for live trading and real-time index data

---

## 1. First-time setup

```bash
# Clone
git clone <repo-url> ai-news-trading-bot
cd "ai-news-trading-bot"

# Create venv + install Python deps
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
# source .venv/bin/activate

pip install -r requirements.txt

# Create your local .env (do NOT commit it)
cp .env.example .env
# Now edit .env and set:
#   DEEPSEEK_API_KEY=sk-...   (optional in paper mode)
#   FYERS_APP_ID=...          (only needed for live trading)
#   FYERS_SECRET_KEY=...      (only needed for live trading)
```

The SQLite database is auto-created on first start (`./data/trading.db`).
Default tables, default signal rules, and default prompt templates are
seeded automatically by the lifespan — no manual migration step.

---

## 2. Run — easy way (Windows, single command)

From the project root, in PowerShell:

```powershell
.\scripts\run.ps1
```

This script will:
1. Activate `.venv`
2. Create `.env` from `.env.example` if missing
3. Seed the default prompt templates (idempotent)
4. Build the React frontend (`npm install` + `npm run build`) if needed
5. Start `uvicorn` on `127.0.0.1:8000`

Open **http://127.0.0.1:8000/** in your browser.

The matching shell script is `scripts/run.sh` (macOS / Linux):
```bash
./scripts/run.sh
```

---

## 3. Run — dev mode (live reload, two terminals)

For active development, you want hot-reload on both the backend and
the frontend.

**Terminal 1 — backend (Python hot-reload):**
```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

**Terminal 2 — frontend (Vite HMR):**
```powershell
cd frontend
npm install        # only the first time
npm run dev
```

Open **http://localhost:5173/** — the Vite dev server proxies
`/api/*` and `/ws` to the backend on `127.0.0.1:8000`, so the UI
talks to the API without CORS issues.

The matching helper is `scripts/dev.ps1` / `scripts/dev.sh`.

---

## 4. Tests

```bash
# Backend (pytest)
pytest -q

# Frontend (vitest)
cd frontend
npm test
```

---

## 5. Verify the install

Once the backend is running:

```bash
# Health check
curl http://127.0.0.1:8000/health

# API docs (Swagger UI)
# Open in browser:  http://127.0.0.1:8000/docs
```

The health response includes:
- `version` — app version
- `trading_mode` — `paper` or `live`
- `fyers_configured` — whether Fyers creds are loaded
- `deepseek_configured` — whether the DeepSeek key is set

---

## 6. Common `.env` keys

| Key                       | Default                | Purpose                                          |
| ------------------------- | ---------------------- | ------------------------------------------------ |
| `TRADING_MODE`            | `paper`                | `paper` (simulated) or `live` (real orders)      |
| `DEEPSEEK_API_KEY`        | empty                  | AI classification; empty = rules-only in paper    |
| `FYERS_APP_ID`            | empty                  | Fyers broker app ID                              |
| `FYERS_SECRET_KEY`        | empty                  | Fyers broker secret                              |
| `FYERS_ACCESS_TOKEN`      | empty                  | Set automatically by OAuth callback              |
| `FYERS_REDIRECT_URI`      | `http://127.0.0.1:8000/api/fyers/callback` | Must match what's registered in the Fyers app |
| `BIND_HOST`               | `127.0.0.1`            | Change to `0.0.0.0` only on a trusted LAN       |
| `BIND_PORT`               | `8000`                 |                                                  |
| `LOG_LEVEL`               | `INFO`                 | `DEBUG` / `INFO` / `WARNING` / `ERROR`           |
| `POLL_INTERVAL_SECONDS`   | `5`                    | How often NSE/BSE monitors fetch                 |
| `MAX_CAPITAL_RISK_PCT`    | `1.0`                  | Default per-trade risk cap                       |
| `DAILY_MAX_LOSS_PCT`      | `2.0`                  | Daily realised-loss circuit-breaker              |
| `PORTFOLIO_VALUE`         | `1000000`              | Paper-mode starting capital (₹)                 |

> **Security:** `BIND_HOST=0.0.0.0` is allowed but the app will log a
> loud warning at startup. There is **no auth** — every endpoint
> including `/api/settings` (which can flip `TRADING_MODE` to
> `live`) is wide open. Bind only to loopback unless you're behind
> a reverse proxy with mTLS/SSO.

---

## 7. Going live (Fyers OAuth, one-time)

Paper mode is the default and is safe. To enable live trading:

1. **Set Fyers creds in `.env`:**
   ```bash
   FYERS_APP_ID=<your-app-id>
   FYERS_SECRET_KEY=<your-secret>
   FYERS_REDIRECT_URI=http://127.0.0.1:8000/api/fyers/callback
   ```

2. **Restart the backend** so the new env vars load.

3. **Verify Fyers is loaded:**
   ```bash
   curl http://127.0.0.1:8000/health
   # Look for: "fyers_configured": true
   ```

4. **Run the OAuth flow** (one-time, browser):
   ```bash
   curl http://127.0.0.1:8000/api/fyers/authorize-url
   ```
   Open the URL it returns → log into Fyers → approve. You'll be
   redirected to `/api/fyers/callback?auth_code=...&state=...`.
   The backend exchanges the code for an access token, stores it in
   the `broker_accounts` table, and writes an `audit_log` row.
   **The token is never logged.**

5. **Check the live market data** (status bar should now show real
   NIFTY / SENSEX / BANKNIFTY):
   ```bash
   curl http://127.0.0.1:8000/api/market/indices
   ```

6. **Flip to live mode** (irreversible per signal — the 10 risk
   rules still apply):
   ```bash
   curl -X POST http://127.0.0.1:8000/api/settings/trading-mode \
        -H 'Content-Type: application/json' \
        -d '{"mode": "live", "confirm": true}'
   ```

> Fyers access tokens expire (typically every 24h). When you see
> `fyers 401: token invalid or expired` in the logs, just re-run
> step 4.

---

## 8. Stopping the app

| Mode            | How                                                            |
| --------------- | -------------------------------------------------------------- |
| Foreground      | `Ctrl+C` in the terminal                                       |
| Background task | `Get-Process -Name uvicorn` (Windows) / `ps aux \| grep uvicorn` (Linux), then `kill <pid>` |
| Port already in use | `netstat -ano \| findstr :8000`, then `taskkill /PID <pid> /F` |

---

## 9. Troubleshooting

| Symptom                                   | Fix                                                                 |
| ----------------------------------------- | ------------------------------------------------------------------- |
| `sqlite3.OperationalError: database is locked` | Another process has `./data/trading.db` open. Stop it.         |
| `Address already in use` on port 8000     | Another app is using the port. See "Stopping the app" above.        |
| `DEEPSEEK_API_KEY not set` warning        | Expected in paper mode. Set the key in `.env` to enable AI.         |
| `FYERS_ACCESS_TOKEN expired` on live orders | Re-run the OAuth flow (step 7.4).                                 |
| `ModuleNotFoundError: playwright`         | `pip install playwright` (only needed for the NSE/BSE scraper).     |
| Frontend blank after build                | `cd frontend && npm run build` failed — check the console.          |
| `Cannot find module 'frontend/dist/index.html'` | Frontend hasn't been built. Run `npm run build` in `frontend/`. |
| Hot-reload doesn't pick up `.env`         | Restart `uvicorn` — settings are `lru_cache`d at first call.        |
| Status bar shows `FYERS NOT CONFIGURED`   | You haven't completed step 7 yet (Fyers OAuth flow).                |
| News Pipeline "Processed" time is wrong   | Make sure you restarted after the timezone fix; the backend needs a reload. |

For deeper operator docs (the 10 risk rules, backtest, webhooks,
logs) see **RUNBOOK.md**.
