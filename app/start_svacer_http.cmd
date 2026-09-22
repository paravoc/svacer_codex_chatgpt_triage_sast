@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_svacer_http.ps1" %*
set "triage_exit=%errorlevel%"
if not "%triage_exit%"=="0" (
  echo.
  echo Svacer MCP failed. See the error above.
  pause
)
exit /b %triage_exit%
