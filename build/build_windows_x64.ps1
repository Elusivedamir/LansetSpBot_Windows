[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$KeepBuildVenv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location -LiteralPath $ProjectRoot

if ($env:OS -ne "Windows_NT") {
    throw "This build must run on 64-bit Windows."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows host is required."
}

$py = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($null -eq $py) {
    throw "Python Launcher was not found. Install Python 3.13 x64."
}
$PythonArgs = @("-3.13-64")
$probe = 'import struct,sys; assert sys.version_info[:2] == (3,13); assert 8*struct.calcsize("P") == 64; print(sys.executable)'
$PythonExecutable = & $py.Source @PythonArgs -c $probe
if ($LASTEXITCODE -ne 0 -or -not $PythonExecutable) {
    throw "Python 3.13 x64 is required for the reproducible Windows build."
}
Write-Host "[LansetSpBot build] Python: $PythonExecutable" -ForegroundColor Cyan

$BuildVenv = Join-Path $ProjectRoot ".venv-build-windows-x64"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
if (Test-Path -LiteralPath $BuildVenv) {
    if (-not (Test-Path -LiteralPath (Join-Path $BuildVenv "pyvenv.cfg") -PathType Leaf)) {
        throw "Refusing to remove an unexpected path: $BuildVenv"
    }
    if (-not $KeepBuildVenv) {
        Remove-Item -LiteralPath $BuildVenv -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $BuildPython -PathType Leaf)) {
    $venvArgs = @($PythonArgs) + @("-m", "venv", $BuildVenv)
    & $py.Source @venvArgs
    if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
}

& $BuildPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw "ensurepip failed." }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r requirements-bootstrap.txt
if ($LASTEXITCODE -ne 0) { throw "Bootstrap dependency installation failed." }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r requirements-runtime.lock
if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed." }
& $BuildPython -m pip install --disable-pip-version-check --retries 8 --timeout 60 -r requirements-openai.txt
if ($LASTEXITCODE -ne 0) { throw "OpenAI SDK installation failed." }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r requirements-build-windows-x64.lock
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
if (-not $SkipTests) {
    & $BuildPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r requirements-dev-windows-x64.lock
    if ($LASTEXITCODE -ne 0) { throw "Test dependency installation failed." }
}

Get-ChildItem -LiteralPath $ProjectRoot -Directory -Recurse -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath ".pytest_cache", ".mypy_cache", ".ruff_cache", ".coverage", "coverage.json" -Recurse -Force -ErrorAction SilentlyContinue

# The guide images are release assets, not manually maintained screenshots.
# Refresh them from the real current widgets after PySide6 is installed and
# before tests, manifest validation or PyInstaller can package stale PNG files.
Write-Host "[LansetSpBot build] Refreshing instruction screenshots..." -ForegroundColor Cyan
$OldInstructionQt = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $BuildPython tools\capture_instruction_screenshots.py
    if ($LASTEXITCODE -ne 0) {
        throw "Instruction screenshot regeneration failed."
    }
}
finally {
    if ($null -eq $OldInstructionQt) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $OldInstructionQt
    }
}

# Screenshot bytes are part of SHA256SUMS.txt. Refresh the manifest immediately,
# so the following tests validate the exact assets that will be packaged.
& $BuildPython tools\generate_manifest.py
if ($LASTEXITCODE -ne 0) {
    throw "Manifest regeneration after instruction screenshots failed."
}

if (-not $SkipTests) {
    $env:QT_QPA_PLATFORM = "offscreen"
    # Run under coverage using only locked dev dependencies: the hash-locked
    # dev graph ships `coverage` and `pytest`, but not `pytest-cov`.
    & $BuildPython -m coverage run -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "pytest failed." }
    # tools/check_critical_coverage.py enforces per-module minimums for the
    # release-critical code. It was previously shipped but never executed, so a
    # module could silently lose its test coverage between releases.
    & $BuildPython -m coverage json -o coverage.json -q
    if ($LASTEXITCODE -ne 0) { throw "Coverage report generation failed." }
    & $BuildPython tools\check_critical_coverage.py
    if ($LASTEXITCODE -ne 0) { throw "Critical coverage gate failed." }
    & $BuildPython -m compileall -q core services storage workers gui main.py tests tools build
    if ($LASTEXITCODE -ne 0) { throw "compileall failed." }
    & $BuildPython -m ruff check core services storage workers gui main.py tests tools build
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }
    & $BuildPython -m mypy --config-file mypy.ini core services storage workers gui main.py
    if ($LASTEXITCODE -ne 0) { throw "Mypy failed." }
    & $BuildPython main.py --self-test
    if ($LASTEXITCODE -ne 0) { throw "Source self-test failed." }
    # A lock generated on one interpreter installs there and fails the hash
    # check on the other, which the user sees as a tampering warning. Both
    # supported interpreters must resolve before a release ships.
    & $BuildPython tools\check_lock_coverage.py
    if ($LASTEXITCODE -ne 0) { throw "Runtime lock does not cover every supported Python version." }
}

& $BuildPython build\generate_windows_version_info.py
if ($LASTEXITCODE -ne 0) { throw "Windows version resource generation failed." }
if (-not (Test-Path -LiteralPath "build\assets\LansetSpBot.ico" -PathType Leaf)) {
    throw "Windows icon build/assets/LansetSpBot.ico is missing."
}

$AppName = & $BuildPython -c "from core.version import APP_NAME; print(APP_NAME)"
$AppVersion = & $BuildPython -c "from core.version import __version__; print(__version__)"
$BuiltDir = Join-Path $ProjectRoot ("dist\" + $AppName)
$BuiltExe = Join-Path $BuiltDir ($AppName + ".exe")
Remove-Item -LiteralPath $BuiltDir, "build\windows-work" -Recurse -Force -ErrorAction SilentlyContinue

& $BuildPython -m PyInstaller --clean --noconfirm --workpath "build\windows-work" "build\LansetSpBot.windows.spec"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
    throw "PyInstaller did not create $BuiltExe"
}

$oldQt = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
$RelocationRoot = $null
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $BuiltExe --self-test
    if ($LASTEXITCODE -ne 0) { throw "Packaged self-test failed." }

    $RelocationRoot = Join-Path $env:TEMP ("LansetSpBot Проверка " + [guid]::NewGuid().ToString("N"))
    $RelocatedDir = Join-Path $RelocationRoot $AppName
    New-Item -ItemType Directory -Path $RelocatedDir -Force | Out-Null
    Copy-Item -Path (Join-Path $BuiltDir "*") -Destination $RelocatedDir -Recurse -Force
    & (Join-Path $RelocatedDir ($AppName + ".exe")) --self-test
    if ($LASTEXITCODE -ne 0) { throw "Relocated packaged self-test failed." }
}
finally {
    if ($null -eq $oldQt) { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue } else { $env:QT_QPA_PLATFORM = $oldQt }
    if ($RelocationRoot -and (Test-Path -LiteralPath $RelocationRoot)) {
        Remove-Item -LiteralPath $RelocationRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$ReleaseParent = Join-Path $ProjectRoot "dist\windows-release"
$ReleaseRoot = Join-Path $ReleaseParent ($AppName + "-Windows-x64")
$ZipPath = Join-Path $ProjectRoot ("dist\" + $AppName + "-Windows-x64.zip")
$ChecksumsPath = Join-Path $ProjectRoot ("dist\" + $AppName + "-Windows-x64-SHA256SUMS.txt")
$SbomPath = Join-Path $ProjectRoot ("dist\" + $AppName + "-Windows-x64-SBOM.cdx.json")
Remove-Item -LiteralPath $ReleaseParent, $ZipPath, $ChecksumsPath, $SbomPath -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
Copy-Item -Path (Join-Path $BuiltDir "*") -Destination $ReleaseRoot -Recurse -Force
Copy-Item -LiteralPath "WINDOWS_X64_README.txt" -Destination $ReleaseRoot
Set-Content -LiteralPath (Join-Path $ReleaseRoot "1_START_LANSETSPBOT.bat") -Encoding Ascii -Value "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0$AppName.exe`"`r`n"

& $BuildPython build\generate_sbom.py --version $AppVersion --requirements requirements-runtime.lock --requirements requirements-openai.txt --name $AppName --output $SbomPath
if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed." }
Copy-Item -LiteralPath $SbomPath -Destination $ReleaseRoot
Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { throw "Release ZIP was not created." }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ChecksumsPath -Encoding Ascii -Value "$hash  $([System.IO.Path]::GetFileName($ZipPath))"

Write-Host "[LansetSpBot build] Windows x64 release created:" -ForegroundColor Green
Write-Host "  $ZipPath"
Write-Host "  $ChecksumsPath"
Write-Host "  $SbomPath"
