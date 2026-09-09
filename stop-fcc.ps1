# Free Claude Code - Server Stopper
Write-Host "Stopping any running FCC processes..." -ForegroundColor Yellow

$processes = @(Get-Process -Name "fcc-server", "fcc-desktop", "fcc-claude" -ErrorAction SilentlyContinue)
if ($processes.Count -eq 0) {
    Write-Host "No active Free Claude Code processes found." -ForegroundColor Green
} else {
    foreach ($p in $processes) {
        Write-Host "Stopping $($p.ProcessName) (PID: $($p.Id))..." -ForegroundColor Cyan
        Stop-Process -Id $p.Id -Force
    }
    Write-Host "All Free Claude Code processes have been stopped." -ForegroundColor Green
}
