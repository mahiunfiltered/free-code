@echo off
setlocal enabledelayedExpansion
title Claude Code - Model Selector

if "%~1"=="--in-wt" (
    shift
    goto :inside_wt
)
if defined WT_SESSION (
    goto :inside_wt
)

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

:menu
cls
echo ========================================================
echo             CLAUDE CODE - MODEL SELECTOR
echo ========================================================
echo.
echo  [1] OpenAI GPT-4o (Official API)
echo      Flagship OpenAI multimodal intelligence
echo.
echo  [2] OpenAI o3-mini (Reasoning)
echo      High-efficiency OpenAI reasoning model
echo.
echo  [3] Nemotron 3 Ultra (550B)
echo      Best for maximum reasoning, deep coding, and complex tasks
echo.
echo  [4] Nemotron 3 Super (120B)
echo      Fast and powerful coding model
echo.
echo  [5] Mistral Nemotron
echo      High-capability coding and reasoning model
echo.
echo  [6] Moonshot Kimi K3
echo      General-purpose reasoning and coding
echo.
echo  [7] Default Model (FCC Managed Default)
echo.
echo  [8] Open Admin UI (http://127.0.0.1:8082/admin)
echo.
echo  [Q] Exit
echo ========================================================
echo.

set /p "MCHOICE=Select a model [1-7, 8, Q]: "

:: Strip all whitespace from user input
if defined MCHOICE set "MCHOICE=!MCHOICE: =!"

if /i "%MCHOICE%"=="1" (
    set "MODEL_FLAG=--model anthropic/openai/gpt-4o"
    set "MODEL_NAME=OpenAI GPT-4o"
    goto start_proxy
)
if /i "%MCHOICE%"=="2" (
    set "MODEL_FLAG=--model anthropic/openai/o3-mini"
    set "MODEL_NAME=OpenAI o3-mini"
    goto start_proxy
)
if /i "%MCHOICE%"=="3" (
    set "MODEL_FLAG=--model anthropic/nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"
    set "MODEL_NAME=Nemotron 3 Ultra (550B)"
    goto start_proxy
)
if /i "%MCHOICE%"=="4" (
    set "MODEL_FLAG=--model anthropic/nvidia_nim/nvidia/nemotron-3-super-120b-a12b"
    set "MODEL_NAME=Nemotron 3 Super (120B)"
    goto start_proxy
)
if /i "%MCHOICE%"=="5" (
    set "MODEL_FLAG=--model anthropic/nvidia_nim/mistralai/mistral-nemotron"
    set "MODEL_NAME=Mistral Nemotron"
    goto start_proxy
)
if /i "%MCHOICE%"=="6" (
    set "MODEL_FLAG=--model anthropic/nvidia_nim/moonshotai/kimi-k3"
    set "MODEL_NAME=Moonshot Kimi K3"
    goto start_proxy
)
if /i "%MCHOICE%"=="7" (
    set "MODEL_FLAG="
    set "MODEL_NAME=FCC Default Model"
    goto start_proxy
)
if /i "%MCHOICE%"=="8" (
    start "" "http://127.0.0.1:8082/admin"
    goto menu
)
if /i "%MCHOICE%"=="Q" exit /b 0

echo.
echo [!] Invalid selection "%MCHOICE%". Please select 1-7, 8, or Q.
ping -n 3 127.0.0.1 >nul 2>&1
goto menu

:start_proxy
echo.
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
echo Launching Claude Code with %MODEL_NAME%...
echo.

set "FINAL_MODEL_FLAG=%MODEL_FLAG%"
setlocal DisableDelayedExpansion
call fcc-claude %FINAL_MODEL_FLAG% %*
