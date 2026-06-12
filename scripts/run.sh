#!/usr/bin/env bash
# Start the AI News Trading Bot in production mode (no reload).
#
# Activates the .venv, bootstraps .env from .env.example if missing,
# seeds the default prompt templates, builds the React dashboard if
# frontend/dist/ is missing, and starts uvicorn on 127.0.0.1:8000.
#
# Use scripts/dev.sh instead for the two-terminal live-reload variant.

set -euo pipefail

# Resolve project root from this script's location so the script works
# regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "==> Project root: $PROJECT_ROOT"

# ---- venv ----------------------------------------------------------------
VENV_ACTIVATE="$PROJECT_ROOT/.venv/bin/activate"
if [ ! -f "$VENV_ACTIVATE" ]; then
    echo "==> .venv not found at $VENV_ACTIVATE"
    echo "    Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
# shellcheck disable=SC1090
echo "==> Activating venv"
. "$VENV_ACTIVATE"

# ---- .env ----------------------------------------------------------------
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"
if [ ! -f "$ENV_FILE" ]; then
    if [ -f "$ENV_EXAMPLE" ]; then
        echo "==> .env not found; copying .env.example to .env"
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        echo "    >>> EDIT .env AND SET YOUR DEEPSEEK_API_KEY (and FYERS_* for live) <<<"
    else
        echo "==> .env and .env.example both missing; proceeding with process env"
    fi
fi

# ---- seed prompt templates ----------------------------------------------
echo "==> Seeding default prompt templates"
if ! python scripts/seed_default_prompts.py; then
    echo "==> WARNING: seed_default_prompts.py failed; continuing"
fi

# ---- build frontend ------------------------------------------------------
DIST_INDEX="$PROJECT_ROOT/frontend/dist/index.html"
if [ ! -f "$DIST_INDEX" ]; then
    echo "==> frontend/dist/ missing; building React dashboard"
    if ! command -v npm >/dev/null 2>&1; then
        echo "==> WARNING: npm not found on PATH. Skipping frontend build."
        echo "    Install Node 20+ to get the dashboard, then run:"
        echo "        cd frontend ; npm install ; npm run build"
    elif [ -d "$PROJECT_ROOT/frontend" ]; then
        pushd "$PROJECT_ROOT/frontend" >/dev/null
        if [ ! -d "node_modules" ]; then
            echo "    npm install ..."
            npm install
        fi
        echo "    npm run build ..."
        npm run build
        popd >/dev/null
    else
        echo "==> WARNING: frontend/ directory not found; skipping dashboard build."
    fi
else
    echo "==> frontend/dist/ present; skipping build"
fi

# ---- start uvicorn -------------------------------------------------------
echo ""
echo "==> Starting uvicorn on 127.0.0.1:8000"
echo "    Open http://127.0.0.1:8000/ in your browser"
echo "    Press Ctrl-C to stop"
echo ""

if ! command -v uvicorn >/dev/null 2>&1; then
    echo "==> ERROR: uvicorn not found on PATH. Did 'pip install -r requirements.txt' run inside .venv?" >&2
    exit 1
fi

exec uvicorn app.main:app --host 127.0.0.1 --port 8000
