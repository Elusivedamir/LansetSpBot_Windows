@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "MODE="
if /I "%~1"=="--execute" set "MODE=-Execute"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_ACCOUNT_ACTIVITY_EXPERIMENTAL.ps1" %MODE%
set "ACTIVITY_EXIT=%ERRORLEVEL%"

if not "%ACTIVITY_EXIT%"=="0" (
    echo.
    echo Account activity runner exited with code: %ACTIVITY_EXIT%
    pause
)
exit /b %ACTIVITY_EXIT%
