# FreeCode - open the chat UI as a standalone desktop-style app window.
# Starts the FCC server hidden if needed; no console window stays visible.
# -Page admin opens the model setup page instead of the chat.
param([string]$Page = "chat")
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Test-Path "$env:USERPROFILE\.local\bin") {
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$fccDir = Join-Path $env:USERPROFILE ".fcc"
$port = 8082
$envFile = Join-Path $fccDir ".env"
if (Test-Path $envFile) {
    $match = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*["'']?(\d+)' | Select-Object -First 1
    if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
}
$chatUrl = "http://127.0.0.1:$port/chat"
$pageUrl = "http://127.0.0.1:$port/$Page"

function Test-Chat {
    try {
        (Invoke-WebRequest -Uri $chatUrl -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch { $false }
}

if (-not (Test-Chat)) {
    Start-Process -FilePath "uv" -ArgumentList "run", "fcc-server" -WorkingDirectory $scriptDir -WindowStyle Hidden
    # First launch on a slow PC compiles packages; give it time.
    $deadline = (Get-Date).AddSeconds(180)
    while (-not (Test-Chat)) {
        if ((Get-Date) -gt $deadline) {
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.MessageBox]::Show(
                "FreeCode server did not start in time.`nSee $fccDir\logs\server.log",
                "FreeCode") | Out-Null
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
}

$browser = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "$env:LOCALAPPDATA\Microsoft\Edge\Application\msedge.exe",
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($browser) {
    $profileDir = Join-Path $fccDir "chat-window"
    Start-Process -FilePath $browser -ArgumentList "--app=$pageUrl", "--user-data-dir=`"$profileDir`"", "--no-first-run", "--no-default-browser-check"
} else {
    Start-Process $pageUrl
}
