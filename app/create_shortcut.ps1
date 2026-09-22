param([switch]$RootOnly)

$ErrorActionPreference = 'Stop'
$toolDirectory = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $toolDirectory 'START.vbs'
$wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
$icon = Join-Path $PSScriptRoot 'assets\svacer-triage-v2.ico'

foreach ($required in @($launcher, $wscript, $icon)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Shortcut dependency is missing: $required"
    }
}

$shell = New-Object -ComObject WScript.Shell
function New-SvacerShortcut {
    param([Parameter(Mandatory = $true)][string]$Path)
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $wscript
    $shortcut.Arguments = "`"$launcher`""
    $shortcut.WorkingDirectory = $toolDirectory
    $shortcut.IconLocation = "$icon,0"
    $shortcut.Description = 'Svacer Triage'
    $shortcut.WindowStyle = 1
    $shortcut.Save()
}

New-SvacerShortcut -Path (Join-Path $toolDirectory 'START.lnk')
if (-not $RootOnly) {
    $programs = [Environment]::GetFolderPath('Programs')
    if (-not [string]::IsNullOrWhiteSpace($programs)) {
        New-SvacerShortcut -Path (Join-Path $programs 'Svacer Triage.lnk')
    }
}
