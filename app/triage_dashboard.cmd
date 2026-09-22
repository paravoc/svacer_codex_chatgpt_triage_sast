@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0triage_dashboard.ps1" %*
set "rc=%ERRORLEVEL%"
endlocal & exit /b %rc%
