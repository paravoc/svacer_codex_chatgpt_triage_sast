[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Svacer", "Analyze", "All")]
    [string]$Mode,
    [switch]$Elevated,
    [switch]$NoElevation,
    [ValidateRange(1, 65535)]
    [int]$Port = 8002
)

$ErrorActionPreference = "Stop"
$mcpPort = $Port
$toolDirectory = Split-Path -Parent $PSScriptRoot

function Test-IsAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-ElevatedSelf {
    Write-Host "Для остановки процесса требуется подтверждение Windows (UAC)." -ForegroundColor Yellow
    $arguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $PSCommandPath + '"'),
        "-Mode", $Mode,
        "-Port", $Port,
        "-Elevated"
    )
    $process = Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
        -WorkingDirectory $PSScriptRoot -ArgumentList $arguments
    exit $process.ExitCode
}

function Get-ProcessSnapshot {
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
}

function Stop-SvacerServer {
    $listeners = @(Get-NetTCPConnection -LocalPort $mcpPort -State Listen -ErrorAction SilentlyContinue)
    if ($listeners.Count -eq 0) {
        Write-Host "Svacer MCP уже остановлен: порт $mcpPort свободен." -ForegroundColor Green
        return
    }

    foreach ($listener in ($listeners | Sort-Object OwningProcess -Unique)) {
        $listenerId = [int]$listener.OwningProcess
        $process = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $listenerId" `
            -OperationTimeoutSec 5 -ErrorAction SilentlyContinue
        if ($null -eq $process) {
            throw "Не удалось определить процесс PID $listenerId на порту $mcpPort."
        }

        $name = [string]$process.Name
        $commandLine = [string]$process.CommandLine
        # A connector may have been started from an older clone at another path.
        # Identify it by the dedicated entrypoint and private stdin-login mode;
        # never stop a generic Python process merely because it owns this port.
        $entrypointPattern = '(?i)(?:^|[\s"])(?:[^"\r\n]*[\\/])?start_svacer_http\.py(?:[\s"]|$)'
        $recognized = $name -in @("python.exe", "pythonw.exe") -and
            $commandLine -match $entrypointPattern -and $commandLine -match '(?i)(?:^|\s)--login-stdin(?:\s|$)'
        if (-not $recognized) {
            throw "Порт $mcpPort занят неизвестной программой $name (PID $listenerId). Она не остановлена."
        }

        # The connector itself owns the listening socket. Stop that exact,
        # recognized process; never walk up to its Qt parent or enumerate and
        # terminate unrelated descendants.
        Write-Host "Останавливается Svacer MCP (PID $listenerId)..." -ForegroundColor Yellow
        Stop-Process -Id $listenerId -Force -ErrorAction Stop
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-NetTCPConnection -LocalPort $mcpPort -State Listen -ErrorAction SilentlyContinue) -and
           [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-NetTCPConnection -LocalPort $mcpPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Порт $mcpPort не освободился после остановки Svacer MCP."
    }
    Write-Host "Svacer MCP остановлен." -ForegroundColor Green
}

function Stop-LocalAnalysis {
    $resultsRoot = Join-Path $toolDirectory "RESULTS"
    $pausedJobs = 0
    if (Test-Path -LiteralPath $resultsRoot) {
        foreach ($job in (Get-ChildItem -LiteralPath $resultsRoot -Directory -ErrorAction SilentlyContinue)) {
            if (-not (Test-Path -LiteralPath (Join-Path $job.FullName "job.json"))) {
                continue
            }
            $control = [ordered]@{
                pause_requested = $true
                updated_at = [DateTime]::UtcNow.ToString("o")
                source = "STOP ANALYZE.cmd"
            }
            $control | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $job.FullName "control.json") -Encoding UTF8
            $pausedJobs++
        }
    }

    $scriptNames = @("triage_dashboard.py", "triage_queue.py")
    $stopped = 0
    foreach ($process in (Get-ProcessSnapshot)) {
        $commandLine = [string]$process.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            continue
        }
        $belongsToTool = $commandLine -like "*$toolDirectory*"
        $isAnalysisProcess = $false
        foreach ($scriptName in $scriptNames) {
            if ($commandLine -like "*$scriptName*") {
                $isAnalysisProcess = $true
                break
            }
        }
        if ($belongsToTool -and $isAnalysisProcess) {
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction SilentlyContinue
            $stopped++
        }
    }

    Write-Host "Локальная очередь поставлена на паузу: задач $pausedJobs." -ForegroundColor Green
    Write-Host "Остановлено локальных процессов анализа/панели: $stopped." -ForegroundColor Green
    Write-Host "Уже выполняемую партию Codex останови кнопкой Stop в самой задаче." -ForegroundColor Yellow
}

try {
    if ($Mode -in @("Svacer", "All") -and -not $NoElevation -and -not (Test-IsAdministrator) -and -not $Elevated) {
        Invoke-ElevatedSelf
    }
    if ($Mode -in @("Analyze", "All")) {
        Stop-LocalAnalysis
    }
    if ($Mode -in @("Svacer", "All")) {
        Stop-SvacerServer
    }
    exit 0
}
catch {
    Write-Host "Ошибка остановки: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
