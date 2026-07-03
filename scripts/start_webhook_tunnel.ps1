<#
.SYNOPSIS
  Start (or repair) the public tunnel for the Fyers order postback.

.DESCRIPTION
  Fyers needs a publicly reachable HTTPS URL to POST order updates to,
  but the bot binds to 127.0.0.1 only. This script:

    1. Checks whether the currently registered PUBLIC_BASE_URL still
       responds — if it does, nothing to do.
    2. Otherwise starts a cloudflared quick tunnel DETACHED (it keeps
       running after this script and your terminal close), pointing at
       the local bot on 127.0.0.1:8000.
    3. Reads the new public URL from the tunnel log, registers it with
       the bot (POST /api/fyers/postback/config), and verifies the
       round-trip through the public URL.
    4. Prints the postback URL to paste into the Fyers API dashboard.

  Quick tunnels get a NEW random URL every time cloudflared restarts
  (e.g. after a reboot) — re-run this script then, and re-paste the
  printed URL into the Fyers dashboard.

  DNS NOTE: a fresh trycloudflare.com name can take minutes to reach
  your ISP's resolver, so verification resolves via Cloudflare DNS
  (1.1.1.1) and pins the IP with curl --resolve. "It doesn't open in
  my browser yet" right after a run is normal and does NOT affect
  Fyers — their resolvers pick it up independently.

.NOTES
  Requires cloudflared on PATH (https://developers.cloudflare.com/cloudflared/).
  The bot must already be running on 127.0.0.1:8000.
#>

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir '..')
$LogFile = Join-Path $ProjectRoot 'logs\cloudflared.log'
$Bot = 'http://127.0.0.1:8000'

function Test-PublicUrl {
    <# True when https://<host>/api/health answers 200 through the
       Cloudflare edge. Resolves via 1.1.1.1 + curl --resolve so a
       stale local/ISP resolver can't produce a false negative. #>
    param([string]$BaseUrl, [int]$Attempts = 5, [int]$DelaySeconds = 5)
    $uri = [Uri]$BaseUrl
    $hostName = $uri.Host
    for ($i = 1; $i -le $Attempts; $i++) {
        $ip = $null
        try {
            $ip = (Resolve-DnsName $hostName -Type A -Server 1.1.1.1 -ErrorAction Stop |
                   Where-Object { $_.Type -eq 'A' } | Select-Object -First 1).IPAddress
        } catch {}
        if ($ip) {
            $code = & curl.exe -s -o NUL -w '%{http_code}' --max-time 10 `
                --resolve "${hostName}:443:${ip}" "https://$hostName/api/health"
            if ($code -eq '200') { return $true }
        }
        if ($i -lt $Attempts) { Start-Sleep -Seconds $DelaySeconds }
    }
    return $false
}

# ---- 0. the bot must be up ------------------------------------------------
try {
    Invoke-RestMethod "$Bot/api/health" -TimeoutSec 5 | Out-Null
} catch {
    Write-Error "Bot is not responding on $Bot — start it first (scripts/run.ps1 or dev.ps1)."
}

# ---- 1. is the registered URL still alive? --------------------------------
$config = Invoke-RestMethod "$Bot/api/fyers/postback/config" -TimeoutSec 10
if ($config.public_base_url) {
    if (Test-PublicUrl $config.public_base_url -Attempts 2 -DelaySeconds 3) {
        Write-Host "==> Tunnel already working: $($config.public_base_url)"
        Write-Host "==> Fyers postback URL (already registered):"
        Write-Host "    $($config.url)"
        exit 0
    }
    Write-Host "==> Registered URL is dead ($($config.public_base_url)) — starting a fresh tunnel"
}

# ---- 2. start cloudflared detached ----------------------------------------
if (-not (Get-Command cloudflared -ErrorAction SilentlyContinue)) {
    Write-Error "cloudflared not found on PATH. Install it or add it to PATH."
}

# Reap orphaned quick tunnels from previous runs (their URLs are dead
# anyway once the registered URL stops responding). Only kills
# cloudflared processes that point at OUR bot URL.
Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
    Where-Object { $_.CommandLine -match [regex]::Escape($Bot) } |
    ForEach-Object {
        Write-Host "==> Stopping stale cloudflared (pid $($_.ProcessId))"
        try { Stop-Process -Id $_.ProcessId -Force -Confirm:$false } catch {}
    }

New-Item -ItemType Directory -Force (Split-Path $LogFile) | Out-Null
if (Test-Path $LogFile) { Clear-Content $LogFile }

# cloudflared writes its banner (with the public URL) to stderr.
$proc = Start-Process -FilePath 'cloudflared' `
    -ArgumentList 'tunnel', '--url', $Bot `
    -RedirectStandardError $LogFile `
    -RedirectStandardOutput (Join-Path $ProjectRoot 'logs\cloudflared.out.log') `
    -WindowStyle Hidden -PassThru
Write-Host "==> cloudflared started (pid $($proc.Id)); waiting for the public URL…"

# ---- 3. wait for the URL in the log ---------------------------------------
$publicUrl = $null
foreach ($i in 1..30) {
    Start-Sleep -Seconds 1
    if (Test-Path $LogFile) {
        $m = Select-String -Path $LogFile -Pattern 'https://[a-z0-9-]+\.trycloudflare\.com' -AllMatches |
             Select-Object -First 1
        if ($m) { $publicUrl = $m.Matches[0].Value; break }
    }
}
if (-not $publicUrl) {
    Write-Error "No tunnel URL appeared in $LogFile after 30s — see the log for errors."
}
Write-Host "==> Public URL: $publicUrl"

# ---- 4. register with the bot + verify round-trip -------------------------
$body = @{ public_base_url = $publicUrl } | ConvertTo-Json
$config = Invoke-RestMethod "$Bot/api/fyers/postback/config" -Method Post `
    -ContentType 'application/json' -Body $body

# Verify Fyers' path in from the outside: edge -> tunnel -> local bot.
# Fresh names can take a while at the edge; be patient (up to ~2 min).
Write-Host "==> Verifying the public round-trip (can take a minute for a fresh URL)…"
if (-not (Test-PublicUrl $publicUrl -Attempts 20 -DelaySeconds 6)) {
    Write-Error "Tunnel is up but $publicUrl/api/health does not reach the bot. See $LogFile."
}

Write-Host ""
Write-Host "==> VERIFIED: $publicUrl reaches the local bot."
Write-Host ""
Write-Host "==> Paste this Postback URL into the Fyers API dashboard (myapi.fyers.in, your app's settings):"
Write-Host ""
Write-Host "    $($config.url)"
Write-Host ""
Write-Host "    Preference/trigger: $($config.preference)"
Write-Host ""
Write-Host "NOTE: the URL changes every time cloudflared restarts (e.g. after a reboot)."
Write-Host "      Re-run this script then, and update the Fyers dashboard with the new URL."
Write-Host "NOTE: your own browser may not open the URL for a few minutes until your ISP's"
Write-Host "      DNS catches up — that's cosmetic; Fyers resolves it independently."
