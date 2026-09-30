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

# 0. First-run setup on a fresh PC: uv (Python + dependencies), Claude Code, starter model config.
function Install-FromWeb([string]$Name, [string]$Url) {
    Write-Status $Name "installing (first run only)..." Yellow
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm $Url | iex"
    foreach ($bin in "$env:USERPROFILE\.local\bin", "$env:USERPROFILE\.cargo\bin") {
        if (Test-Path $bin) { $env:Path = "$bin;$env:Path" }
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Install-FromWeb "uv" "https://astral.sh/uv/install.ps1"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Status "uv" "install failed - see https://docs.astral.sh/uv/" Red
    Read-Host "  Press Enter to close"
    exit 1
}
Write-Status "Python + packages" "checking (first run downloads ~1-3 min)..." Cyan
Push-Location $scriptDir
uv sync --quiet
$syncOk = $LASTEXITCODE -eq 0
Pop-Location
if (-not $syncOk -and (Test-Path (Join-Path $scriptDir ".venv"))) {
    # Usually a running server holding files open; the existing environment still works.
    Write-Status "Python + packages" "update skipped (server running?) - using existing install" Yellow
} elseif (-not $syncOk) {
    Write-Status "Python + packages" "uv sync failed (see output above)" Red
    Read-Host "  Press Enter to close"
    exit 1
} else {
    Write-Status "Python + packages" "ready" Green
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    # Claude Code on Windows runs its shell tools through Git Bash.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Status "Git" "installing (first run only)..." Yellow
        winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
    } else {
        Write-Status "Git" "missing - install from https://git-scm.com/download/win" Yellow
    }
}
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Install-FromWeb "Claude Code" "https://claude.ai/install.ps1"
}
if (Get-Command claude -ErrorAction SilentlyContinue) {
    Write-Status "Claude Code" "ready" Green
} else {
    Write-Status "Claude Code" "install failed - see https://docs.claude.com/claude-code" Red
}

$fccDir = Join-Path $env:USERPROFILE ".fcc"
$starterEnv = Join-Path $fccDir ".env"
if (-not (Test-Path $starterEnv)) {
    # Starter models (NVIDIA NIM). Students only add their API key on the setup page.
    New-Item -ItemType Directory -Force $fccDir | Out-Null
    $ultra = "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"
    $nano = "nvidia_nim/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning"
    $flash = "nvidia_nim/deepseek-ai/deepseek-v4.1-flash"
    $starter = @(
        "# Managed by Free Claude Code.",
        "# Edit settings in /admin when possible.",
        "FCC_CONFIG_SCHEMA=1",
        "MODEL=`"$ultra`"",
        "MODEL_OPUS=`"$ultra`"",
        "MODEL_SONNET=`"$ultra`"",
        "MODEL_HAIKU=`"$nano`"",
        "MODEL_FALLBACKS=`"$nano,$flash`"",
        "CHAT_MODELS=`"$ultra,$nano,$flash,nvidia_nim/moonshotai/kimi-k3,nvidia_nim/z-ai/glm-5.3`"",
        "FCC_OPEN_BROWSER=false",
        "# Free-tier queues can stall; fail over to the next model after 45s.",
        "HTTP_FIRST_BYTE_TIMEOUT=45"
    )
    [IO.File]::WriteAllText($starterEnv, ($starter -join "`n") + "`n")
    Write-Status "Model config" "starter config created" Green
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
