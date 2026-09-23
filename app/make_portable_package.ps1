$ErrorActionPreference = "Stop"

# Only this explicit allowlist is packaged. Runtime data and credentials are excluded.
$toolDirectory = Split-Path -Parent $PSScriptRoot
$rootFiles = @('START.cmd', 'START.vbs', 'README.md', 'LICENSE', 'pyproject.toml', 'poetry.lock')
$docsFiles = @(
    'demo.mp4', 'demo-preview.gif', 'record_demo.py', 'render_screenshots.py',
    'screenshots/overview.png', 'screenshots/markers.png',
    'screenshots/in-progress.png', 'screenshots/notifications.png',
    'screenshots/history.png', 'screenshots/settings.png',
    'screenshots/multi-select.png', 'CONNECTOR.md', 'PROMPTING.md', 'AUTOMATIC_ANALYSIS.md'
)
$appFiles = @(
    'START.ps1', 'bootstrap.ps1', 'startup_splash.ps1', 'CODEX_TASK.md',
    'assets/svacer-triage.svg', 'assets/svacer-triage-v2.ico',
    'assets/svacer-hamster-loading.gif',
    'new_triage_job.ps1', 'new_triage_job.cmd',
    'setup_mcp.ps1', 'setup_mcp.cmd', 'start_svacer_http.ps1', 'start_svacer_http.py',
    'start_svacer_http.cmd', 'restart_svacer_http.ps1', 'restart_svacer_http.cmd',
    'stop_components.ps1', 'STOP SVACER.cmd', 'STOP ANALYZE.cmd', 'STOP ALL.cmd',
    'triage_dashboard.py', 'triage_dashboard.ps1', 'triage_dashboard.cmd',
    'triage_gui.py', 'triage_gui_qt.py',
    'svacer_connection.py', 'svacer_login_qt.py', 'svacer_login_app.py',
    'triage_connector/__init__.py', 'triage_connector/client.py', 'triage_connector/service.py',
    'triage_connector/markup.py', 'triage_connector/server.py',
    'start_svacer_stdio.py', 'start_svacer_mcp.ps1',
    'desktop_theme.py', 'marker_history.py', 'marker_notifications.py', 'decision_quality.py', 'comment_format.py', 'import_selection.py',
    'triage_gui.ps1', 'triage_gui.cmd', 'codex_run.py',
    'parallel_analysis.py', 'continuous_analysis.py', 'analysis_scope.py', 'analysis_campaign.py', 'usage_guard.py',
    'dependency_sources.py', 'investigation.py', 'result_transport.py', 'source_inspect.py', 'web_research.py',
    'make_portable_package.ps1', 'make_portable_package.cmd',
    'triage_queue.py', 'local_jobs.py', 'project_setup.py', 'project_setup_qt.py',
    'create_shortcut.ps1', 'make_windows_icon.py',
    'make_mcp_decisions_template.py', 'validate_mcp_decisions.py',
    'export_decisions_csv.py', 'extract_gost_markers.py', 'run_extractor.cmd',
    'make_decisions_template.py', 'validate_decisions.py', 'svacer-settings.example.json'
)
$files = @($rootFiles) + @($docsFiles | ForEach-Object { "docs/$_" }) +
    @($appFiles | ForEach-Object { "app/$_" })
$rootPath = [IO.Path]::GetFullPath($toolDirectory)

foreach ($relative in $files) {
    $item = Get-Item -LiteralPath (Join-Path $rootPath $relative)
    if ($item.PSIsContainer) { throw "Expected file: $relative" }
    $node = $item
    while ($node -and $node.FullName -ne $rootPath) {
        if ($node.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Package input must not be a link: $relative"
        }
        if ($node -is [IO.FileInfo]) { $node = $node.Directory } else { $node = $node.Parent }
    }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$stamp = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$archiveDirectory = Join-Path $toolDirectory "ARCHIVE"
if (-not (Test-Path -LiteralPath $archiveDirectory)) {
    New-Item -ItemType Directory -Path $archiveDirectory | Out-Null
}
$archivePath = Join-Path $archiveDirectory "svacer_gost_triage_portable_$stamp.zip"
$archiveStream = [IO.File]::Open($archivePath, [IO.FileMode]::CreateNew)
try {
    $zip = New-Object IO.Compression.ZipArchive($archiveStream, [IO.Compression.ZipArchiveMode]::Create, $true)
    try {
        $null = $zip.CreateEntry("svacer_gost_triage/RESULTS/")
        foreach ($relative in $files) {
            $null = [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $zip, (Join-Path $rootPath $relative),
                "svacer_gost_triage/$relative", [IO.Compression.CompressionLevel]::Optimal)
        }
    }
    finally { $zip.Dispose() }
}
catch {
    Write-Warning 'Packaging failed. Do not distribute the incomplete ZIP.'
    throw
}
finally { $archiveStream.Dispose() }
Write-Host "Переносимый архив создан: $archivePath"
Write-Host "На другом компьютере распакуйте архив и запустите START.vbs (START.cmd оставлен для совместимости)."
