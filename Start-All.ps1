# Free Claude Code - start every local server, then open the Claude-style app window.
#   1. Ollama            (local models, port 11434)       - if installed
#   2. NVIDIA proxy      (~/.nvidia-proxy/proxy.py, 8787) - if present
#   3. FCC server + chat window (via Claude-Desktop.ps1, port from ~/.fcc/.env, default 8082)
# Servers already running are left alone. Double-click Start-All.bat to run this.
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
foreach ($bin in "$env:USERPROFILE\.local\bin", "$env:APPDATA\npm") {
    if (Test-Path $bin) { $env:Path = "$bin;$env:Path" }
}

function Test-Port([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Write-Status([string]$Name, [string]$State, [string]$Color) {
    Write-Host ("  {0,-22} {1}" -f $Name, $State) -ForegroundColor $Color
}

Write-Host ""
Write-Host "  Starting Claude (Free Claude Code)" -ForegroundColor Cyan
Write-Host "  ----------------------------------" -ForegroundColor Cyan

# 1. Ollama
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
if (-not $ollama) {
    Write-Status "Ollama" "not installed (skipped)" DarkGray
} elseif (Test-Port 11434) {
    Write-Status "Ollama" "already running" Green
} else {
    Start-Process -FilePath $ollama.Source -ArgumentList "serve" -WindowStyle Hidden
    Write-Status "Ollama" "started" Green
}

# 2. NVIDIA proxy
$proxyScript = Join-Path $env:USERPROFILE ".nvidia-proxy\proxy.py"
if (-not (Test-Path $proxyScript)) {
    Write-Status "NVIDIA proxy" "not found (skipped)" DarkGray
} elseif (Test-Port 8787) {
    Write-Status "NVIDIA proxy" "already running" Green
} else {
    $pythonw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) {
        $pythonw = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\*\pythonw.exe" -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if ($pythonw) {
        Start-Process -FilePath $pythonw -ArgumentList "`"$proxyScript`"" -WindowStyle Hidden
        Write-Status "NVIDIA proxy" "started" Green
    } else {
        Write-Status "NVIDIA proxy" "pythonw not found (skipped)" Yellow
    }
}

# 3. FCC server + app window (starts the server hidden and waits until /chat answers)
Write-Status "FCC server + Claude" "starting..." Cyan
& (Join-Path $scriptDir "Claude-Desktop.ps1")
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    Write-Status "FCC server + Claude" "failed - see ~\.fcc\logs\server.log" Red
    Start-Sleep -Seconds 8
    exit 1
}
Write-Status "FCC server + Claude" "running - window opened" Green
Write-Host ""
Write-Host "  Chat:  http://127.0.0.1:8082/chat" -ForegroundColor Gray
Write-Host "  Admin: http://127.0.0.1:8082/admin" -ForegroundColor Gray
Write-Host "  Stop:  run stop-fcc.ps1" -ForegroundColor Gray
Start-Sleep -Seconds 4
