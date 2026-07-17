# Deploying TRADEBOT to AWS Lightsail

End-to-end runbook for running the bot 24/7 on a Lightsail instance in
**ap-south-1 (Mumbai)** — the region closest to NSE/BSE and Fyers, because
latency is the product.

## Architecture on the server

```
Internet ──443──► Caddy (HTTPS via Let's Encrypt + basic auth)
                    │ 127.0.0.1:8000
                    ▼
                  uvicorn app.main:app  (systemd: tradebot.service)
                    │
                  SQLite  ./data/trading.db  (WAL, single process)
```

Deliberate decisions baked into the config (don't undo them):

- **The app never binds publicly.** It stays on `127.0.0.1:8000`; Caddy is
  the only public listener. The app has no authentication of its own — the
  proxy's basic auth is the lock on the door.
- **No `--proxy-headers` on uvicorn.** The `/ws` WebSocket endpoint enforces
  a loopback-only client check; Caddy connects from `127.0.0.1`, which
  passes. Proxy headers would rewrite the client IP and break it.
- **Single uvicorn worker, always.** The app is one stateful process
  (in-memory event bus, schedulers, Fyers sockets). Two workers = duplicate
  monitors = duplicate trades.
- **The Fyers postback path (`/api/fyers/postback`) bypasses basic auth** —
  Fyers' servers call it and can't send credentials. It is protected by its
  own `?token=<FYERS_POSTBACK_SECRET>` check inside the app. The config
  endpoints under `/api/fyers/postback/config` stay behind auth.

## What you need before starting

| Item | Why |
|------|-----|
| AWS account + IAM access key | to create the Lightsail instance (or use the console) |
| DuckDNS account (free, [duckdns.org](https://www.duckdns.org)) | stable HTTPS hostname for the UI and the Fyers redirect URI |
| Fyers API dashboard access ([myapi.fyers.in](https://myapi.fyers.in)) | update redirect URI, whitelist the server IP (SEBI static-IP rule) |
| DeepSeek API key | the LLM analyzer |
| Your local `data/trading.db` | rules, prompts, settings, outcome history migrate with it |

Monthly cost: **~$12** (2 GB bundle) + a few cents for the static IP snapshot
storage. The static IP itself is free while attached.

## Step 1 — Create the instance

Console: Lightsail → Create instance → Region **Mumbai (ap-south-1)** →
Linux → **Ubuntu 24.04 LTS** → **2 GB RAM / 2 vCPU / 60 GB SSD ($12)** plan →
name it `tradebot`.

Or with the AWS CLI (verify IDs with `aws lightsail get-blueprints` /
`get-bundles` if these have rotated):

```bash
aws lightsail create-instances --region ap-south-1 \
  --instance-names tradebot --availability-zone ap-south-1a \
  --blueprint-id ubuntu_24_04 --bundle-id small_3_0
```

## Step 2 — Static IP + firewall

```bash
aws lightsail allocate-static-ip --region ap-south-1 --static-ip-name tradebot-ip
aws lightsail attach-static-ip  --region ap-south-1 --static-ip-name tradebot-ip --instance-name tradebot
aws lightsail open-instance-public-ports --region ap-south-1 --instance-name tradebot \
  --port-info fromPort=443,toPort=443,protocol=TCP
aws lightsail get-static-ip --region ap-south-1 --static-ip-name tradebot-ip
```

Ports that should be open: **22** (SSH — restrict to your home IP in the
Lightsail firewall if it's stable), **80** (Caddy's ACME challenge +
HTTP→HTTPS redirect), **443**. Port 8000 stays closed; the app is loopback-only.

The static IP matters twice: DuckDNS points at it, and **Fyers requires order
placement from a whitelisted static IP** (SEBI rule; error `-50` means the IP
isn't whitelisted).

## Step 3 — DuckDNS

1. Sign in at duckdns.org, create a subdomain (e.g. `mybot`).
2. Set its IP to the static IP from step 2.
3. Your domain is `mybot.duckdns.org`. The IP never changes while attached,
   so no update cron is needed.

## Step 4 — Bootstrap the server

SSH in (download the default key pair from the Lightsail console → Account →
SSH keys, or use the browser terminal):

```bash
ssh -i LightsailDefaultKey-ap-south-1.pem ubuntu@<STATIC_IP>
```

Then:

```bash
git clone -b aws https://github.com/thenameispravardhan/AI-News-Trading-Bot.git tradebot
cd tradebot
bash deploy/setup.sh
```

The script prompts for the domain and a dashboard username/password, then
does everything in [deploy/setup.sh](../deploy/setup.sh)'s header comment:
swap, Python 3.11, Node 20, Caddy, venv + requirements, Playwright Chromium,
frontend build, `.env` bootstrap, systemd unit, HTTPS. Re-running it is safe.

## Step 5 — Migrate your local data

The local DB carries your operator-built rules (deleted rules never
resurrect — you'd rebuild by hand), prompts, UI settings and the ML outcome
dataset. From **Windows PowerShell**, with the local bot **stopped**:

```powershell
# stop the service on the server first
ssh -i $KEY ubuntu@$IP "sudo systemctl stop tradebot"

cd "C:\Projects\AI News Trading Bot"
scp -i $KEY data\trading.db      ubuntu@${IP}:~/tradebot/data/
scp -i $KEY data\trading.db-wal  ubuntu@${IP}:~/tradebot/data/   # if present
scp -i $KEY data\trading.db-shm  ubuntu@${IP}:~/tradebot/data/   # if present
scp -i $KEY -r data\scrip_master ubuntu@${IP}:~/tradebot/data/

ssh -i $KEY ubuntu@$IP "cd tradebot && sqlite3 data/trading.db 'PRAGMA wal_checkpoint(TRUNCATE);' && sudo systemctl start tradebot"
```

(`$KEY` = path to the .pem, `$IP` = the static IP.) The `broker_accounts`
row travels with the DB; its stored Fyers token will be expired — reconnect
via the UI (step 7).

## Step 6 — Secrets in `.env`

```bash
nano ~/tradebot/.env
```

Set at minimum:

| Key | Value |
|-----|-------|
| `DEEPSEEK_API_KEY` | your DeepSeek key |
| `FYERS_APP_ID` / `FYERS_SECRET_KEY` | from the Fyers dashboard |
| `FYERS_REDIRECT_URI` | `https://<your-domain>/api/fyers/callback` (setup.sh already set this) |
| `FYERS_POSTBACK_SECRET` | a long random string |
| `TRADING_MODE` | `paper` until the smoke test passes |

Then `sudo systemctl restart tradebot`.

> Config-drift note: `app/config.py` defaults win when a key is absent from
> `.env`, and they differ from `.env.example` for a few keys
> (`POLL_INTERVAL_SECONDS`, `MAX_NEWS_AGE_SECONDS`, `LLM_MAX_TOKENS`,
> `MAX_CONCURRENT_POSITIONS`). Pin anything you care about explicitly.
> UI-saved settings override `.env` and survive restarts.

## Step 7 — Fyers dashboard

At [myapi.fyers.in](https://myapi.fyers.in), on your app:

1. **Redirect URI** → `https://<your-domain>/api/fyers/callback`
   (must match `.env` character-for-character).
2. **Whitelist the static IP** (SEBI static-IP requirement; without it live
   orders fail with `-50`).
3. In the bot UI → accounts, click **Connect Fyers** and complete the OAuth
   popup. Tokens expire daily — this re-connect is a **daily manual step**,
   same as it was locally, just from any browser now.
4. Postback: in the UI's postback config, set the public base URL to
   `https://<your-domain>` so Fyers order webhooks reach
   `/api/fyers/postback`. The cloudflared tunnel from the Windows setup is
   no longer needed.

## Step 8 — Smoke test (stay in paper mode)

1. `https://<your-domain>` → basic-auth login → dashboard loads.
2. The connection dot (WebSocket) is green. If the browser console shows a
   401 on `/ws`, see Troubleshooting.
3. `journalctl -u tradebot -f` shows monitors polling during market hours.
4. Leave it in **paper** for at least a full trading day before typing the
   `LIVE` confirmation.

## Daily operations

| Task | How |
|------|-----|
| Fyers token re-auth (daily, pre-market) | UI → Connect Fyers |
| Watch logs | `journalctl -u tradebot -f` |
| Restart app | `sudo systemctl restart tradebot` |
| Deploy new code | `cd ~/tradebot && bash deploy/update.sh` |
| DB backup (weekdays 18:30 IST) | `crontab -e` → `30 18 * * 1-5 /home/ubuntu/tradebot/deploy/backup.sh >> /home/ubuntu/tradebot/logs/backup.log 2>&1` |
| Pull a backup down to Windows | `scp -i $KEY ubuntu@${IP}:~/tradebot/data/backups/<file> .` |

## Troubleshooting

- **WebSocket fails with 401** — some browsers don't attach cached basic-auth
  credentials to the `wss://` handshake. Fix: in `/etc/caddy/Caddyfile` add a
  `@ws path /ws` matcher with its own `handle @ws { reverse_proxy 127.0.0.1:8000 }`
  block above the authenticated `handle` (the socket is read-only event
  streaming; control stays behind auth), then `sudo systemctl reload caddy`.
- **Fyers error `-50`** — server IP not whitelisted in the Fyers dashboard.
- **OAuth popup lands on an error** — redirect URI mismatch between `.env`
  and the Fyers dashboard, or you started the login from a different origin
  than the one the state was issued on (the CSRF state lives in the app
  process — don't restart the service mid-login).
- **No TLS cert** — the DuckDNS A record isn't pointing at the static IP
  yet, or port 80 is closed (Caddy needs it for the ACME challenge).
  `journalctl -u caddy -n 50`.
- **OOM / app killed during market hours** — check `free -h`; the 2 GB
  bundle + 2 GB swap should hold, but if Chromium spikes recur, upgrade to
  the 4 GB bundle (Lightsail supports snapshotting into a bigger instance).
- **Frontend build dies on the server** — build locally instead
  (`cd frontend; npm run build`) and `scp -r frontend\dist` to
  `~/tradebot/frontend/`.
- **App won't start** — `journalctl -u tradebot -n 100 --no-pager`; check
  `.env` syntax and that `data/` is owned by `ubuntu`
  (`sudo chown -R ubuntu:ubuntu ~/tradebot/data`).

## Security notes

- Basic auth is the **only** thing between the internet and a bot that can
  place real orders. Use a long random password; rotate it by re-running
  `caddy hash-password` and editing `/etc/caddy/Caddyfile`.
- Keep `TRADING_MODE=paper` until you've watched a full market day.
- The kill switch (`POST /api/risk/kill` / UI kill button) and circuit
  breakers work exactly as they do locally; breaker state persists in the DB
  across restarts.
- Restrict SSH (port 22) to your own IP in the Lightsail firewall if
  possible; consider `sudo apt install fail2ban` as a cheap extra.
