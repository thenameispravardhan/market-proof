#!/usr/bin/env bash
# Redeploy the latest code on the server.
#
#   bash deploy/update.sh              # pull + deps + restart
#   bash deploy/update.sh --frontend   # force a dashboard rebuild too
#
# The frontend is rebuilt automatically when the pulled commits touch
# frontend/ or when dist/ is missing.

set -euo pipefail

# Braces: bash reads the whole block before running it, so the `git pull`
# below can rewrite this file without corrupting the run in flight.
{

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

BEFORE="${UPDATE_BEFORE:-$(git rev-parse HEAD)}"
git pull --ff-only
AFTER="$(git rev-parse HEAD)"

# The braces mean this run is the script as it was BEFORE the pull; a pull that
# changed it would skip whatever the new version adds (2026-10-26: the first
# deploy of the C++ build step never built it). So hand over to the new one.
if [ -z "${UPDATE_BEFORE:-}" ] && ! git diff --quiet "$BEFORE" "$AFTER" -- deploy/update.sh; then
    echo "==> deploy/update.sh changed in this pull; running the new version"
    UPDATE_BEFORE="$BEFORE" exec bash deploy/update.sh "$@"
fi

echo "==> Python dependencies"
.venv/bin/pip install -r requirements.txt --quiet

# The backtest's candle loop + Monte Carlo in C++ (app/algo/native.cpp). Optional:
# no g++, a failed build or a failed parity check leaves the Python engine — the
# .so is only kept once native_check proves it answers exactly like Python.
rm -f app/algo/_mc.so                                    # the Monte-Carlo-only build it replaced
CXX="$(command -v g++ || command -v g++-14 || true)"     # the server has only g++-14
if [ -n "$CXX" ]; then
    echo "==> Building app/algo/_native.so"
    if "$CXX" -O2 -std=c++17 -ffp-contract=off -shared -fPIC -o app/algo/_native.so.tmp app/algo/native.cpp \
        && mv app/algo/_native.so.tmp app/algo/_native.so; then
        .venv/bin/python -m app.algo.native_check \
            || { rm -f app/algo/_native.so; echo "    (parity check failed; backtests use the Python engine)"; }
    else
        echo "    (build failed; backtests use the Python engine)"
    fi
fi

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

# Startup takes ~60-65s with a full DB (init + instrument master + services);
# poll up to 3 minutes instead of a single premature probe.
for i in $(seq 1 36); do
    if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
        echo "==> Health check: OK (after ~$((i * 5))s)"
        exit 0
    fi
    sleep 5
done
echo "==> Health check: NOT RESPONDING after 180s — journalctl -u tradebot -n 50 --no-pager" >&2
exit 1
}
