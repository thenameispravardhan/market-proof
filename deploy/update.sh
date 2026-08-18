#!/usr/bin/env bash
# Redeploy the latest code on the server.
#
#   bash deploy/update.sh              # pull + deps + restart
#   bash deploy/update.sh --frontend   # force a dashboard rebuild too
#
# The frontend is rebuilt automatically when the pulled commits touch
# frontend/ or when dist/ is missing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BEFORE="$(git rev-parse HEAD)"
git pull --ff-only
AFTER="$(git rev-parse HEAD)"

echo "==> Python dependencies"
.venv/bin/pip install -r requirements.txt --quiet

REBUILD=0
[ "${1:-}" = "--frontend" ] && REBUILD=1
[ ! -f frontend/dist/index.html ] && REBUILD=1
if [ "$BEFORE" != "$AFTER" ] && ! git diff --quiet "$BEFORE" "$AFTER" -- frontend/; then
    REBUILD=1
fi
if [ "$REBUILD" = "1" ]; then
    echo "==> Rebuilding the React dashboard"
    (cd frontend && npm ci && NODE_OPTIONS=--max-old-space-size=1024 npm run build)
else
    echo "==> No frontend changes; skipping dashboard rebuild"
fi

echo "==> Seeding default prompt templates"
.venv/bin/python scripts/seed_default_prompts.py \
    || echo "    WARNING: seed_default_prompts.py failed; continuing"

echo "==> Restarting tradebot"
# daemon-reload FIRST: `restart` alone re-runs the unit systemd already
# has loaded, so an edited tradebot.service (e.g. the --no-proxy-headers
# guard) silently doesn't take effect.
sudo systemctl daemon-reload
sudo systemctl restart tradebot

# Startup takes ~15s with a full DB (init + instrument master + services);
# poll instead of a single premature probe.
for i in $(seq 1 12); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "==> Health check: OK (after ~$((i * 5))s)"
        exit 0
    fi
    sleep 5
done
echo "==> Health check: NOT RESPONDING after 60s — journalctl -u tradebot -n 50 --no-pager" >&2
exit 1
