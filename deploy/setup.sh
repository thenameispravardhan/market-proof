#!/usr/bin/env bash
# One-shot server bootstrap for the AI News Trading Bot on AWS Lightsail
# (Ubuntu 24.04, ap-south-1). Run it from inside the cloned repo:
#
#   git clone -b aws https://github.com/thenameispravardhan/AI-News-Trading-Bot.git tradebot
#   cd tradebot && bash deploy/setup.sh
#
# Idempotent — safe to re-run. Uses sudo (Lightsail's ubuntu user has it).
#
# What it does:
#   1. 2 GB swap file (headroom for Chromium + the frontend build on a 2 GB box)
#   2. System timezone -> Asia/Kolkata (app timestamps stay UTC internally)
#   3. System packages: Python 3.11 (deadsnakes), Node 20 (NodeSource),
#      Caddy (official repo), sqlite3, git
#   4. Python venv + requirements + Playwright Chromium with system libs
#   5. React dashboard build (frontend/dist/) if missing
#   6. .env bootstrap with the production Fyers redirect URI
#   7. systemd unit (tradebot.service) — app bound to 127.0.0.1:8000
#   8. Caddy: HTTPS on your domain + basic auth on everything except the
#      Fyers postback (which has its own secret token)
#
# Full runbook: docs/DEPLOY_AWS.md

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_USER="$(id -un)"
cd "$PROJECT_ROOT"

echo "==> Project root: $PROJECT_ROOT (running as: $RUN_USER)"

# ---- inputs ---------------------------------------------------------------
# Can be pre-set as env vars for a non-interactive run:
#   DOMAIN=mybot.duckdns.org BASIC_USER=me BASIC_PASS=... bash deploy/setup.sh
DOMAIN="${DOMAIN:-}"
BASIC_USER="${BASIC_USER:-}"
BASIC_PASS="${BASIC_PASS:-}"
if [ -z "$DOMAIN" ]; then
    read -rp "Domain that points at this server (e.g. mybot.duckdns.org): " DOMAIN
fi
if [ -z "$BASIC_USER" ]; then
    read -rp "Dashboard username (basic auth): " BASIC_USER
fi
if [ -z "$BASIC_PASS" ]; then
    read -rsp "Dashboard password (input hidden): " BASIC_PASS
    echo
fi
if [ -z "$DOMAIN" ] || [ -z "$BASIC_USER" ] || [ -z "$BASIC_PASS" ]; then
    echo "ERROR: domain, username and password are all required." >&2
    exit 1
fi

# ---- 1. swap --------------------------------------------------------------
if ! sudo swapon --show | grep -q '^/swapfile'; then
    echo "==> Creating 2G swap file"
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
    sudo swapon /swapfile
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
else
    echo "==> Swap already configured"
fi

# ---- 2. timezone ----------------------------------------------------------
echo "==> Setting system timezone to Asia/Kolkata"
sudo timedatectl set-timezone Asia/Kolkata

# ---- 3. system packages ---------------------------------------------------
echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get install -y software-properties-common curl git sqlite3 \
    debian-keyring debian-archive-keyring apt-transport-https

# Python 3.11 (Ubuntu 24.04 ships 3.12; the project targets 3.11)
if ! command -v python3.11 >/dev/null 2>&1; then
    echo "==> Installing Python 3.11 (deadsnakes PPA)"
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get install -y python3.11 python3.11-venv python3.11-dev
fi

# Node 20 (needed only to build the frontend)
if ! command -v node >/dev/null 2>&1 \
   || ! node -e 'process.exit(+process.versions.node.split(".")[0] >= 20 ? 0 : 1)'; then
    echo "==> Installing Node 20 (NodeSource)"
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

# Caddy (official repo)
if ! command -v caddy >/dev/null 2>&1; then
    echo "==> Installing Caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo apt-get update -y
    sudo apt-get install -y caddy
fi

# ---- 4. python venv + deps ------------------------------------------------
echo "==> Python venv + dependencies"
if [ ! -f .venv/bin/activate ]; then
    python3.11 -m venv .venv
fi
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt

echo "==> Playwright Chromium (NSE/BSE scrape fallback)"
sudo "$PROJECT_ROOT/.venv/bin/python" -m playwright install-deps chromium
.venv/bin/python -m playwright install chromium

# ---- 5. frontend build ----------------------------------------------------
if [ ! -f frontend/dist/index.html ]; then
    echo "==> Building the React dashboard (this takes a few minutes)"
    (cd frontend && npm ci && NODE_OPTIONS=--max-old-space-size=1024 npm run build)
else
    echo "==> frontend/dist present; skipping build (deploy/update.sh --frontend rebuilds)"
fi

# ---- 6. .env --------------------------------------------------------------
if [ ! -f .env ]; then
    echo "==> Creating .env from .env.example"
    cp .env.example .env
    # The Fyers OAuth redirect must match the Fyers dashboard entry
    # character-for-character.
    sed -i "s|^FYERS_REDIRECT_URI=.*|FYERS_REDIRECT_URI=https://${DOMAIN}/api/fyers/callback|" .env
    echo ""
    echo "    >>> EDIT .env NOW OR AFTER SETUP: set DEEPSEEK_API_KEY,"
    echo "    >>> FYERS_APP_ID, FYERS_SECRET_KEY, FYERS_POSTBACK_SECRET."
    echo ""
else
    echo "==> .env exists; leaving it alone."
    echo "    Verify: FYERS_REDIRECT_URI=https://${DOMAIN}/api/fyers/callback"
fi

# ---- 7. seed default prompts ----------------------------------------------
echo "==> Seeding default prompt templates"
.venv/bin/python scripts/seed_default_prompts.py \
    || echo "    WARNING: seed_default_prompts.py failed; continuing"

# ---- 8. systemd unit ------------------------------------------------------
echo "==> Installing systemd unit: tradebot.service"
sed -e "s|__PROJECT_ROOT__|${PROJECT_ROOT}|g" \
    -e "s|__RUN_USER__|${RUN_USER}|g" \
    deploy/tradebot.service | sudo tee /etc/systemd/system/tradebot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable tradebot
sudo systemctl restart tradebot

# ---- 9. Caddy -------------------------------------------------------------
echo "==> Configuring Caddy (HTTPS + basic auth) for ${DOMAIN}"
BASIC_HASH="$(caddy hash-password --plaintext "$BASIC_PASS")"
sed -e "s|__DOMAIN__|${DOMAIN}|g" \
    -e "s|__BASIC_USER__|${BASIC_USER}|g" \
    -e "s|__BASIC_HASH__|${BASIC_HASH}|g" \
    deploy/Caddyfile | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo systemctl enable caddy
sudo systemctl reload caddy || sudo systemctl restart caddy

# ---- summary --------------------------------------------------------------
echo ""
echo "=================================================================="
echo " Setup complete."
echo ""
echo "   App:     http://127.0.0.1:8000 (loopback only, via systemd)"
echo "   Public:  https://${DOMAIN}  (basic auth: ${BASIC_USER})"
echo ""
sleep 3
if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo "   Health check: OK (http://127.0.0.1:8000/api/health)"
else
    echo "   Health check: NOT RESPONDING yet — inspect with:"
    echo "     journalctl -u tradebot -n 50 --no-pager"
fi
echo ""
echo " Next steps (see docs/DEPLOY_AWS.md for details):"
echo "   1. Edit .env secrets, then: sudo systemctl restart tradebot"
echo "   2. Upload your local data/trading.db (runbook step 5)"
echo "   3. Fyers dashboard: set redirect URI to"
echo "        https://${DOMAIN}/api/fyers/callback"
echo "      and whitelist this server's static IP (SEBI requirement)."
echo "   4. Optional nightly DB backup — add to crontab -e:"
echo "        30 18 * * 1-5 ${PROJECT_ROOT}/deploy/backup.sh"
echo "=================================================================="
