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
$ReleaseReadme = Join-Path $ProjectRoot "README.txt"

if ($env:OS -ne "Windows_NT") {
    throw "This build must run on 64-bit Windows."
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "A 64-bit Windows host is required."
}
if (-not (Test-Path -LiteralPath $ReleaseReadme -PathType Leaf)) {
    throw "Release documentation is missing: $ReleaseReadme"
}

$py = Get-Command "py.exe" -ErrorAction SilentlyContinue
if ($null -eq $py) {
    throw "Python Launcher was not found. Install Python 3.13 x64."
}
$PythonArgs = @("-3.13-64")
$probe = "import struct,sys; assert sys.version_info[:2] == (3,13); assert 8*struct.calcsize('P') == 64; print(sys.executable)"
$PythonExecutable = & $py.Source @PythonArgs -c $probe
if ($LASTEXITCODE -ne 0 -or -not $PythonExecutable) {
    throw "Python 3.13 x64 is required for the reproducible Windows build."
}
Write-Host "[LansetSpBot build] Python: $PythonExecutable" -ForegroundColor Cyan

function Write-BuildStage {
    param([Parameter(Mandatory = $true)][string]$Name)
    Write-Host "[LansetSpBot build][stage] $Name" -ForegroundColor Cyan
}


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

Write-BuildStage "Preparing isolated build environment"
& $BuildPython -m ensurepip --upgrade
if ($LASTEXITCODE -ne 0) { throw "ensurepip failed." }
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r requirements-bootstrap.txt
if ($LASTEXITCODE -ne 0) { throw "Bootstrap dependency installation failed." }
Write-BuildStage "Installing runtime dependencies"
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r requirements-runtime.lock
if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed." }
Write-BuildStage "Installing OpenAI dependencies"
& $BuildPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r requirements-openai.lock
if ($LASTEXITCODE -ne 0) { throw "OpenAI SDK installation failed." }
& $BuildPython tools\generate_openai_lock.py --output requirements-openai.lock --check
if ($LASTEXITCODE -ne 0) { throw "OpenAI lock verification failed." }
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
    $env:PYTHONFAULTHANDLER = "1"

    # Run the non-GUI and GUI-heavy suites in separate interpreter processes.
    # Each suite stops at its first assertion failure and prints a complete
    # traceback. The watchdog terminates a single hung test after 180 seconds
    # while dumping every Python thread to the live CI log.
    & $BuildPython -m coverage erase
    if ($LASTEXITCODE -ne 0) { throw "Could not reset coverage data." }

    # The in-process faulthandler cannot terminate a native Qt/SQLite deadlock.
    # Run pytest behind an external parent process as well. It relays output live,
    # records the last started node, and kills the complete child tree after four
    # silent minutes instead of leaving the GitHub runner stuck indefinitely.
    $pytestErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        Write-BuildStage "Running core pytest diagnostics"
        $env:PYTEST_CURRENT_TEST_FILE = "ci-proof\pytest-core-current-test.txt"
        & $BuildPython tools\run_ci_subprocess.py `
            --label core `
            --log "ci-proof\pytest-core.log" `
            --idle-timeout-seconds 660 `
            --total-timeout-seconds 3600 `
            -- `
            $BuildPython -X faulthandler -m coverage run --parallel-mode -m pytest `
            -vv --tb=long --showlocals -ra --durations=20 `
            -p tools.pytest_ci_watchdog `
            --junitxml "ci-proof\pytest-core.xml" `
            --ignore "tests/test_gui_v45.py" tests
        $coreTestsExit = $LASTEXITCODE

        Write-BuildStage "Running GUI pytest diagnostics in isolated process"
        $env:PYTEST_CURRENT_TEST_FILE = "ci-proof\pytest-gui-current-test.txt"
        & $BuildPython tools\run_ci_subprocess.py `
            --label gui `
            --log "ci-proof\pytest-gui.log" `
            --idle-timeout-seconds 240 `
            --total-timeout-seconds 900 `
            -- `
            $BuildPython -X faulthandler -m coverage run --parallel-mode -m pytest `
            -vv --tb=long --showlocals -ra --durations=20 `
            -p tools.pytest_ci_watchdog `
            --junitxml "ci-proof\pytest-gui.xml" `
            "tests/test_gui_v45.py"
        $guiTestsExit = $LASTEXITCODE
    }
    finally {
        Remove-Item Env:PYTEST_CURRENT_TEST_FILE -ErrorAction SilentlyContinue
        $ErrorActionPreference = $pytestErrorActionPreference
    }

    # Preserve partial coverage even when either diagnostic suite fails. This is
    # evidence only; the release gate below still fails closed on any test error.
    & $BuildPython -m coverage combine
    $coverageCombineExit = $LASTEXITCODE
    if ($coverageCombineExit -eq 0) {
        & $BuildPython -m coverage json -o coverage.json -q
        $coverageJsonExit = $LASTEXITCODE
    }
    else {
        $coverageJsonExit = 1
    }

    @(
        "core_exit=$coreTestsExit"
        "gui_exit=$guiTestsExit"
        "coverage_combine_exit=$coverageCombineExit"
        "coverage_json_exit=$coverageJsonExit"
    ) | Set-Content -LiteralPath "ci-proof\pytest-diagnostics-summary.txt" -Encoding UTF8

    if ($coreTestsExit -ne 0 -or $guiTestsExit -ne 0) {
        throw "pytest diagnostics failed: core=$coreTestsExit gui=$guiTestsExit. See ci-proof pytest logs and JUnit reports."
    }
    if ($coverageCombineExit -ne 0 -or $coverageJsonExit -ne 0) {
        throw "Coverage report generation failed after split pytest runs."
    }

    # tools/check_critical_coverage.py enforces per-module minimums for the
    # release-critical code. It was previously shipped but never executed, so a
    # module could silently lose its test coverage between releases.
    & $BuildPython tools\check_critical_coverage.py
    if ($LASTEXITCODE -ne 0) { throw "Critical coverage gate failed." }
    & $BuildPython -m compileall -q core services storage workers gui main.py tests tools build
    if ($LASTEXITCODE -ne 0) { throw "compileall failed." }
    & $BuildPython -m ruff check core services storage workers gui main.py tests tools build
    if ($LASTEXITCODE -ne 0) { throw "Ruff failed." }
    Write-BuildStage "Running Mypy"
    & $BuildPython -m mypy --config-file mypy.ini core services storage workers gui main.py
    if ($LASTEXITCODE -ne 0) { throw "Mypy failed." }
    Write-BuildStage "Running source self-test"
    & $BuildPython main.py --self-test
    if ($LASTEXITCODE -ne 0) { throw "Source self-test failed." }
    # A lock generated on one interpreter installs there and fails the hash
    # check on the other, which the user sees as a tampering warning. Both
    # supported interpreters must resolve before a release ships.
    Write-BuildStage "Checking runtime lock coverage"
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

Write-BuildStage "Building Windows application with PyInstaller"
& $BuildPython -m PyInstaller --clean --noconfirm --workpath "build\windows-work" "build\LansetSpBot.windows.spec"
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
    throw "PyInstaller did not create $BuiltExe"
}

$oldQt = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
$oldAppData = [Environment]::GetEnvironmentVariable("APPDATA", "Process")
$oldCanonicalDataDir = [Environment]::GetEnvironmentVariable("LANSETSPBOT_DATA_DIR", "Process")
$oldLegacyDataDir = [Environment]::GetEnvironmentVariable("MARLEN_DATA_DIR", "Process")
$RelocationRoot = $null
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    Write-BuildStage "Running packaged self-test"
    & $BuiltExe --self-test
    if ($LASTEXITCODE -ne 0) { throw "Packaged self-test failed." }

    $RelocationRoot = Join-Path $env:TEMP ("LansetSpBot Проверка " + [guid]::NewGuid().ToString("N"))
    $RelocatedDir = Join-Path $RelocationRoot $AppName
    New-Item -ItemType Directory -Path $RelocatedDir -Force | Out-Null
    Copy-Item -Path (Join-Path $BuiltDir "*") -Destination $RelocatedDir -Recurse -Force
    $RelocatedExe = Join-Path $RelocatedDir ($AppName + ".exe")

    Write-BuildStage "Running relocated packaged self-test"
    & $RelocatedExe --self-test
    if ($LASTEXITCODE -ne 0) { throw "Relocated packaged self-test failed." }

    Write-BuildStage "Running packaged profile migration smoke test"
    $MigrationAppData = Join-Path $RelocationRoot "MigrationAppData"
    $LegacyProfile = Join-Path $MigrationAppData "Marlen"
    $CanonicalProfile = Join-Path $MigrationAppData "LansetSpBot"
    $LegacySessions = Join-Path $LegacyProfile "sessions"
    New-Item -ItemType Directory -Path $LegacySessions -Force | Out-Null

    [System.IO.File]::WriteAllBytes(
        (Join-Path $LegacyProfile "marlen.db"),
        [byte[]](0x4c, 0x53, 0x42, 0x2d, 0x44, 0x42)
    )
    [System.IO.File]::WriteAllBytes(
        (Join-Path $LegacySessions "build.session"),
        [byte[]](0x53, 0x45, 0x53, 0x53, 0x49, 0x4f, 0x4e)
    )

    $ExpectedDatabaseHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $LegacyProfile "marlen.db")
    ).Hash
    $ExpectedSessionHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $LegacySessions "build.session")
    ).Hash

    $env:APPDATA = $MigrationAppData
    Remove-Item Env:LANSETSPBOT_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:MARLEN_DATA_DIR -ErrorAction SilentlyContinue

    $MigrationProcess = Start-Process -FilePath $RelocatedExe `
        -ArgumentList "--migrate-profile" `
        -PassThru

    if (-not $MigrationProcess.WaitForExit(30000)) {
        Stop-Process -Id $MigrationProcess.Id -Force -ErrorAction SilentlyContinue
        throw "Packaged profile migration command did not exit within 30 seconds."
    }
    if ($MigrationProcess.ExitCode -ne 0) {
        throw "Packaged profile migration command failed with exit code $($MigrationProcess.ExitCode)."
    }
    if (Test-Path -LiteralPath $LegacyProfile) {
        throw "Packaged profile migration left the legacy profile behind."
    }
    if (-not (Test-Path -LiteralPath $CanonicalProfile -PathType Container)) {
        throw "Packaged profile migration did not create the canonical profile."
    }

    $ActualDatabaseHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $CanonicalProfile "marlen.db")
    ).Hash
    $ActualSessionHash = (
        Get-FileHash -Algorithm SHA256 -LiteralPath (
            Join-Path $CanonicalProfile "sessions\build.session"
        )
    ).Hash

    if ($ActualDatabaseHash -ne $ExpectedDatabaseHash) {
        throw "Packaged profile migration changed the database bytes."
    }
    if ($ActualSessionHash -ne $ExpectedSessionHash) {
        throw "Packaged profile migration changed the session bytes."
    }
}
finally {
    if ($null -eq $oldQt) { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue } else { $env:QT_QPA_PLATFORM = $oldQt }
    if ($null -eq $oldAppData) { Remove-Item Env:APPDATA -ErrorAction SilentlyContinue } else { $env:APPDATA = $oldAppData }
    if ($null -eq $oldCanonicalDataDir) { Remove-Item Env:LANSETSPBOT_DATA_DIR -ErrorAction SilentlyContinue } else { $env:LANSETSPBOT_DATA_DIR = $oldCanonicalDataDir }
    if ($null -eq $oldLegacyDataDir) { Remove-Item Env:MARLEN_DATA_DIR -ErrorAction SilentlyContinue } else { $env:MARLEN_DATA_DIR = $oldLegacyDataDir }
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
Copy-Item -LiteralPath $ReleaseReadme -Destination (Join-Path $ReleaseRoot "WINDOWS_X64_README.txt")
Set-Content -LiteralPath (Join-Path $ReleaseRoot "1_START_LANSETSPBOT.bat") -Encoding Ascii -Value "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0$AppName.exe`"`r`n"

& $BuildPython build\generate_sbom.py --version $AppVersion --requirements requirements-runtime.lock --requirements requirements-openai.lock --name $AppName --output $SbomPath
if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed." }
Copy-Item -LiteralPath $SbomPath -Destination $ReleaseRoot
Write-BuildStage "Creating release archive"
Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { throw "Release ZIP was not created." }
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $ZipPath).Hash.ToLowerInvariant()
Set-Content -LiteralPath $ChecksumsPath -Encoding Ascii -Value "$hash  $([System.IO.Path]::GetFileName($ZipPath))"

Write-Host "[LansetSpBot build] Windows x64 release created:" -ForegroundColor Green
Write-Host "  $ZipPath"
Write-Host "  $ChecksumsPath"
Write-Host "  $SbomPath"
