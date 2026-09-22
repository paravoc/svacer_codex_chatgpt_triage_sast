$ErrorActionPreference = "Stop"

$toolDirectory = Split-Path -Parent $PSScriptRoot
$settingsPath = Join-Path $PSScriptRoot "svacer-settings.json"
$pythonPath = Join-Path $toolDirectory ".venv\Scripts\python.exe"
$serverPath = Join-Path $PSScriptRoot "start_svacer_http.py"
$logPath = Join-Path $toolDirectory "svacer-http.log"
$port = 8002

trap {
    $message = ($_ | Out-String)
    Add-Content -LiteralPath $logPath -Value $message -Encoding UTF8
    Write-Host ""
    Write-Host "Ошибка запуска Svacer MCP:"
    Write-Host $message
    Write-Host "Журнал: $logPath"
    Read-Host "Нажмите Enter для закрытия"
    exit 1
}

$listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
if ($listeners.Count -gt 0) {
    Write-Host "Порт $port уже занят. Это может быть ранее запущенный MCP или другая программа."
    Write-Host "Проверьте существующее окно MCP и подключение из Codex. Сам по себе занятый порт не подтверждает готовность."
    Read-Host "Нажмите Enter для закрытия"
    exit 3
}
if (-not (Test-Path -LiteralPath $settingsPath)) {
    throw "Не найден файл настроек: $settingsPath"
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Не найдено Python-окружение. Открой START.cmd и выбери пункт 6"
}

$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$localToken = [Environment]::GetEnvironmentVariable("SVACER_LOCAL_MCP_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($localToken)) {
    throw "Не найден локальный MCP-токен. Открой START.cmd и выбери пункт 6"
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Однократный вход в Svacer"
$form.StartPosition = "CenterScreen"
$form.ClientSize = New-Object System.Drawing.Size(430, 210)
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.TopMost = $true

$serverLabel = New-Object System.Windows.Forms.Label
$serverLabel.Location = New-Object System.Drawing.Point(20, 15)
$serverLabel.Size = New-Object System.Drawing.Size(390, 35)
$serverLabel.Text = "Сервер: $($settings.svacer_url)"

$loginLabel = New-Object System.Windows.Forms.Label
$loginLabel.Location = New-Object System.Drawing.Point(20, 60)
$loginLabel.Size = New-Object System.Drawing.Size(90, 23)
$loginLabel.Text = "Логин"

$loginBox = New-Object System.Windows.Forms.TextBox
$loginBox.Location = New-Object System.Drawing.Point(115, 57)
$loginBox.Size = New-Object System.Drawing.Size(290, 23)

$passwordLabel = New-Object System.Windows.Forms.Label
$passwordLabel.Location = New-Object System.Drawing.Point(20, 98)
$passwordLabel.Size = New-Object System.Drawing.Size(90, 23)
$passwordLabel.Text = "Пароль"

$passwordBox = New-Object System.Windows.Forms.TextBox
$passwordBox.Location = New-Object System.Drawing.Point(115, 95)
$passwordBox.Size = New-Object System.Drawing.Size(290, 23)
$passwordBox.UseSystemPasswordChar = $true

$okButton = New-Object System.Windows.Forms.Button
$okButton.Location = New-Object System.Drawing.Point(235, 150)
$okButton.Size = New-Object System.Drawing.Size(80, 30)
$okButton.Text = "Войти"
$okButton.DialogResult = [System.Windows.Forms.DialogResult]::OK

$cancelButton = New-Object System.Windows.Forms.Button
$cancelButton.Location = New-Object System.Drawing.Point(325, 150)
$cancelButton.Size = New-Object System.Drawing.Size(80, 30)
$cancelButton.Text = "Отмена"
$cancelButton.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

$form.AcceptButton = $okButton
$form.CancelButton = $cancelButton
$null = $form.Controls.AddRange(@(
    $serverLabel, $loginLabel, $loginBox, $passwordLabel, $passwordBox,
    $okButton, $cancelButton
))
$null = $form.Add_Shown({ $loginBox.Select() })
$dialogResult = $form.ShowDialog()
if ($dialogResult -ne [System.Windows.Forms.DialogResult]::OK -or
    [string]::IsNullOrWhiteSpace($loginBox.Text) -or
    [string]::IsNullOrWhiteSpace($passwordBox.Text)) {
    $passwordBox.Clear()
    $form.Dispose()
    Write-Host "Вход отменён."
    exit 2
}

$env:SVACER_URL = [string]$settings.svacer_url
$env:SVACER_LOGIN = $loginBox.Text
$env:SVACER_PASSWORD = $passwordBox.Text
$env:SVACER_MCP_TOKEN = $localToken
$env:SVACER_MCP_RESOURCE_URL = "http://127.0.0.1:$port"
$env:SVACER_HTTP_PORT = [string]$port
$env:SVACER_TRIAGE_ROOT = $toolDirectory
$env:SVACER_TOOLS = "get_projects,get_snapshots,get_warnings,get_markers,get_project_stats,get_project_groups,get_advanced_file_preview,get_diff,prepare_markup_import,apply_markup_import"
$env:PYTHONUTF8 = "1"

$passwordBox.Clear()
$form.Dispose()
Write-Host "Svacer MCP запускается на http://127.0.0.1:$port/mcp"
Write-Host "Оставь это окно открытым на время работы Codex."
Write-Host "Для остановки нажми Ctrl+C или закрой окно."
Write-Host ""

try {
    # Do not merge Python stderr into the PowerShell pipeline here. Windows
    # PowerShell 5.1 turns ordinary Python logging written to stderr into a
    # NativeCommandError when ErrorActionPreference is Stop.
    & $pythonPath $serverPath
    $serverExitCode = $LASTEXITCODE
    if ($serverExitCode -ne 0) {
        Write-Host ""
        Write-Host "Сервер завершился с ошибкой (код $serverExitCode)."
        Write-Host "Текст ошибки показан выше."
        Read-Host "Нажмите Enter для закрытия"
    }
    exit $serverExitCode
}
finally {
    Remove-Item Env:SVACER_LOGIN -ErrorAction SilentlyContinue
    Remove-Item Env:SVACER_PASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:SVACER_TRIAGE_ROOT -ErrorAction SilentlyContinue
}
