<#
.SYNOPSIS
  Start the AI News Trading Bot in development mode (live-reload).

.DESCRIPTION
  This script is identical to run.ps1 EXCEPT it runs uvicorn with
  --reload and prints an extra instruction telling the user to start
  the Vite dev server in a SECOND terminal for hot-reloading the
  React UI.

  Terminal 1 (this script):
      uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

  Terminal 2:
      cd frontend ; npm run dev
      open http://localhost:5173/

  Use scripts/run.ps1 for the single-process production variant.
#>

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir '..')
Set-Location $ProjectRoot

Write-Host "==> Project root: $ProjectRoot"

# ---- venv ----------------------------------------------------------------
$venvActivate = Join-Path $ProjectRoot '.venv\Scripts\Activate.ps1'
if (-not (Test-Path $venvActivate)) {
    Write-Host "==> .venv not found at $venvActivate"
    Write-Host "    Run: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt"
    exit 1
}
Write-Host "==> Activating venv"
. $venvActivate

# ---- .env ----------------------------------------------------------------
$envFile = Join-Path $ProjectRoot '.env'
$envExample = Join-Path $ProjectRoot '.env.example'
if (-not (Test-Path $envFile)) {
    if (Test-Path $envExample) {
        Write-Host "==> .env not found; copying .env.example to .env"
        Copy-Item $envExample $envFile
        Write-Host "    >>> EDIT .env AND SET YOUR DEEPSEEK_API_KEY (and FYERS_* for live) <<<"
    } else {
        Write-Warning ".env and .env.example both missing; proceeding with process env"
    }
}

# ---- seed prompt templates ----------------------------------------------
Write-Host "==> Seeding default prompt templates"
try {
    python scripts/seed_default_prompts.py | Out-Host
} catch {
    Write-Warning "seed_default_prompts.py failed: $_"
}

# ---- npm install (no build) ---------------------------------------------
# In dev mode the Vite dev server (Terminal 2) serves the React UI.
# We still want node_modules so `npm run dev` works immediately.
$frontendDir = Join-Path $ProjectRoot 'frontend'
$npm = Get-Command npm -ErrorAction SilentlyContinue
if ($null -ne $npm -and (Test-Path $frontendDir)) {
    if (-not (Test-Path (Join-Path $frontendDir 'node_modules'))) {
        Write-Host "==> frontend/node_modules missing; running npm install"
        Push-Location $frontendDir
        try { npm install | Out-Host } finally { Pop-Location }
    } else {
        Write-Host "==> frontend/node_modules present; skipping npm install"
    }
} else {
    Write-Warning "npm or frontend/ not found; you'll need Node 20+ to run the dashboard."
}

# ---- start uvicorn with --reload ----------------------------------------
Write-Host ""
Write-Host "==> Starting uvicorn (--reload) on 127.0.0.1:8000"
Write-Host "    Open http://127.0.0.1:8000/  (production-style UI, served by FastAPI)"
Write-Host ""
Write-Host "    >>> In a SECOND terminal, run:"
Write-Host "        cd frontend"
Write-Host "        npm run dev"
Write-Host "    ...and open http://localhost:5173/  for the live-reload dev server."
Write-Host ""
Write-Host "    Press Ctrl-C to stop uvicorn."
Write-Host ""

$uvicorn = Get-Command uvicorn -ErrorAction SilentlyContinue
if ($null -eq $uvicorn) {
    Write-Error "uvicorn not found on PATH. Did 'pip install -r requirements.txt' run inside .venv?"
    exit 1
}

uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
