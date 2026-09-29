@echo off
:: Double-click to stop the Claude window, FCC server, NVIDIA proxy, and Ollama.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Stop-All.ps1"
