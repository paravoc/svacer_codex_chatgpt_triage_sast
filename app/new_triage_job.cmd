@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0new_triage_job.ps1" %*
set "triage_exit=%errorlevel%"
if not "%triage_exit%"=="0" (
  echo.
  echo Job creation failed. See the error above.
  pause
)
exit /b %triage_exit%
