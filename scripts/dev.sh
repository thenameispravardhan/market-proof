#!/usr/bin/env bash
# Start the AI News Trading Bot in development mode (live-reload).
#
# This script is identical to run.sh EXCEPT it runs uvicorn with --reload
# and prints an extra instruction telling the user to start the Vite
# dev server in a SECOND terminal for hot-reloading the React UI.
#
# Terminal 1 (this script):
#     uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
#
# Terminal 2:
#     cd frontend ; npm run dev
#     open http://localhost:5173/
#
# Use scripts/run.sh for the single-process production variant.

set -euo pipefail

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

# ---- npm install (no build) ---------------------------------------------
# In dev mode the Vite dev server (Terminal 2) serves the React UI.
# We still want node_modules so `npm run dev` works immediately.
if command -v npm >/dev/null 2>&1 && [ -d "$PROJECT_ROOT/frontend" ]; then
    if [ ! -d "$PROJECT_ROOT/frontend/node_modules" ]; then
        echo "==> frontend/node_modules missing; running npm install"
        ( cd "$PROJECT_ROOT/frontend" && npm install )
    else
        echo "==> frontend/node_modules present; skipping npm install"
    fi
else
    echo "==> WARNING: npm or frontend/ not found; you'll need Node 20+ to run the dashboard."
fi

# ---- start uvicorn with --reload ----------------------------------------
echo ""
echo "==> Starting uvicorn (--reload) on 127.0.0.1:8000"
echo "    Open http://127.0.0.1:8000/  (production-style UI, served by FastAPI)"
echo ""
echo "    >>> In a SECOND terminal, run:"
echo "        cd frontend"
echo "        npm run dev"
echo "    ...and open http://localhost:5173/  for the live-reload dev server."
echo ""
echo "    Press Ctrl-C to stop uvicorn."
echo ""

if ! command -v uvicorn >/dev/null 2>&1; then
    echo "==> ERROR: uvicorn not found on PATH. Did 'pip install -r requirements.txt' run inside .venv?" >&2
    exit 1
fi

exec uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
