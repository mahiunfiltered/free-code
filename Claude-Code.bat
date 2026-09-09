@echo off
setlocal DisableDelayedExpansion
title Claude Code

:: Check if already running inside Windows Terminal or dispatched with --in-wt
if "%~1"=="--in-wt" (
    shift
    goto :inside_wt
)
if defined WT_SESSION (
    goto :inside_wt
)

:: Discover wt.exe
set "WT_EXE="
for /f "tokens=*" %%i in ('where wt.exe 2^>nul') do (
    if not defined WT_EXE set "WT_EXE=%%i"
)
if not defined WT_EXE (
    if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" (
        set "WT_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"
    )
)

if defined WT_EXE (
    start "" "%WT_EXE%" -d "%~dp0." cmd.exe /c "call ""%~f0"" --in-wt %*"
    exit /b 0
)

:inside_wt
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PATH=%USERPROFILE%\.local\bin;%APPDATA%\npm;%PATH%"

:: Check if FCC is already running
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
    echo ========================================================
    echo ERROR: Free Claude Code server failed to start in time.
    echo Please check logs: %USERPROFILE%\.fcc\logs\server.log
    echo ========================================================
    echo.
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
echo Launching Claude Code...
echo.

call fcc-claude %*
