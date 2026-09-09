# Free Claude Code - Interactive Model Switcher (TUI)
param(
    [string]$Model = ""
)

$models = @{
    "1" = @{ Name = "OpenAI GPT-4o"; Ref = "openai/gpt-4o" }
    "gpt4o" = @{ Name = "OpenAI GPT-4o"; Ref = "openai/gpt-4o" }
    "2" = @{ Name = "OpenAI o3-mini"; Ref = "openai/o3-mini" }
    "o3mini" = @{ Name = "OpenAI o3-mini"; Ref = "openai/o3-mini" }
    "3" = @{ Name = "Nemotron 3 Ultra (550B)"; Ref = "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b" }
    "ultra" = @{ Name = "Nemotron 3 Ultra (550B)"; Ref = "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b" }
    "4" = @{ Name = "Nemotron 3 Super (120B)"; Ref = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b" }
    "super" = @{ Name = "Nemotron 3 Super (120B)"; Ref = "nvidia_nim/nvidia/nemotron-3-super-120b-a12b" }
    "5" = @{ Name = "Mistral Nemotron"; Ref = "nvidia_nim/mistralai/mistral-nemotron" }
    "mistral" = @{ Name = "Mistral Nemotron"; Ref = "nvidia_nim/mistralai/mistral-nemotron" }
    "6" = @{ Name = "Moonshot Kimi K3"; Ref = "nvidia_nim/moonshotai/kimi-k3" }
    "kimi" = @{ Name = "Moonshot Kimi K3"; Ref = "nvidia_nim/moonshotai/kimi-k3" }
}

$ADMIN_URL = $env:FCC_ADMIN_URL ?? "http://127.0.0.1:8082"
$AUTH_TOKEN = $env:ANTHROPIC_AUTH_TOKEN ?? "freecc"

function Invoke-AdminApi {
    param(
        [string]$Endpoint,
        [string]$Method = "GET",
        [hashtable]$Body = $null
    )
    $headers = @{
        "Authorization" = "Bearer $AUTH_TOKEN"
        "Content-Type"  = "application/json"
        "Origin"        = $ADMIN_URL
    }
    $uri = "$ADMIN_URL$Endpoint"
    try {
        if ($Body) {
            $json = $Body | ConvertTo-Json -Depth 5
            $res = Invoke-RestMethod -Uri $uri -Method $Method -Body $json -Headers $headers -ContentType "application/json" -TimeoutSec 5
        } else {
            $res = Invoke-RestMethod -Uri $uri -Method $Method -Headers $headers -TimeoutSec 5
        }
        return $res
    } catch {
        Write-Error "API call failed: $($_.Exception.Message)"
        return $null
    }
}

function Get-CurrentModel {
    $status = Invoke-AdminApi "/admin/api/status"
    return $status.model ?? ""
}

function Get-Models {
    $resp = Invoke-AdminApi "/admin/api/models"
    $models = @()
    foreach ($m in $resp.models) {
        $models += [pscustomobject]@{
            Slug = $m
            ProviderRef = $m
            DisplayName = $m
            AllowsReasoning = $false
        }
    }
    return $models
}

function Apply-Model {
    param([string]$ModelRef)
    $payload = @{ values = @{ MODEL = $ModelRef } }
    return Invoke-AdminApi "/admin/api/config/apply" "POST" $payload
}

function Clear-Screen {
    if ($IsWindows) { [Console]::Clear() } else { Write-Host "`e[2J`e[H" -NoNewline }
}

function Print-Header {
    param([string]$Current)
    Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
    Write-Host "║           FREE CLAUDE CODE - MODEL SWITCHER                  ║" -ForegroundColor Cyan
    Write-Host "╠══════════════════════════════════════════════════════════════╣" -ForegroundColor Cyan
    Write-Host "║  Current: $($Current.PadRight(52))║" -ForegroundColor Cyan
    Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
    Write-Host ""
}

function Print-Models {
    param($Models, [string]$Current, [int]$SelectedIdx)
    for ($i = 0; $i -lt $Models.Count; $i++) {
        $m = $Models[$i]
        $isCurrent = $m.ProviderRef -eq $Current
        $isSelected = $i -eq $SelectedIdx

        $prefix = if ($isSelected) { "► " } else { "  " }
        $currentMarker = if ($isCurrent) { " ✓ current" } else { "" }
        $reasoning = if ($m.AllowsReasoning) { " (reasoning)" } else { "" }

        if ($isSelected) {
            Write-Host "$prefix$($m.DisplayName)$currentMarker$reasoning" -ForegroundColor Yellow -NoNewline
        } else {
            Write-Host "$prefix$($m.DisplayName)$currentMarker$reasoning" -ForegroundColor White -NoNewline
        }
        Write-Host ""
        Write-Host "    $($m.ProviderRef)" -ForegroundColor Gray
        Write-Host ""
    }
}

function Print-Help {
    Write-Host "↑/↓ or j/k: navigate   Enter: select   q: quit   r: refresh" -ForegroundColor DarkGray
}

if ($Model) {
    $modelsList = Get-Models
    if (-not $modelsList) { exit 1 }
    $modelMap = @{}
    foreach ($m in $modelsList) {
        $modelMap[$m.ProviderRef] = $m
        $modelMap[$m.Slug] = $m
        $modelMap[$m.ProviderRef.Split('/')[-1]] = $m
    }
    if (-not $modelMap.ContainsKey($Model)) {
        Write-Host "Unknown model: $Model" -ForegroundColor Red
        Write-Host "Available:" -ForegroundColor Gray
        foreach ($m in $modelsList) {
            Write-Host "  $($m.ProviderRef) ($($m.ProviderRef.Split('/')[-1]))" -ForegroundColor Gray
        }
        exit 1
    }
    $target = $modelMap[$Model]
    $current = Get-CurrentModel
    if ($target.ProviderRef -eq $current) {
        Write-Host "Already using $($target.DisplayName)" -ForegroundColor Yellow
        exit 0
    }
    $result = Apply-Model $target.ProviderRef
    if ($result.applied) {
        Write-Host "✓ Switched to $($target.DisplayName)" -ForegroundColor Green
        if ($result.restart.automatic) {
            Write-Host "Server restarting..." -ForegroundColor Gray
        }
        exit 0
    }
    Write-Host "Failed: $($result.errors)" -ForegroundColor Red
    exit 1
}

# Interactive TUI
$current = Get-CurrentModel
$modelsList = Get-Models
if (-not $modelsList) {
    Write-Host "No models available" -ForegroundColor Red
    exit 1
}

$selectedIdx = 0
for ($i = 0; $i -lt $modelsList.Count; $i++) {
    if ($modelsList[$i].ProviderRef -eq $current) {
        $selectedIdx = $i
        break
    }
}

while ($true) {
    Clear-Screen
    Print-Header $current
    Print-Models $modelsList $current $selectedIdx
    Print-Help

    $key = [Console]::ReadKey($true).Key
    switch ($key) {
        UpArrow { $selectedIdx = ($selectedIdx - 1 + $modelsList.Count) % $modelsList.Count }
        DownArrow { $selectedIdx = ($selectedIdx + 1) % $modelsList.Count }
        Enter {
            $model = $modelsList[$selectedIdx]
            if ($model.ProviderRef -eq $current) {
                Write-Host "`nAlready using $($model.DisplayName)" -ForegroundColor Yellow
            } else {
                $result = Apply-Model $model.ProviderRef
                if ($result.applied) {
                    Write-Host "`n✓ Switched to $($model.DisplayName)" -ForegroundColor Green
                    if ($result.restart.automatic) {
                        Write-Host "Server restarting..." -ForegroundColor Gray
                    }
                    $current = $model.ProviderRef
                } else {
                    Write-Host "`nFailed: $($result.errors)" -ForegroundColor Red
                }
            }
            Write-Host "`nPress Enter to continue..." -ForegroundColor DarkGray -NoNewline
            [Console]::ReadKey($true) | Out-Null
        }
        Q { exit 0 }
        R {
            try {
                $modelsList = Get-Models
                $current = Get-CurrentModel
                $selectedIdx = 0
                for ($i = 0; $i -lt $modelsList.Count; $i++) {
                    if ($modelsList[$i].ProviderRef -eq $current) {
                        $selectedIdx = $i
                        break
                    }
                }
            } catch {
                Write-Host "`nRefresh failed: $_" -ForegroundColor Red
                Write-Host "Press Enter to continue..." -ForegroundColor DarkGray -NoNewline
                [Console]::ReadKey($true) | Out-Null
            }
        }
        J { $selectedIdx = ($selectedIdx + 1) % $modelsList.Count }
        K { $selectedIdx = ($selectedIdx - 1 + $modelsList.Count) % $modelsList.Count }
    }
}