$ErrorActionPreference = "Stop"

$port = 8002
$stopScript = Join-Path $PSScriptRoot "stop_components.ps1"
$toolDirectory = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $toolDirectory ".venv\Scripts\pythonw.exe"
$loginLauncher = Join-Path $PSScriptRoot "svacer_login_app.py"

try {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -gt 0) {
        Write-Host "Порт $port занят старым MCP. Выполняется автоматическая остановка..." -ForegroundColor Yellow
        & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $stopScript -Mode Svacer
        if ($LASTEXITCODE -ne 0) {
            throw "Не удалось автоматически освободить порт $port. Открой START.cmd и выбери пункт 8."
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) -and
           [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        throw "Порт $port не освободился после остановки прежнего MCP."
    }

    if (-not (Test-Path -LiteralPath $pythonPath)) {
        throw "Не найдено Python-окружение. Откройте START.cmd и выберите пункт 6."
    }
    if (-not (Test-Path -LiteralPath $loginLauncher)) {
        throw "Не найден компонент входа в Svacer. Восстановите установку через пункт 6."
    }
    $localToken = [Environment]::GetEnvironmentVariable("SVACER_LOCAL_MCP_TOKEN", "User")
    if ([string]::IsNullOrWhiteSpace($localToken)) {
        throw "Локальное подключение не установлено. Откройте START.cmd и выберите пункт 6."
    }

    Write-Host "Открываю форму входа в Svacer без дополнительного окна консоли." -ForegroundColor Cyan
    $env:SVACER_LOCAL_MCP_TOKEN = $localToken
    try {
        Start-Process -FilePath $pythonPath -WorkingDirectory $PSScriptRoot `
            -ArgumentList @("`"$loginLauncher`"")
    }
    finally {
        Remove-Item Env:SVACER_LOCAL_MCP_TOKEN -ErrorAction SilentlyContinue
        $localToken = $null
    }
}
catch {
    Write-Host ""
    Write-Host "Не удалось переподключить Svacer:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Read-Host "Нажмите Enter для закрытия"
    exit 1
}
