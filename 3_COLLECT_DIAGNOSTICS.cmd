@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "MARLEN_PY=%~dp0.venv-windows-x64\Scripts\python.exe"
if not exist "%MARLEN_PY%" (
    echo Virtual environment not found. Run 1_RUN_LANSETSPBOT_WINDOWS.bat first,
    echo or install dependencies as described in README.txt.
    echo Falling back to the Python Launcher.
    set "MARLEN_PY=py"
)

"%MARLEN_PY%" "%~dp0tools\collect_diagnostics.py" %*
set "MARLEN_EXIT=%ERRORLEVEL%"
if not "%MARLEN_EXIT%"=="0" (
    echo.
    echo Diagnostics collection failed. Error code: %MARLEN_EXIT%
)
pause
exit /b %MARLEN_EXIT%
