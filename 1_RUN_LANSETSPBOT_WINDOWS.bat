@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_FROM_SOURCE_WINDOWS.ps1"
set "MARLEN_EXIT=%ERRORLEVEL%"

if not "%MARLEN_EXIT%"=="0" (
    echo.
    echo LansetSpBot could not start. Error code: %MARLEN_EXIT%
    echo Read README.txt and the message above.
    pause
)
exit /b %MARLEN_EXIT%
