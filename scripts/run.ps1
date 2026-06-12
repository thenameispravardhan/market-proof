<#
.SYNOPSIS
  Start the AI News Trading Bot in production mode (no reload).

.DESCRIPTION
  Activates the .venv, bootstraps .env from .env.example if missing,
  seeds the default prompt templates, builds the React dashboard if
  frontend/dist/ is missing, and starts uvicorn on 127.0.0.1:8000.

  The FastAPI app serves the built dashboard from / when frontend/dist
  exists, so the user only needs to open http://127.0.0.1:8000/.

  Use scripts/dev.ps1 instead for the two-terminal live-reload variant.
#>

$ErrorActionPreference = 'Stop'

# Resolve project root from this script's location so the script works
# regardless of the caller's cwd.
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

# ---- build frontend ------------------------------------------------------
$distIndex = Join-Path $ProjectRoot 'frontend\dist\index.html'
if (-not (Test-Path $distIndex)) {
    Write-Host "==> frontend/dist/ missing; building React dashboard"
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        Write-Warning "npm not found on PATH. Skipping frontend build."
        Write-Warning "Install Node 20+ to get the dashboard, then run:"
        Write-Warning "    cd frontend ; npm install ; npm run build"
    } else {
        $frontendDir = Join-Path $ProjectRoot 'frontend'
        if (Test-Path $frontendDir) {
            Push-Location $frontendDir
            try {
                if (-not (Test-Path 'node_modules')) {
                    Write-Host "    npm install ..."
                    npm install | Out-Host
                }
                Write-Host "    npm run build ..."
                npm run build | Out-Host
            } finally {
                Pop-Location
            }
        } else {
            Write-Warning "frontend/ directory not found; skipping dashboard build."
        }
    }
} else {
    Write-Host "==> frontend/dist/ present; skipping build"
}

# ---- start uvicorn -------------------------------------------------------
Write-Host ""
Write-Host "==> Starting uvicorn on 127.0.0.1:8000"
Write-Host "    Open http://127.0.0.1:8000/ in your browser"
Write-Host "    Press Ctrl-C to stop"
Write-Host ""

$uvicorn = Get-Command uvicorn -ErrorAction SilentlyContinue
if ($null -eq $uvicorn) {
    Write-Error "uvicorn not found on PATH. Did 'pip install -r requirements.txt' run inside .venv?"
    exit 1
}

uvicorn app.main:app --host 127.0.0.1 --port 8000
