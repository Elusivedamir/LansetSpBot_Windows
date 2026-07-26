@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0RUN_FROM_SOURCE_WINDOWS_DIRECT_314.ps1"
set "MARLEN_EXIT=%ERRORLEVEL%"
if not "%MARLEN_EXIT%"=="0" (
    echo.
    echo LansetSpBot direct Python 3.14 launch failed. Error code: %MARLEN_EXIT%
    pause
)
exit /b %MARLEN_EXIT%
