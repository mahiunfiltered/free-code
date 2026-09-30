@echo off
:: Free Claude Code - open the FreeCode chat as a standalone app window.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0FreeCode.ps1"
