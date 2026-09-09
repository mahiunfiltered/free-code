# Free Claude Code - Claude Code Coding Agent Launcher
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

# Ensure uv and npm global bins are on PATH
if (Test-Path "$env:USERPROFILE\.local\bin") {
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (Test-Path "$env:APPDATA\npm") {
    $env:Path = "$env:APPDATA\npm;$env:Path"
}

$env:PYTHONUTF8 = "1"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Launching Claude Code Agent via FCC" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

fcc-claude @args
