@echo off
:: Free Claude Code - open the Claude-style chat as a standalone app window.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Claude-Desktop.ps1"
