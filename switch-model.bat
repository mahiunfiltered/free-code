@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\npm;%PATH%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0switch-model.ps1" %*
