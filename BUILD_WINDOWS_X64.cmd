@echo off
setlocal EnableExtensions
cd /d "%~dp0"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build\build_windows_x64.ps1"
set "MARLEN_EXIT=%ERRORLEVEL%"
if not "%MARLEN_EXIT%"=="0" pause
exit /b %MARLEN_EXIT%
