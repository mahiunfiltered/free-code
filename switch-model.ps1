# Free Claude Code - Live Model Switcher (Zero Restart)
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

if (-not $Model) {
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "   FCC LIVE MODEL SWITCHER (No Restart) " -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " [1] OpenAI GPT-4o" -ForegroundColor Green
    Write-Host " [2] OpenAI o3-mini" -ForegroundColor Green
    Write-Host " [3] Nemotron 3 Ultra (550B)" -ForegroundColor Green
    Write-Host " [4] Nemotron 3 Super (120B)" -ForegroundColor Green
    Write-Host " [5] Mistral Nemotron" -ForegroundColor Green
    Write-Host " [6] Moonshot Kimi K3" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    $Model = Read-Host "Select model [1-6]"
}

$key = $Model.ToLower().Trim()
if (-not $models.ContainsKey($key)) {
    Write-Host "Invalid model selection: $Model" -ForegroundColor Red
    exit 1
}

$selected = $models[$key]
$mName = $selected.Name
$mRef = $selected.Ref

Write-Host "Switching active model to: $mName..." -ForegroundColor Yellow

$payload = @{ values = @{ MODEL = $mRef } } | ConvertTo-Json
$headers = @{ Origin = "http://127.0.0.1:8082" }
try {
    $res = Invoke-RestMethod -Uri "http://127.0.0.1:8082/admin/api/config/apply" -Method Post -Body $payload -ContentType "application/json" -Headers $headers -TimeoutSec 5
    Write-Host "SUCCESS: Active model switched to $mName ($mRef)" -ForegroundColor Green
    Write-Host "All subsequent requests in your Claude Code session will immediately use this model." -ForegroundColor Cyan
} catch {
    Write-Host "Failed to switch model: $($_.Exception.Message)" -ForegroundColor Red
}
