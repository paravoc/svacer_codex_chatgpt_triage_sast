param(
    [string]$SnapshotUrl,
    [string]$RepositoryUrl,
    [string]$GitRef,
    [switch]$NoClipboard,
    [switch]$NoOpen,
    [switch]$NoDashboard
)

$ErrorActionPreference = "Stop"
$toolDirectory = Split-Path -Parent $PSScriptRoot
$settingsPath = Join-Path $PSScriptRoot "svacer-settings.json"
if (-not (Test-Path -LiteralPath $settingsPath)) {
    throw "Не найден файл настроек: $settingsPath"
}
$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$filterName = [string]$settings.filter_name
$advancedFilter = [string]$settings.advanced_filter
$parallelWorkers = [int]$settings.parallel_workers
$verificationEnabled = [bool]$settings.verification_enabled
$verificationVerdicts = @($settings.verification_verdicts | ForEach-Object { [string]$_ })
$verificationWorkers = [int]$settings.verification_workers
$tokenWarning = [int]$settings.saved_context_token_warning
if ([string]::IsNullOrWhiteSpace($filterName)) {
    throw "В svacer-settings.json не задан filter_name"
}
if ($advancedFilter -cne 'filter(markers, "ГОСТ 71207-2024" in .checker_labels)') {
    throw "В svacer-settings.json должен быть точный фильтр ГОСТ 71207-2024"
}
if ($parallelWorkers -lt 1 -or $parallelWorkers -gt 8) {
    throw "parallel_workers должен быть от 1 до 8"
}
if ($verificationWorkers -lt 1 -or $verificationWorkers -gt 8) {
    throw "verification_workers должен быть от 1 до 8"
}
if (-not $verificationEnabled) {
    throw "verification_enabled должен быть true: Confirmed нельзя импортировать без независимой проверки"
}
if ($verificationVerdicts.Count -ne 1 -or $verificationVerdicts[0] -cne "Confirmed") {
    throw "verification_verdicts должен содержать только Confirmed"
}
if ($tokenWarning -lt 0) {
    throw "saved_context_token_warning не может быть отрицательным"
}

if ([string]::IsNullOrWhiteSpace($SnapshotUrl)) {
    $SnapshotUrl = Read-Host "Вставьте ссылку на снимок Svacer"
}
if ([string]::IsNullOrWhiteSpace($RepositoryUrl)) {
    $RepositoryUrl = Read-Host "Вставьте URL Git-репозитория"
}
if ([string]::IsNullOrWhiteSpace($GitRef)) {
    $GitRef = Read-Host "Введите точный тег, ветку или commit"
}

if ([string]::IsNullOrWhiteSpace($SnapshotUrl) -or
    [string]::IsNullOrWhiteSpace($RepositoryUrl) -or
    [string]::IsNullOrWhiteSpace($GitRef)) {
    throw "Все три значения обязательны."
}
$snapshotUri = $null
$serverUri = $null
if (-not [Uri]::TryCreate($SnapshotUrl.Trim(), [UriKind]::Absolute, [ref]$snapshotUri) -or
    $snapshotUri.Scheme -notin @('http', 'https') -or $snapshotUri.UserInfo) {
    throw "Нужна полная http/https ссылка на снимок без пароля или токена в URL."
}
if (-not [Uri]::TryCreate([string]$settings.svacer_url, [UriKind]::Absolute, [ref]$serverUri) -or
    $snapshotUri.Authority -ne $serverUri.Authority -or $snapshotUri.Scheme -ne $serverUri.Scheme) {
    throw "Сервер ссылки не совпадает с svacer_url в svacer-settings.json. Исправьте настройку и перезапустите MCP."
}
$uuid = '[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
$snapshotMatch = [regex]::Match($snapshotUri.AbsolutePath, "/project/($uuid)/branch/($uuid)/snapshot/($uuid)(?:/|$)")
if (-not $snapshotMatch.Success) {
    throw "Ссылка не похожа на ссылку снимка Svacer: отсутствуют UUID project/branch/snapshot."
}

$stamp = (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0, 8)
$jobDir = Join-Path (Join-Path $toolDirectory "RESULTS") $stamp
$null = New-Item -ItemType Directory -Path $jobDir
$null = New-Item -ItemType Directory -Path (Join-Path $jobDir "raw") -Force
$null = New-Item -ItemType Directory -Path (Join-Path $jobDir "notes") -Force

$job = [ordered]@{
    snapshot_url = $SnapshotUrl.Trim()
    project_id = $snapshotMatch.Groups[1].Value
    branch_id = $snapshotMatch.Groups[2].Value
    snapshot_id = $snapshotMatch.Groups[3].Value
    repository_url = $RepositoryUrl.Trim()
    git_ref = $GitRef.Trim()
    filter_name = $filterName
    advanced_filter = $advancedFilter
    parallel_workers = $parallelWorkers
    codex_model = ''
    manual_selection_only = $true
    verification_enabled = $verificationEnabled
    verification_verdicts = $verificationVerdicts
    verification_workers = $verificationWorkers
    saved_context_token_warning = $tokenWarning
    tool_directory = $toolDirectory
    app_directory = $PSScriptRoot
    job_directory = $jobDir
    created_at = (Get-Date).ToString("o")
}
$job | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $jobDir "job.json") -Encoding UTF8

$prompt = @"
Automated Svacer Triage job.
Configuration: $(Join-Path $jobDir "job.json")
Filter: $filterName

This file is retained for recovery and diagnostics. Do not paste it into Codex.
The Start analysis action prepares the exact revision and assigned traces and then
sends a compact English runtime prompt. Only the final Svacer comment is in Russian.
"@
$promptPath = Join-Path $jobDir "START_PROMPT.txt"
$prompt | Set-Content -LiteralPath $promptPath -Encoding UTF8

Write-Host ""
Write-Host "Задача создана: $jobDir"
Write-Host "Ручная очередь: до $parallelWorkers агентов, по одному выбранному маркеру на агента."
Write-Host "Промпт автоматически сохранён: $promptPath"
Write-Host "В панели получите маркеры, выделите нужные, добавьте их в очередь и нажмите 'Начать анализ'."
Write-Host ""
if (-not $NoOpen) {
    Start-Process explorer.exe -ArgumentList @($jobDir)
}
if (-not $NoDashboard) {
    $dashboardScript = Join-Path $PSScriptRoot "triage_gui.ps1"
    $dashboardArguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$dashboardScript`" -JobDirectory `"$jobDir`""
    # The user explicitly requested a visible interactive graphical dashboard.
    Start-Process -FilePath "powershell.exe" -ArgumentList $dashboardArguments
}
