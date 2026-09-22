@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_components.ps1" -Mode Analyze
set "rc=%ERRORLEVEL%"
echo.
pause
exit /b %rc%
