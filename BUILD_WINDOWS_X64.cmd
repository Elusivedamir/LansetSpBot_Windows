@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where winget.exe >nul 2>nul || (
  echo ERROR: winget is required. Install or update Microsoft App Installer.
  pause
  exit /b 2
)
where git.exe >nul 2>nul || winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
where py.exe >nul 2>nul || winget install --id Python.Launcher -e --source winget --accept-package-agreements --accept-source-agreements
py.exe -3.13-64 -c "import struct,sys;assert sys.version_info[:2]==(3,13) and struct.calcsize('P')==8" >nul 2>nul || winget install --id Python.Python.3.13 -e --architecture x64 --source winget --accept-package-agreements --accept-source-agreements

set "PATH=%LocalAppData%\Programs\Python\Launcher;%LocalAppData%\Programs\Git\cmd;%ProgramFiles%\Git\cmd;%PATH%"
set "GIT_CONFIG_COUNT=1"
set "GIT_CONFIG_KEY_0=safe.directory"
set "GIT_CONFIG_VALUE_0=%CD%"
where git.exe >nul 2>nul || goto :missing
where py.exe >nul 2>nul || goto :missing
py.exe -3.13-64 -c "import struct,sys;assert sys.version_info[:2]==(3,13) and struct.calcsize('P')==8" >nul 2>nul || goto :missing

py.exe -3.13-64 tools\generate_manifest.py
if errorlevel 1 goto :failed
if not exist ".git" (
  git init || goto :failed
  git config user.name "LansetSpBot Local Builder" || goto :failed
  git config user.email "local-builder@invalid" || goto :failed
  git add -A || goto :failed
  git commit -m "Local source snapshot for reproducible Windows build" || goto :failed
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0build\build_windows_x64.ps1"
set "MARLEN_EXIT=%ERRORLEVEL%"
if not "%MARLEN_EXIT%"=="0" pause
exit /b %MARLEN_EXIT%

:missing
echo ERROR: Python 3.13 x64, Python Launcher, or Git is still unavailable.
echo Restart Windows and run this file again.
:failed
pause
exit /b 1
