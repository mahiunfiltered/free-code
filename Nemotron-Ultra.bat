@echo off
setlocal DisableDelayedExpansion
title Claude Code (Nemotron 3 Ultra)

chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\npm;%PATH%"

curl.exe -s -m 1 http://127.0.0.1:8082/health >nul 2>&1
if %errorlevel% equ 0 (
    goto ready
)

echo Starting Free Claude Code...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath 'fcc-server' -WindowStyle Hidden"

echo Waiting for FCC server...
set /a attempts=0

:wait_loop
set /a attempts+=1
if %attempts% gtr 20 (
    echo.
    echo ERROR: Free Claude Code server failed to start in time.
    pause
    exit /b 1
)

curl.exe -s -m 1 http://127.0.0.1:8082/health >nul 2>&1
if %errorlevel% equ 0 (
    goto ready
)

ping -n 2 127.0.0.1 >nul 2>&1
goto wait_loop

:ready
echo FCC server ready.
echo Launching Claude Code with Nemotron 3 Ultra (550B)...
echo.

call fcc-claude --model "anthropic/nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b" %*