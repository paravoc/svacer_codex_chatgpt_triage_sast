$ErrorActionPreference = "Stop"
$toolDirectory = Split-Path -Parent $PSScriptRoot
$resultsDirectory = Join-Path $toolDirectory "RESULTS"
$openMenu = $args -contains "-Menu"

function Get-LastJob {
    if (-not (Test-Path -LiteralPath $resultsDirectory)) { return $null }
    return Get-ChildItem -LiteralPath $resultsDirectory -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "job.json") } |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
}

function Show-Menu {
    Clear-Host
    $listener = Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    $mcpStatus = if ($listener) { "запущен" } else { "не запущен" }
    $mcpColor = if ($listener) { "Green" } else { "Yellow" }
    $lastJob = Get-LastJob

    Write-Host "SVACER TRIAGE" -ForegroundColor Cyan
    Write-Host "==================" -ForegroundColor DarkGray
    Write-Host -NoNewline "Svacer MCP  "
    Write-Host $mcpStatus -ForegroundColor $mcpColor
    if ($lastJob) {
        try {
            $job = Get-Content -LiteralPath (Join-Path $lastJob.FullName "job.json") -Raw -Encoding UTF8 |
                ConvertFrom-Json
            $project = [IO.Path]::GetFileNameWithoutExtension(([string]$job.repository_url).TrimEnd('/'))
            Write-Host "Последняя задача  $project  $($job.git_ref)" -ForegroundColor Gray
        }
        catch {
            Write-Host "Последняя задача  $($lastJob.Name)" -ForegroundColor Gray
        }
    }
    else {
        Write-Host "Последняя задача  пока не создана" -ForegroundColor Gray
    }
    Write-Host ""
    Write-Host "1  Создать новый триаж" -ForegroundColor Green
    Write-Host "2  Продолжить последнюю задачу" -ForegroundColor Green
    Write-Host "3  Подключить Svacer" -ForegroundColor Yellow
    Write-Host "4  Открыть результаты" -ForegroundColor Cyan
    Write-Host "5  Показать краткую инструкцию" -ForegroundColor Cyan
    Write-Host "6  Установить или восстановить программу" -ForegroundColor Yellow
    Write-Host "7  Остановить анализ" -ForegroundColor Magenta
    Write-Host "8  Остановить Svacer MCP" -ForegroundColor Magenta
    Write-Host "9  Остановить всё" -ForegroundColor Red
    Write-Host "10 Создать архив для другого компьютера" -ForegroundColor Cyan
    Write-Host "0  Закрыть меню" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "Для обычной работы запускайте только START.cmd" -ForegroundColor DarkGray
    Write-Host ""
}

function Invoke-Stop {
    param([Parameter(Mandatory = $true)][string]$Mode)
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "stop_components.ps1") -Mode $Mode
    Read-Host "Нажмите Enter"
}

if (-not $openMenu) {
    $lastJob = Get-LastJob
    $pythonPath = Join-Path $toolDirectory ".venv\Scripts\pythonw.exe"
    if (Test-Path -LiteralPath $pythonPath) {
        if ($lastJob) {
            & (Join-Path $PSScriptRoot "triage_gui.ps1") -JobDirectory $lastJob.FullName
        }
        else {
            & (Join-Path $PSScriptRoot "triage_gui.ps1") -NewProject
        }
        exit 0
    }
    Write-Host "Для первого запуска нужно создать задачу или установить программу." -ForegroundColor Yellow
    Write-Host "Открываю служебное меню." -ForegroundColor Gray
    Start-Sleep -Milliseconds 700
}

while ($true) {
    Show-Menu
    $choice = Read-Host "Выберите действие"
    switch ($choice) {
        "1" {
            & (Join-Path $PSScriptRoot "triage_gui.ps1") -NewProject
            if ($LASTEXITCODE -ne 0) { Read-Host "Нажмите Enter" }
        }
        "2" {
            & (Join-Path $PSScriptRoot "triage_gui.ps1")
        }
        "3" {
            & (Join-Path $PSScriptRoot "restart_svacer_http.ps1")
            Start-Sleep -Seconds 1
        }
        "4" {
            if (-not (Test-Path -LiteralPath $resultsDirectory)) {
                New-Item -ItemType Directory -Path $resultsDirectory | Out-Null
            }
            Start-Process explorer.exe -ArgumentList @($resultsDirectory)
        }
        "5" {
            Start-Process notepad.exe -ArgumentList @((Join-Path $toolDirectory "README.md"))
        }
        "6" {
            try {
                & (Join-Path $PSScriptRoot "setup_mcp.ps1")
                if ($LASTEXITCODE -ne 0) {
                    throw "Установщик завершился с кодом $LASTEXITCODE."
                }
                Read-Host "Установка завершена. Нажмите Enter"
            }
            catch {
                Write-Host ""
                Write-Host "Установка не завершена:" -ForegroundColor Red
                Write-Host $_.Exception.Message -ForegroundColor Red
                Write-Host "Сфотографируйте или скопируйте это сообщение — по нему можно определить причину." `
                    -ForegroundColor Yellow
                Read-Host "Нажмите Enter, чтобы вернуться в меню"
            }
        }
        "7" { Invoke-Stop -Mode "Analyze" }
        "8" { Invoke-Stop -Mode "Svacer" }
        "9" { Invoke-Stop -Mode "All" }
        "10" {
            & (Join-Path $PSScriptRoot "make_portable_package.ps1")
            Read-Host "Нажмите Enter"
        }
        "0" { exit 0 }
        default {
            Write-Host "Неизвестный пункт: $choice" -ForegroundColor Red
            Start-Sleep -Seconds 1
        }
    }
}
