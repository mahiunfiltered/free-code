# Free Claude Code - Windows Terminal Launcher
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $scriptDir

$wtCmd = Get-Command wt.exe -ErrorAction SilentlyContinue
if (-not $wtCmd) {
    if (Test-Path "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe") {
        $wtPath = "$env:LOCALAPPDATA\Microsoft\WindowsApps\wt.exe"
    }
} else {
    $wtPath = $wtCmd.Source
}

if ($env:WT_SESSION -or (-not $wtPath)) {
    # Already inside Windows Terminal or WT is not available
    & "$scriptDir\launch-claude.ps1" @args
    exit $LASTEXITCODE
}

# Launch inside Windows Terminal
$argList = @("-d", $scriptDir, "powershell.exe", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", "$scriptDir\launch-claude.ps1")
if ($args.Count -gt 0) {
    $argList += $args
}

Start-Process -FilePath $wtPath -ArgumentList $argList
