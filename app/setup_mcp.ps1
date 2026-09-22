$ErrorActionPreference = "Stop"

$toolDirectory = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $toolDirectory ".venv\Scripts\python.exe"
$poetryVersion = "2.2.1"
$poetryToolDirectory = Join-Path $toolDirectory ".tools\poetry"
$poetryPython = Join-Path $poetryToolDirectory "Scripts\python.exe"
$poetryExecutable = Join-Path $poetryToolDirectory "Scripts\poetry.exe"
$dependencyStamp = Join-Path $toolDirectory ".venv\svacer-lock.sha256"

function Get-DependencyFingerprint {
    $projectHash = (Get-FileHash -LiteralPath (Join-Path $toolDirectory 'pyproject.toml') -Algorithm SHA256).Hash
    $lockHash = (Get-FileHash -LiteralPath (Join-Path $toolDirectory 'poetry.lock') -Algorithm SHA256).Hash
    return "$projectHash`:$lockHash"
}

function Find-CodexExecutable {
    $command = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue
    }
    if ($command -and $command.Source) {
        return $command.Source
    }

    $localAppData = [Environment]::GetFolderPath('LocalApplicationData')
    if (-not [string]::IsNullOrWhiteSpace($localAppData)) {
        foreach ($binDirectory in @(
            (Join-Path $localAppData 'Programs\OpenAI\Codex\bin'),
            (Join-Path $localAppData 'OpenAI\Codex\bin')
        )) {
            if (Test-Path -LiteralPath $binDirectory) {
                $candidates = @(
                    Get-ChildItem -LiteralPath $binDirectory -Filter 'codex.exe' -File -Recurse -ErrorAction SilentlyContinue
                )
                $candidate = $candidates |
                    Sort-Object LastWriteTimeUtc -Descending |
                    Select-Object -First 1
                if ($candidate) {
                    return $candidate.FullName
                }
            }
        }
    }
    return $null
}
$codexPath = Find-CodexExecutable
if (-not $codexPath) {
    Write-Host "Codex CLI не найден. Устанавливаю официальный Codex CLI для Windows..."
    $installer = '$env:CODEX_NON_INTERACTIVE="1"; Invoke-RestMethod https://chatgpt.com/codex/install.ps1 | Invoke-Expression'
    $encodedInstaller = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($installer))
    $installerProcess = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -Wait -PassThru `
        -ArgumentList @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                        '-EncodedCommand', $encodedInstaller)
    if ($installerProcess.ExitCode -ne 0) {
        throw "Не удалось установить Codex CLI официальным установщиком OpenAI. Проверьте доступ к chatgpt.com."
    }
    $codexPath = Find-CodexExecutable
    if (-not $codexPath) {
        throw "Codex CLI установлен, но исполняемый файл не найден. Закройте приложение и запустите START снова."
    }
}
Write-Host "Доступность подагентов проверяется в самой задаче Codex; без них анализ идёт последовательно."
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git не найден в PATH. Установите Git и откройте новое окно терминала."
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    Write-Host "Создаю локальное Python-окружение..."
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        & $pythonCommand.Source -m venv (Join-Path $toolDirectory ".venv")
    }
    if (-not (Test-Path -LiteralPath $pythonPath) -and $pyCommand) {
        & $pyCommand.Source -3 -m venv (Join-Path $toolDirectory ".venv")
    }
    if (-not $pythonCommand -and -not $pyCommand) {
        throw "Python не найден. Установите Python 3.10 или новее и снова запустите START."
    }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath)) {
        throw "Не удалось создать локальное Python-окружение"
    }
}

& $pythonPath -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info < (3, 15) else 1)"
if ($LASTEXITCODE -ne 0) { throw "Нужен Python версии от 3.10 до 3.14 включительно. Старое .venv автоматически не удаляется." }
if (-not (Test-Path -LiteralPath (Join-Path $toolDirectory "poetry.lock"))) {
    throw "Не найден poetry.lock. Скачайте полный архив приложения заново."
}
if (-not (Test-Path -LiteralPath $poetryExecutable)) {
    Write-Host "Устанавливаю локальный менеджер зависимостей Poetry $poetryVersion..."
    & $pythonPath -m venv $poetryToolDirectory
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $poetryPython)) {
        throw "Не удалось создать служебное окружение Poetry"
    }
    # pip is used only to bootstrap the pinned Poetry executable. Application
    # dependencies are installed exclusively from poetry.lock below.
    & $poetryPython -m pip install --disable-pip-version-check "poetry==$poetryVersion"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $poetryExecutable)) {
        throw "Не удалось установить Poetry $poetryVersion"
    }
}
Write-Host "Синхронизирую зависимости приложения по poetry.lock..."
$savedPoetryInProject = $env:POETRY_VIRTUALENVS_IN_PROJECT
$env:POETRY_VIRTUALENVS_IN_PROJECT = "true"
try {
    & $poetryExecutable sync --directory $toolDirectory --with desktop --without dev --no-root
}
finally {
    if ($null -eq $savedPoetryInProject) {
        Remove-Item Env:POETRY_VIRTUALENVS_IN_PROJECT -ErrorAction SilentlyContinue
    }
    else {
        $env:POETRY_VIRTUALENVS_IN_PROJECT = $savedPoetryInProject
    }
}
if ($LASTEXITCODE -ne 0) { throw "Не удалось установить зависимости из poetry.lock" }

$localToken = [Environment]::GetEnvironmentVariable("SVACER_LOCAL_MCP_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($localToken)) {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $localToken = -join ($bytes | ForEach-Object { $_.ToString("x2") })
    [Environment]::SetEnvironmentVariable("SVACER_LOCAL_MCP_TOKEN", $localToken, "User")
}
$env:SVACER_LOCAL_MCP_TOKEN = $localToken

# Windows PowerShell 5.1 can promote native stderr into a terminating error.
$savedErrorPreference = $ErrorActionPreference
try {
    $ErrorActionPreference = 'Continue'
    & $codexPath mcp get svacer *> $null
    $existingMcp = ($LASTEXITCODE -eq 0)
}
finally { $ErrorActionPreference = $savedErrorPreference }
if ($existingMcp) {
    & $codexPath mcp remove svacer
    if ($LASTEXITCODE -ne 0) { throw "Не удалось удалить старую настройку MCP" }
}

& $codexPath mcp add svacer --url "http://127.0.0.1:8002/mcp" --bearer-token-env-var SVACER_LOCAL_MCP_TOKEN
if ($LASTEXITCODE -ne 0) { throw "Не удалось зарегистрировать Svacer MCP в Codex" }

& (Join-Path $PSScriptRoot 'create_shortcut.ps1')
if ($LASTEXITCODE -ne 0) { throw "Не удалось создать ярлык Svacer Triage" }

[IO.File]::WriteAllText($dependencyStamp, (Get-DependencyFingerprint) + "`n", (New-Object Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "Svacer MCP зарегистрирован."
Write-Host "Ярлыки START и «Svacer Triage» в меню Пуск созданы с иконкой приложения."
Write-Host "При первом открытии приложение само предложит войти в Svacer."
Write-Host "Укажите адрес сервера, логин и пароль в форме; отдельная консоль не нужна."
Write-Host "После этого полностью перезапусти Codex Desktop."
