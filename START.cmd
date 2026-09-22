@echo off
rem Compatibility launcher. START.vbs and installed shortcuts are fully windowless;
rem this file immediately hands off to the same background bootstrapper.
start "" wscript.exe "%~dp0START.vbs"
exit /b 0
