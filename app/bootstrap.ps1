$ErrorActionPreference = 'Stop'

$toolDirectory = Split-Path -Parent $PSScriptRoot
$logDirectory = Join-Path $toolDirectory 'logs'
$logPath = Join-Path $logDirectory 'startup.log'
$statePath = Join-Path $logDirectory 'startup.state'
$readyPath = Join-Path $logDirectory 'startup.ready'
$python = Join-Path $toolDirectory '.venv\Scripts\python.exe'
$pythonw = Join-Path $toolDirectory '.venv\Scripts\pythonw.exe'
$gui = Join-Path $PSScriptRoot 'triage_gui_qt.py'
$setup = Join-Path $PSScriptRoot 'setup_mcp.ps1'
$splash = Join-Path $PSScriptRoot 'startup_splash.ps1'
$loadingGif = Join-Path $PSScriptRoot 'assets\svacer-hamster-loading.gif'
$applicationIcon = Join-Path $PSScriptRoot 'assets\svacer-triage-v2.ico'
$dependencyStamp = Join-Path $toolDirectory '.venv\svacer-lock.sha256'
$setupTimeoutMilliseconds = 20 * 60 * 1000
$mutex = New-Object Threading.Mutex($false, 'Local\SvacerTriageBootstrap')
$ownsMutex = $false

function Write-StartupLog {
    param([Parameter(Mandatory = $true)][string]$Message)
    $line = '{0}  {1}' -f ([DateTime]::Now.ToString('yyyy-MM-dd HH:mm:ss')), $Message
    $stream = New-Object IO.FileStream(
        $logPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite
    )
    try {
        [void]$stream.Seek(0, [IO.SeekOrigin]::End)
        $writer = New-Object IO.StreamWriter($stream, (New-Object Text.UTF8Encoding($false)))
        try {
            $writer.WriteLine($line)
            $writer.Flush()
        }
        finally { $writer.Dispose() }
    }
    finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Show-StartupError {
    param([Parameter(Mandatory = $true)][string]$Message)
    Add-Type -AssemblyName System.Windows.Forms
    $text = "$Message`r`n`r`nDetails were written to:`r`n$logPath"
    [void][Windows.Forms.MessageBox]::Show(
        $text, 'Svacer Triage',
        [Windows.Forms.MessageBoxButtons]::OK,
        [Windows.Forms.MessageBoxIcon]::Error
    )
}

function Set-StartupState {
    param([Parameter(Mandatory = $true)][string]$Phase)
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        $stateStream = $null
        try {
            $stateStream = New-Object IO.FileStream(
                $statePath, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite
            )
            $stateWriter = New-Object IO.StreamWriter($stateStream, (New-Object Text.UTF8Encoding($false)))
            try {
                $stateWriter.Write($Phase)
                $stateWriter.Flush()
            }
            finally { $stateWriter.Dispose() }
            return
        }
        catch {
            if ($attempt -eq 5) {
                Write-StartupLog "Could not update startup state: $($_.Exception.GetType().Name)."
                return
            }
            Start-Sleep -Milliseconds 40
        }
        finally {
            if ($stateStream) { $stateStream.Dispose() }
        }
    }
}

function Start-StartupSplash {
    if (-not (Test-Path -LiteralPath $splash -PathType Leaf)) {
        Write-StartupLog 'Startup splash is missing; continuing without it.'
        return
    }
    try {
        Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru `
            -ArgumentList @(
                '-NoLogo', '-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass',
                '-File', "`"$splash`"", '-ParentProcessId', ([string]$PID),
                '-StatePath', "`"$statePath`"", '-GifPath', "`"$loadingGif`"",
                '-IconPath', "`"$applicationIcon`""
            ) | Out-Null
    }
    catch {
        Write-StartupLog "Startup splash could not be opened: $($_.Exception.GetType().Name)."
    }
}

function Find-CodexExecutable {
    $command = Get-Command codex.exe -CommandType Application -ErrorAction SilentlyContinue
    if (-not $command) {
        $command = Get-Command codex -CommandType Application -ErrorAction SilentlyContinue
    }
    if ($command -and $command.Source) { return $command.Source }
    $localAppData = [Environment]::GetFolderPath('LocalApplicationData')
    if ([string]::IsNullOrWhiteSpace($localAppData)) { return $null }
    foreach ($directory in @(
        (Join-Path $localAppData 'Programs\OpenAI\Codex\bin'),
        (Join-Path $localAppData 'OpenAI\Codex\bin')
    )) {
        if (-not (Test-Path -LiteralPath $directory)) { continue }
        $candidate = Get-ChildItem -LiteralPath $directory -Filter 'codex.exe' -File -Recurse `
            -ErrorAction SilentlyContinue | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    return $null
}

function Get-DependencyFingerprint {
    $projectPath = Join-Path $toolDirectory 'pyproject.toml'
    $lockPath = Join-Path $toolDirectory 'poetry.lock'
    if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        return $null
    }
    $projectHash = (Get-FileHash -LiteralPath $projectPath -Algorithm SHA256).Hash
    $lockHash = (Get-FileHash -LiteralPath $lockPath -Algorithm SHA256).Hash
    return "$projectHash`:$lockHash"
}

function Test-Installation {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
        -not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
        return $false
    }
    $expectedFingerprint = Get-DependencyFingerprint
    if ([string]::IsNullOrWhiteSpace($expectedFingerprint) -or
        -not (Test-Path -LiteralPath $dependencyStamp -PathType Leaf) -or
        ([IO.File]::ReadAllText($dependencyStamp).Trim() -ne $expectedFingerprint)) {
        return $false
    }
    & $python -c 'import PySide6, httpx, mcp, starlette, uvicorn' *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    $token = [Environment]::GetEnvironmentVariable('SVACER_LOCAL_MCP_TOKEN', 'User')
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -lt 32) { return $false }
    $codex = Find-CodexExecutable
    if (-not $codex) { return $false }
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $codex mcp get svacer *> $null
        return ($LASTEXITCODE -eq 0)
    }
    finally { $ErrorActionPreference = $savedPreference }
}

function Invoke-AutomaticSetup {
    $stdoutPath = Join-Path $logDirectory 'setup.stdout.tmp'
    $stderrPath = Join-Path $logDirectory 'setup.stderr.tmp'
    Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    try {
        $process = Start-Process -FilePath 'powershell.exe' -WindowStyle Hidden -PassThru `
            -ArgumentList @(
                '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                '-File', "`"$setup`""
            ) `
            -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
        # Windows PowerShell may lose ExitCode when it opens the process handle
        # only after the child exits. Retain the handle before waiting.
        $setupHandle = $process.Handle
        if (-not $process.WaitForExit($setupTimeoutMilliseconds)) {
            Write-StartupLog 'Automatic setup timed out after 20 minutes; stopping its process tree.'
            & taskkill.exe /PID ([string]$process.Id) /T /F *> $null
            throw 'Automatic setup exceeded 20 minutes and was stopped. Check startup.log and retry.'
        }
        # Flush redirected native output before reading the temporary files.
        $process.WaitForExit()
        foreach ($path in @($stdoutPath, $stderrPath)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
            foreach ($setupLine in Get-Content -LiteralPath $path) {
                if (-not [string]::IsNullOrWhiteSpace($setupLine)) {
                    Write-StartupLog ([string]$setupLine)
                }
            }
        }
        if ($null -eq $process.ExitCode) {
            throw 'Automatic setup finished without a readable exit code.'
        }
        if ($process.ExitCode -ne 0) {
            throw "Automatic setup exited with code $($process.ExitCode)."
        }
    }
    finally {
        Remove-Item -LiteralPath $stdoutPath, $stderrPath -Force -ErrorAction SilentlyContinue
    }
}

try {
    $ownsMutex = $mutex.WaitOne(0)
    if (-not $ownsMutex) { exit 0 }
    if (-not (Test-Path -LiteralPath $logDirectory)) {
        New-Item -ItemType Directory -Path $logDirectory | Out-Null
    }
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 5MB) {
        [IO.File]::Copy($logPath, "$logPath.previous", $true)
        [IO.File]::WriteAllText($logPath, '')
    }
    Remove-Item -LiteralPath $statePath, $readyPath -Force -ErrorAction SilentlyContinue
    Write-StartupLog 'Startup requested.'
    Set-StartupState 'checking'
    Start-StartupSplash

    if (-not (Test-Installation)) {
        Write-StartupLog 'Installation is incomplete; starting automatic setup.'
        Set-StartupState 'installing'
        Invoke-AutomaticSetup
        if (-not (Test-Installation)) {
            throw 'Automatic installation did not pass the startup checks.'
        }
        Write-StartupLog 'Automatic setup completed successfully.'
    }
    else {
        Write-StartupLog 'Installation check passed.'
    }

    if (-not (Test-Path -LiteralPath $gui -PathType Leaf)) {
        throw 'The graphical application file is missing.'
    }
    Write-StartupLog 'Launching the graphical application.'
    Set-StartupState 'starting'
    $guiProcess = Start-Process -FilePath $pythonw -WorkingDirectory $toolDirectory -PassThru `
        -ArgumentList @("`"$gui`"", '--startup-ready-file', "`"$readyPath`"")
    $readyDeadline = [DateTime]::UtcNow.AddSeconds(60)
    while (-not (Test-Path -LiteralPath $readyPath -PathType Leaf)) {
        if ($guiProcess.HasExited) {
            throw "The graphical application exited before opening (code $($guiProcess.ExitCode))."
        }
        if ([DateTime]::UtcNow -ge $readyDeadline) {
            throw 'The graphical application did not signal readiness within 60 seconds.'
        }
        Start-Sleep -Milliseconds 150
        $guiProcess.Refresh()
    }
    Remove-Item -LiteralPath $readyPath -Force -ErrorAction SilentlyContinue
    Write-StartupLog 'Graphical application opened successfully.'
    Set-StartupState 'success'
}
catch {
    $safeMessage = 'Automatic startup failed.'
    Write-StartupLog "$safeMessage $($_.Exception.GetType().Name): $($_.Exception.Message)"
    Set-StartupState 'error'
    Show-StartupError $safeMessage
    exit 1
}
finally {
    if ($ownsMutex) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
