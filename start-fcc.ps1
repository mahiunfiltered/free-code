# Free Claude Code - Server Launcher
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

# Ensure uv and local bins are on PATH
if (Test-Path "$env:USERPROFILE\.local\bin") {
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting Free Claude Code (FCC) Server" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Proxy URL: http://127.0.0.1:8082" -ForegroundColor Green
Write-Host "Admin UI:  http://127.0.0.1:8082/admin" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C in this window to stop the server." -ForegroundColor Yellow
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

fcc-server
