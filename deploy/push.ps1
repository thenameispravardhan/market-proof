# One-command redeploy FROM your Windows dev machine TO the live server.
#
#   .\deploy\push.ps1                 # git push (if needed) + run update.sh on the server
#   .\deploy\push.ps1 -Frontend       # also force a dashboard rebuild
#   .\deploy\push.ps1 -NoGit          # skip the local git push; just redeploy the server
#
# Reads the concrete server address from deploy/target.local.env (gitignored;
# see target.local.env.example). PowerShell twin of deploy/push.sh.
#
# NOTE: update.sh ends with `sudo systemctl restart tradebot` (~15s downtime).
# Avoid market hours (09:15-15:30 IST) while a position is open.

param(
    [switch]$Frontend,
    [switch]$NoGit
)

$ErrorActionPreference = "Stop"
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$EnvFile     = Join-Path $ScriptDir "target.local.env"

if (-not (Test-Path $EnvFile)) {
    Write-Error "target.local.env not found. Copy deploy/target.local.env.example to deploy/target.local.env and fill it in."
    exit 1
}

# Parse the KEY=value file.
$cfg = @{}
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    $cfg[$k.Trim()] = $v.Trim()
}

$sshKey = $cfg['TRADEBOT_SSH_KEY'] -replace '^~', $env:USERPROFILE
$host_  = if ($cfg['TRADEBOT_SERVER_IP']) { $cfg['TRADEBOT_SERVER_IP'] } else { $cfg['TRADEBOT_DOMAIN'] }
$user   = if ($cfg['TRADEBOT_SSH_USER']) { $cfg['TRADEBOT_SSH_USER'] } else { "ubuntu" }
$remote = if ($cfg['TRADEBOT_REMOTE_DIR']) { $cfg['TRADEBOT_REMOTE_DIR'] } else { "~/tradebot" }

if (-not (Test-Path $sshKey)) {
    Write-Error "SSH key not found at $sshKey (from TRADEBOT_SSH_KEY)."
    exit 1
}

if (-not $NoGit) {
    $branch = (git -C $ProjectRoot rev-parse --abbrev-ref HEAD).Trim()
    Write-Host "==> Pushing $branch to origin"
    git -C $ProjectRoot push origin $branch
}

$frontendArg = if ($Frontend) { "--frontend" } else { "" }
Write-Host "==> Deploying to ${user}@${host_}:${remote}"
ssh -i $sshKey -o StrictHostKeyChecking=accept-new "${user}@${host_}" "cd ${remote} && bash deploy/update.sh ${frontendArg}"

$dash = if ($cfg['TRADEBOT_DOMAIN']) { $cfg['TRADEBOT_DOMAIN'] } else { $host_ }
Write-Host "==> Done. Dashboard: https://$dash"
