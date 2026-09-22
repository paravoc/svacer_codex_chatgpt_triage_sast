param(
    [Parameter(Mandatory = $true)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][string]$StatePath,
    [Parameter(Mandatory = $true)][string]$GifPath,
    [string]$IconPath = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SvacerSplashNativeMethods {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
}
"@
[Windows.Forms.Application]::EnableVisualStyles()
[Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

$form = New-Object Windows.Forms.Form
$form.Text = 'Запуск Svacer Triage'
$form.StartPosition = [Windows.Forms.FormStartPosition]::CenterScreen
$form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ShowInTaskbar = $false
$form.TopMost = $true
$form.ClientSize = New-Object Drawing.Size(460, 520)
$form.BackColor = [Drawing.Color]::FromArgb(17, 19, 23)
$form.ForeColor = [Drawing.Color]::FromArgb(231, 233, 237)
$form.Font = New-Object Drawing.Font('Segoe UI', 10)

$ownedIcon = $null
if ($IconPath -and (Test-Path -LiteralPath $IconPath -PathType Leaf)) {
    try {
        $ownedIcon = New-Object Drawing.Icon($IconPath)
        $form.Icon = $ownedIcon
    }
    catch { $ownedIcon = $null }
}

$title = New-Object Windows.Forms.Label
$title.Text = 'Svacer Triage запускается'
$title.Font = New-Object Drawing.Font('Segoe UI Semibold', 17)
$title.AutoSize = $false
$title.TextAlign = [Drawing.ContentAlignment]::MiddleCenter
$title.Location = New-Object Drawing.Point(20, 18)
$title.Size = New-Object Drawing.Size(420, 42)
$form.Controls.Add($title)

$picture = New-Object Windows.Forms.PictureBox
$picture.Location = New-Object Drawing.Point(70, 66)
$picture.Size = New-Object Drawing.Size(320, 320)
$picture.SizeMode = [Windows.Forms.PictureBoxSizeMode]::Zoom
$picture.BackColor = $form.BackColor
$ownedImage = $null
if (Test-Path -LiteralPath $GifPath -PathType Leaf) {
    try {
        $ownedImage = [Drawing.Image]::FromFile($GifPath)
        $picture.Image = $ownedImage
    }
    catch { $ownedImage = $null }
}
$form.Controls.Add($picture)

$status = New-Object Windows.Forms.Label
$status.Text = 'Проверяю установку…'
$status.Font = New-Object Drawing.Font('Segoe UI Semibold', 11)
$status.ForeColor = [Drawing.Color]::FromArgb(99, 214, 169)
$status.AutoSize = $false
$status.TextAlign = [Drawing.ContentAlignment]::MiddleCenter
$status.Location = New-Object Drawing.Point(20, 397)
$status.Size = New-Object Drawing.Size(420, 30)
$form.Controls.Add($status)

$detail = New-Object Windows.Forms.Label
$detail.Text = 'При первом запуске установка зависимостей может занять несколько минут.'
$detail.ForeColor = [Drawing.Color]::FromArgb(157, 165, 176)
$detail.AutoSize = $false
$detail.TextAlign = [Drawing.ContentAlignment]::TopCenter
$detail.Location = New-Object Drawing.Point(30, 430)
$detail.Size = New-Object Drawing.Size(400, 42)
$form.Controls.Add($detail)

$progress = New-Object Windows.Forms.ProgressBar
$progress.Style = [Windows.Forms.ProgressBarStyle]::Marquee
$progress.MarqueeAnimationSpeed = 24
$progress.Location = New-Object Drawing.Point(30, 484)
$progress.Size = New-Object Drawing.Size(400, 8)
$form.Controls.Add($progress)

$script:startedAt = [DateTime]::UtcNow
$script:terminalAt = $null
$script:parentMissingAt = $null
$script:lastPhase = ''

function Read-StartupPhase {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) { return '' }
    try {
        $line = Get-Content -LiteralPath $StatePath -TotalCount 1 -ErrorAction Stop
        return ([string]$line).Trim().ToLowerInvariant()
    }
    catch { return '' }
}

function Set-PhaseAppearance {
    param([Parameter(Mandatory = $true)][string]$Phase)
    switch ($Phase) {
        'checking' {
            $status.Text = 'Проверяю установку…'
            $detail.Text = 'Проверяю Python, зависимости, Codex и локальный коннектор.'
        }
        'installing' {
            $status.Text = 'Устанавливаю и настраиваю…'
            $detail.Text = 'Первый запуск может занять несколько минут. Подробности сохраняются в logs\startup.log.'
        }
        'starting' {
            $status.Text = 'Открываю приложение…'
            $detail.Text = 'Установка готова. Ожидаю появления главного окна.'
        }
        'success' {
            $status.Text = 'Готово — приложение открыто'
            $status.ForeColor = [Drawing.Color]::FromArgb(99, 214, 169)
            $detail.Text = 'Можно начинать работу.'
            $progress.Style = [Windows.Forms.ProgressBarStyle]::Continuous
            $progress.Value = 100
            if ($null -eq $script:terminalAt) { $script:terminalAt = [DateTime]::UtcNow }
        }
        'error' {
            $status.Text = 'Запуск не завершён'
            $status.ForeColor = [Drawing.Color]::FromArgb(237, 146, 146)
            $detail.Text = 'Причина показана в отдельном сообщении и записана в logs\startup.log.'
            $progress.Style = [Windows.Forms.ProgressBarStyle]::Continuous
            $progress.Value = 0
            if ($null -eq $script:terminalAt) { $script:terminalAt = [DateTime]::UtcNow }
        }
    }
}

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 180
$timer.Add_Tick({
    $phase = Read-StartupPhase
    if ($phase -and $phase -ne $script:lastPhase) {
        $script:lastPhase = $phase
        Set-PhaseAppearance $phase
    }

    if ($null -ne $script:terminalAt) {
        $terminalAge = ([DateTime]::UtcNow - $script:terminalAt).TotalMilliseconds
        $totalAge = ([DateTime]::UtcNow - $script:startedAt).TotalMilliseconds
        if ($terminalAge -ge 900 -and $totalAge -ge 1700) { $form.Close() }
        return
    }

    if (Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue) {
        $script:parentMissingAt = $null
        return
    }
    if ($null -eq $script:parentMissingAt) {
        $script:parentMissingAt = [DateTime]::UtcNow
        return
    }
    if (([DateTime]::UtcNow - $script:parentMissingAt).TotalMilliseconds -ge 1800) {
        Set-PhaseAppearance 'error'
    }
})

$form.Add_Shown({ $timer.Start() })
$form.Add_FormClosed({
    $timer.Stop()
    $timer.Dispose()
    if ($ownedImage) { $picture.Image = $null; $ownedImage.Dispose() }
    if ($ownedIcon) { $ownedIcon.Dispose() }
})

$form.Show()
# The bootstrap PowerShell process has a hidden console. Explicitly reveal only
# the WinForms window after its handle exists.
[void][SvacerSplashNativeMethods]::ShowWindow($form.Handle, 5)
[void][Windows.Forms.Application]::Run($form)
