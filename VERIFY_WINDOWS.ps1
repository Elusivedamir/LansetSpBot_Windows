$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $venv = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path $venv) {
        & $venv @Arguments
    } else {
        & py -3.13 @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Arguments -join ' ')"
    }
}

Write-Host "[1/8] Manifest" -ForegroundColor Cyan
Invoke-Python tools/generate_manifest.py --check

Write-Host "[2/8] Compile all" -ForegroundColor Cyan
Invoke-Python -m compileall -q core gui services storage workers tests tools

Write-Host "[3/8] Ruff" -ForegroundColor Cyan
Invoke-Python -m ruff check .

Write-Host "[4/8] Mypy" -ForegroundColor Cyan
Invoke-Python -m mypy core gui services storage workers tools

Write-Host "[5/8] Focused regression tests" -ForegroundColor Cyan
Invoke-Python -m pytest -q `
    tests/test_master_ui_link_fixes.py `
    tests/test_account_import_service.py `
    tests/test_v4713_request_floor_and_wait_buffer.py `
    tests/test_v491_popup_contrast.py `
    tests/test_gui_v45.py `
    tests/test_v4721_activity_panel_lifecycle_v17.py `
    tests/test_v4721_account_floodwait_link_stop_v6.py `
    tests/test_v4721_account_restrictions_v11.py

Write-Host "[6/8] Official Windows source CI" -ForegroundColor Cyan
& powershell -NoProfile -ExecutionPolicy Bypass -File tools/run_windows_source_ci.ps1
if ($LASTEXITCODE -ne 0) { throw "Official Windows source CI failed" }

Write-Host "[7/8] Windows x64 release build" -ForegroundColor Cyan
& cmd /c BUILD_WINDOWS_X64.cmd
if ($LASTEXITCODE -ne 0) { throw "Windows x64 build failed" }

Write-Host "[8/8] Repository state" -ForegroundColor Cyan
& git status --short
& git rev-parse HEAD
Write-Host "Проверка завершена. Smoke/release proof выполняются существующими build-скриптами проекта." -ForegroundColor Green
