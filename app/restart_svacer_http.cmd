@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart_svacer_http.ps1"
exit /b %ERRORLEVEL%
