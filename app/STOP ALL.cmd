@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_components.ps1" -Mode All
set "rc=%ERRORLEVEL%"
echo.
pause
exit /b %rc%
