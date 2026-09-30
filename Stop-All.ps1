# Free Claude Code - stop everything Start-All.ps1 starts:
#   Claude app window, live Claude Code chat processes, FCC server, NVIDIA proxy, Ollama.
$ErrorActionPreference = "SilentlyContinue"

function Write-Status([string]$Name, [string]$State, [string]$Color) {
    Write-Host ("  {0,-22} {1}" -f $Name, $State) -ForegroundColor $Color
}

function Stop-Tree([int[]]$ProcessIds) {
    # /T also ends child processes (e.g. the claude.exe chats the FCC server spawned).
    foreach ($id in ($ProcessIds | Sort-Object -Unique)) { taskkill /PID $id /T /F *> $null }
}

function Stop-PortOwner([string]$Name, [int]$Port, [string]$Expect = "") {
    $ids = @(Get-NetTCPConnection -LocalPort $Port -State Listen | Select-Object -ExpandProperty OwningProcess)
    if ($Expect) {
        # Only stop the listener if it is the process Start-All launched (not some other app on that port).
        $ids = @($ids | Where-Object {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$_"
            "$($proc.Name) $($proc.CommandLine)" -like "*$Expect*"
        })
    }
    if ($ids.Count) {
        Stop-Tree $ids
        Write-Status $Name "stopped" Green
    } else {
        Write-Status $Name "not running" DarkGray
    }
}

Write-Host ""
Write-Host "  Stopping Claude (Free Claude Code)" -ForegroundColor Cyan
Write-Host "  ----------------------------------" -ForegroundColor Cyan

# Claude app window: only browser processes using the dedicated chat-window profile.
$profileDir = Join-Path $env:USERPROFILE ".fcc\chat-window"
$window = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe'" |
    Where-Object { $_.CommandLine -like "*$profileDir*" } | Select-Object -ExpandProperty ProcessId)
if ($window.Count) { Stop-Tree $window; Write-Status "Claude window" "closed" Green }
else { Write-Status "Claude window" "not open" DarkGray }

# FCC server (port from ~/.fcc/.env, default 8082), plus any stray fcc processes.
$port = 8082
$envFile = Join-Path $env:USERPROFILE ".fcc\.env"
if (Test-Path $envFile) {
    $match = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*["'']?(\d+)' | Select-Object -First 1
    if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
}
Stop-PortOwner "FCC server" $port
Stop-Tree @(Get-Process -Name "fcc-server", "fcc-desktop" | Select-Object -ExpandProperty Id)

Stop-PortOwner "NVIDIA proxy" 8787 "proxy.py"

# Ollama: the tray app respawns the server, so stop both.
Stop-Tree @(Get-Process -Name "ollama app" | Select-Object -ExpandProperty Id)
Stop-PortOwner "Ollama" 11434 "ollama"

Write-Host ""
Write-Host "  All stopped. Start again with Start-All.bat" -ForegroundColor Gray
Start-Sleep -Seconds 3
