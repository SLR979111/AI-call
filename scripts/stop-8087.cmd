@echo off
setlocal
echo Stopping only the 8087 experimental service...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-8087.ps1"
echo.
pause
