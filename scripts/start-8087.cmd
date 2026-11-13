@echo off
setlocal
echo Starting only the 8087 experimental service...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-8087.ps1"
echo.
pause
