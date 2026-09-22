@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "%~dp0..\.venv\Scripts\python.exe" (
  "%~dp0..\.venv\Scripts\python.exe" "%~dp0extract_gost_markers.py" %*
) else (
  python "%~dp0extract_gost_markers.py" %*
)
set "extract_exit=%errorlevel%"
echo.
if not "%extract_exit%"=="0" (
  echo Failed. See the error above.
) else (
  echo Done.
)
pause
exit /b %extract_exit%
