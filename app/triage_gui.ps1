param(
    [string]$JobDirectory,
    [switch]$NewProject
)

$ErrorActionPreference = "Stop"
$toolDirectory = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $toolDirectory ".venv\Scripts\pythonw.exe"
$pythonConsole = Join-Path $toolDirectory ".venv\Scripts\python.exe"
$guiPath = Join-Path $PSScriptRoot "triage_gui_qt.py"
if (-not (Test-Path -LiteralPath $pythonPath) -or -not (Test-Path -LiteralPath $pythonConsole)) {
    throw "Не найдено Python-окружение. Откройте START.cmd и выберите пункт 6"
}
& $pythonConsole -c "import PySide6"
if ($LASTEXITCODE -ne 0) {
    throw "Не найден PySide6. Откройте START.cmd -Menu и выберите пункт 6"
}
$localToken = [Environment]::GetEnvironmentVariable("SVACER_LOCAL_MCP_TOKEN", "User")
if (-not [string]::IsNullOrWhiteSpace($localToken)) {
    $env:SVACER_LOCAL_MCP_TOKEN = $localToken
}
try {
    $arguments = "`"$guiPath`""
    if (-not [string]::IsNullOrWhiteSpace($JobDirectory)) {
        $arguments += " --job `"$JobDirectory`""
    }
    if ($NewProject) { $arguments += " --new-project" }
    Start-Process -FilePath $pythonPath -ArgumentList $arguments
}
finally {
    Remove-Item Env:SVACER_LOCAL_MCP_TOKEN -ErrorAction SilentlyContinue
}
