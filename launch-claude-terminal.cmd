@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

set "WT_EXE="
for /f "tokens=*" %%i in ('where wt.exe 2^>nul') do (
    if not defined WT_EXE set "WT_EXE=%%i"
)
if not defined WT_EXE (
    if exist "%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe" (
        set "WT_EXE=%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"
    )
)

if defined WT_SESSION (
    call "%SCRIPT_DIR%\Claude-Code.bat" %*
    exit /b %errorlevel%
)

if defined WT_EXE (
    start "" "%WT_EXE%" -d "%SCRIPT_DIR%" cmd.exe /c "call ""%SCRIPT_DIR%\Claude-Code.bat"" %*"
    exit /b 0
)

call "%SCRIPT_DIR%\Claude-Code.bat" %*
exit /b %errorlevel%
