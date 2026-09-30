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

# 0. Check every dependency: install what is missing, skip what is already present.
function Install-FromWeb([string]$Name, [string]$Url) {
    Write-Status $Name "missing - installing..." Yellow
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm $Url | iex"
    foreach ($bin in "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin") {
        if (Test-Path $bin) { $env:Path = "$bin;$env:Path" }
    }
}

function Stop-Setup([string]$Name, [string]$Help) {
    Write-Status $Name "install failed - $Help" Red
    Read-Host "  Press Enter to close"
    exit 1
}

# uv (manages Python 3.14 and the app's packages)
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Status "uv" "found - skipped" Green
} else {
    Install-FromWeb "uv" "https://astral.sh/uv/install.ps1"
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { Stop-Setup "uv" "see https://docs.astral.sh/uv/" }
    Write-Status "uv" "installed" Green
}

# Python + packages: uv sync is a quick no-op when everything is already installed.
$venv = Join-Path $scriptDir ".venv"
$hadVenv = Test-Path $venv
if (-not $hadVenv) { Write-Status "Python + packages" "missing - installing (1-3 min)..." Yellow }
Push-Location $scriptDir
uv sync --quiet
$syncOk = $LASTEXITCODE -eq 0
Pop-Location
if ($syncOk) {
    Write-Status "Python + packages" $(if ($hadVenv) { "found - up to date" } else { "installed" }) Green
} elseif ($hadVenv) {
    # Usually a running server holding files open; the existing environment still works.
    Write-Status "Python + packages" "update skipped (server running?) - using existing install" Yellow
} else {
    Stop-Setup "Python + packages" "see the uv output above"
}

# Git (Claude Code on Windows runs its shell tools through Git Bash)
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Status "Git" "found - skipped" Green
} elseif (Get-Command winget -ErrorAction SilentlyContinue) {
    Write-Status "Git" "missing - installing..." Yellow
    winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
    $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
    Write-Status "Git" $(if (Get-Command git -ErrorAction SilentlyContinue) { "installed" } else { "install failed - https://git-scm.com/download/win" }) Yellow
} else {
    Write-Status "Git" "missing - install from https://git-scm.com/download/win" Yellow
}

# Claude Code (the agent engine behind the chat window)
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Status "Claude Code" "found - skipped" Green
} else {
    Install-FromWeb "Claude Code" "https://claude.ai/install.ps1"
    if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Stop-Setup "Claude Code" "see https://docs.claude.com/claude-code" }
    Write-Status "Claude Code" "installed" Green
}

# Starter model config (NVIDIA NIM). Students only add their API key on the setup page.
$fccDir = Join-Path $env:USERPROFILE ".fcc"
$starterEnv = Join-Path $fccDir ".env"
if (Test-Path $starterEnv) {
    Write-Status "Model config" "found - skipped" Green
} else {
    New-Item -ItemType Directory -Force $fccDir | Out-Null
    $nano = "nvidia_nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    $ultra = "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"
    $flash = "nvidia_nim/deepseek-ai/deepseek-v4.1-flash"
    $starter = @(
        "# Managed by Free Claude Code.",
        "# Edit settings in /admin when possible.",
        "FCC_CONFIG_SCHEMA=1",
        "MODEL=`"$nano`"",
        "MODEL_FABLE=`"$nano`"",
        "MODEL_OPUS=`"$nano`"",
        "MODEL_SONNET=`"$nano`"",
        "MODEL_HAIKU=`"$nano`"",
        "MODEL_FALLBACKS=`"$ultra,$flash`"",
        "CHAT_MODELS=`"$nano,$ultra,$flash,nvidia_nim/moonshotai/kimi-k3,nvidia_nim/z-ai/glm-5.3`"",
        "FCC_OPEN_BROWSER=false",
        "# Free-tier queues can stall; fail over to the next model after 45s.",
        "HTTP_FIRST_BYTE_TIMEOUT=45"
    )
    [IO.File]::WriteAllText($starterEnv, ($starter -join "`n") + "`n")
    Write-Status "Model config" "created (default: Nemotron 3 Nano)" Green
}

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
$port = 8082
$envFile = Join-Path $env:USERPROFILE ".fcc\.env"
if (Test-Path $envFile) {
    $match = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*["'']?(\d+)' | Select-Object -First 1
    if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
}
try {
    $connected = (Invoke-RestMethod "http://127.0.0.1:$port/chat/api/models" -TimeoutSec 5).models.Count
} catch { $connected = 1 }
if ($connected -eq 0) {
    # No model has an API key yet: open the setup page on top of the chat window.
    & (Join-Path $scriptDir "Claude-Desktop.ps1") -Page admin
    Write-Status "Model setup" "add your API key in the window that opened" Yellow
}
Write-Host ""
Write-Host "  Chat:  http://127.0.0.1:$port/chat" -ForegroundColor Gray
Write-Host "  Admin: http://127.0.0.1:$port/admin" -ForegroundColor Gray
Write-Host "  Stop:  double-click Stop-All.bat" -ForegroundColor Gray
Start-Sleep -Seconds 4
