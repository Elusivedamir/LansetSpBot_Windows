[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$KeepBuildVenv,
    [string]$SigningPfxPath = "",
    [string]$TimestampUrl = "https://timestamp.digicert.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location -LiteralPath $ProjectRoot
$ReleaseReadme = Join-Path $ProjectRoot "README.txt"
$OutputRoot = Join-Path $ProjectRoot "dist"
$StageRoot = Join-Path $OutputRoot "staging"
$ProofRoot = Join-Path $OutputRoot "ci-proof"
$PyInstallerRoot = Join-Path $OutputRoot "pyinstaller"

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

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $ResolvedPath = [System.IO.Path]::GetFullPath($LiteralPath)
    $Stream = [System.IO.File]::Open(
        $ResolvedPath,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    try {
        $Sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            return ([System.BitConverter]::ToString(
                $Sha256.ComputeHash($Stream)
            ) -replace "-", "").ToLowerInvariant()
        }
        finally {
            $Sha256.Dispose()
        }
    }
    finally {
        $Stream.Dispose()
    }
}

function Copy-DirectoryTree {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Directory copy source does not exist: $Source"
    }
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $Robocopy = Get-Command "robocopy.exe" -ErrorAction SilentlyContinue
    if ($null -eq $Robocopy) {
        throw "robocopy.exe was not found on the Windows build host."
    }

    # /E preserves empty Qt/Shiboken runtime directories when the one-dir
    # bundle is relocated and again when the final release tree is staged.
    & $Robocopy.Source $Source $Destination `
        /E /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS /NP
    $RobocopyExit = $LASTEXITCODE
    if ($RobocopyExit -ge 8) {
        throw "robocopy failed with exit code $RobocopyExit: $Source -> $Destination"
    }
}

function Invoke-PackagedProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [int]$TimeoutMilliseconds = 120000
    )

    $Process = Start-Process -FilePath $Executable `
        -ArgumentList $Arguments `
        -PassThru
    try {
        if (-not $Process.WaitForExit($TimeoutMilliseconds)) {
            Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
            throw "$Label did not exit within $TimeoutMilliseconds milliseconds."
        }
        $Process.Refresh()
        if ($Process.ExitCode -ne 0) {
            throw "$Label failed with exit code $($Process.ExitCode)."
        }
    }
    finally {
        $Process.Dispose()
    }
}

function Assert-CleanCheckout {
    param([Parameter(Mandatory = $true)][string]$Stage)
    $SafeStage = ($Stage -replace "[^A-Za-z0-9._-]", "-")
    $Evidence = Join-Path $ProofRoot ("checkout-" + $SafeStage + ".json")
    & $py.Source @PythonArgs tools\release_checkout.py `
        --root $ProjectRoot `
        --stage $Stage `
        --evidence $Evidence
    if ($LASTEXITCODE -ne 0) {
        throw "Release proof modified the checkout at stage: $Stage"
    }
}

Assert-CleanCheckout "before-build"

$BuildVenv = Join-Path $OutputRoot ".venv-build-windows-x64"
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

# Every generated release input lives under the ignored dist/ tree. The source
# checkout remains byte-for-byte identical to the exact commit being proved.
$InstructionAssets = Join-Path $StageRoot "instruction-assets"
$SourceManifest = Join-Path $ProofRoot "source-SHA256SUMS.txt"
$VersionInfo = Join-Path $StageRoot "windows_version_info.txt"
Remove-Item -LiteralPath $StageRoot, $PyInstallerRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $InstructionAssets, $ProofRoot -Force | Out-Null

Write-Host "[LansetSpBot build] Rendering instruction screenshots in staging..." -ForegroundColor Cyan
$OldInstructionQt = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $BuildPython tools\capture_instruction_screenshots.py --destination $InstructionAssets
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

& $BuildPython tools\generate_manifest.py --output $SourceManifest
if ($LASTEXITCODE -ne 0) {
    throw "Staged source manifest generation failed."
}
& $BuildPython build\generate_windows_version_info.py --output $VersionInfo
if ($LASTEXITCODE -ne 0) {
    throw "Staged Windows version resource generation failed."
}
Assert-CleanCheckout "after-generation"

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
        Write-BuildStage "Running core pytest diagnostics in four file shards"
        $CoreTestFiles = @(
            Get-ChildItem -LiteralPath (Join-Path $ProjectRoot "tests") `
                -Recurse -File -Filter "test_*.py" |
                Where-Object { $_.Name -ne "test_gui_v45.py" } |
                Sort-Object FullName |
                ForEach-Object {
                    $FullTestPath = [System.IO.Path]::GetFullPath($_.FullName)
                    $RootPrefix = $ProjectRoot + [System.IO.Path]::DirectorySeparatorChar
                    if (-not $FullTestPath.StartsWith(
                        $RootPrefix,
                        [System.StringComparison]::OrdinalIgnoreCase
                    )) {
                        throw "Test file is outside the project root: $FullTestPath"
                    }
                    $FullTestPath.Substring($RootPrefix.Length)
                }
        )
        $CoreShardCount = 4
        $coreTestsExit = 0
        $coreShardResults = @()
        for ($ShardIndex = 0; $ShardIndex -lt $CoreShardCount; $ShardIndex++) {
            $ShardNumber = $ShardIndex + 1
            $ShardFiles = @()
            for (
                $FileIndex = $ShardIndex;
                $FileIndex -lt $CoreTestFiles.Count;
                $FileIndex += $CoreShardCount
            ) {
                $ShardFiles += $CoreTestFiles[$FileIndex]
            }
            if ($ShardFiles.Count -eq 0) {
                continue
            }
            $env:PYTEST_CURRENT_TEST_FILE = Join-Path $ProofRoot (
                "pytest-core-shard-$ShardNumber-current-test.txt"
            )
            $ShardCommand = @(
                "tools\run_ci_subprocess.py",
                "--label", "core-shard-$ShardNumber",
                "--log", (Join-Path $ProofRoot "pytest-core-shard-$ShardNumber.log"),
                "--idle-timeout-seconds", "300",
                "--total-timeout-seconds", "1500",
                "--",
                $BuildPython,
                "-X", "faulthandler",
                "-m", "coverage", "run", "--parallel-mode",
                "-m", "pytest",
                "-vv", "--tb=long", "--showlocals", "-ra", "--durations=20",
                "-p", "tools.pytest_ci_watchdog",
                "--junitxml", (Join-Path $ProofRoot "pytest-core-shard-$ShardNumber.xml")
            ) + $ShardFiles
            & $BuildPython @ShardCommand
            $ShardExit = $LASTEXITCODE
            $coreShardResults += "core_shard_$ShardNumber=$ShardExit"
            if ($ShardExit -ne 0 -and $coreTestsExit -eq 0) {
                $coreTestsExit = $ShardExit
            }
        }

        Write-BuildStage "Running GUI pytest diagnostics in isolated process"
        $env:PYTEST_CURRENT_TEST_FILE = (Join-Path $ProofRoot "pytest-gui-current-test.txt")
        & $BuildPython tools\run_ci_subprocess.py `
            --label gui `
            --log (Join-Path $ProofRoot "pytest-gui.log") `
            --idle-timeout-seconds 300 `
            --total-timeout-seconds 900 `
            -- `
            $BuildPython -X faulthandler -m coverage run --parallel-mode -m pytest `
            -vv --tb=long --showlocals -ra --durations=20 `
            -p tools.pytest_ci_watchdog `
            --junitxml (Join-Path $ProofRoot "pytest-gui.xml") `
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
        if ($coverageJsonExit -eq 0) {
            Copy-Item -LiteralPath "coverage.json" `
                -Destination (Join-Path $ProofRoot "coverage.json") -Force
        }
    }
    else {
        $coverageJsonExit = 1
    }

    @(
        "core_exit=$coreTestsExit"
        $coreShardResults
        "gui_exit=$guiTestsExit"
        "coverage_combine_exit=$coverageCombineExit"
        "coverage_json_exit=$coverageJsonExit"
    ) | Set-Content -LiteralPath (Join-Path $ProofRoot "pytest-diagnostics-summary.txt") -Encoding UTF8

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
Assert-CleanCheckout "after-tests"

if (-not (Test-Path -LiteralPath "build\assets\LansetSpBot.ico" -PathType Leaf)) {
    throw "Windows icon build/assets/LansetSpBot.ico is missing."
}

$AppName = & $BuildPython -c "from core.version import APP_NAME; print(APP_NAME)"
$AppVersion = & $BuildPython -c "from core.version import __version__; print(__version__)"
$BuiltDir = Join-Path $PyInstallerRoot $AppName
$BuiltExe = Join-Path $BuiltDir ($AppName + ".exe")
$PyInstallerWork = Join-Path $StageRoot "pyinstaller-work"
Remove-Item -LiteralPath $BuiltDir, $PyInstallerWork -Recurse -Force -ErrorAction SilentlyContinue

Write-BuildStage "Building Windows application with PyInstaller"
$OldBuildInstructionAssets = [Environment]::GetEnvironmentVariable("LANSETSPBOT_BUILD_INSTRUCTION_ASSETS", "Process")
$OldBuildVersionInfo = [Environment]::GetEnvironmentVariable("LANSETSPBOT_BUILD_VERSION_INFO", "Process")
try {
    $env:LANSETSPBOT_BUILD_INSTRUCTION_ASSETS = $InstructionAssets
    $env:LANSETSPBOT_BUILD_VERSION_INFO = $VersionInfo
    & $BuildPython -m PyInstaller --clean --noconfirm `
        --distpath $PyInstallerRoot `
        --workpath $PyInstallerWork `
        "build\LansetSpBot.windows.spec"
}
finally {
    if ($null -eq $OldBuildInstructionAssets) { Remove-Item Env:LANSETSPBOT_BUILD_INSTRUCTION_ASSETS -ErrorAction SilentlyContinue } else { $env:LANSETSPBOT_BUILD_INSTRUCTION_ASSETS = $OldBuildInstructionAssets }
    if ($null -eq $OldBuildVersionInfo) { Remove-Item Env:LANSETSPBOT_BUILD_VERSION_INFO -ErrorAction SilentlyContinue } else { $env:LANSETSPBOT_BUILD_VERSION_INFO = $OldBuildVersionInfo }
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $BuiltExe -PathType Leaf)) {
    throw "PyInstaller did not create $BuiltExe"
}

$ReleaseSigned = $false
if ($SigningPfxPath) {
    if ($env:GITHUB_EVENT_NAME -eq "pull_request") {
        throw "Authenticode signing is forbidden in pull-request builds."
    }
    if (-not (Test-Path -LiteralPath $SigningPfxPath -PathType Leaf)) {
        throw "Authenticode PFX file is missing."
    }
    $SigningPassword = [Environment]::GetEnvironmentVariable(
        "LANSETSPBOT_SIGNING_PFX_PASSWORD",
        "Process"
    )
    if (-not $SigningPassword) {
        throw "Authenticode password secret is missing."
    }
    if (-not $TimestampUrl.StartsWith("https://", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Authenticode timestamp URL must use HTTPS."
    }

    $SignTool = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($null -eq $SignTool) {
        $SignTool = Get-ChildItem `
            -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" `
            -File -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
    }
    if ($null -eq $SignTool) {
        throw "signtool.exe was not found on the Windows release runner."
    }

    $ImportedCertificates = @()
    $SigningCertificate = $null
    try {
        $SecurePassword = ConvertTo-SecureString $SigningPassword -AsPlainText -Force
        $ImportedCertificates = @(
            Import-PfxCertificate `
                -FilePath $SigningPfxPath `
                -CertStoreLocation "Cert:\CurrentUser\My" `
                -Password $SecurePassword `
                -Exportable:$false
        )
        $SigningCertificate = $ImportedCertificates |
            Where-Object {
                $_.HasPrivateKey -and
                ($_.EnhancedKeyUsageList.ObjectId -contains "1.3.6.1.5.5.7.3.3")
            } |
            Select-Object -First 1
        if ($null -eq $SigningCertificate -or -not $SigningCertificate.Thumbprint) {
            throw "PFX contains no private code-signing certificate."
        }
        Write-BuildStage "Signing Windows executable with Authenticode"
        & $SignTool.FullName sign `
            /fd SHA256 `
            /td SHA256 `
            /tr $TimestampUrl `
            /s My `
            /sha1 $SigningCertificate.Thumbprint `
            $BuiltExe
        if ($LASTEXITCODE -ne 0) { throw "Authenticode signing failed." }
        & $SignTool.FullName verify /pa /all /v $BuiltExe
        if ($LASTEXITCODE -ne 0) { throw "Authenticode verification failed." }
        $ReleaseSigned = $true
    }
    finally {
        foreach ($Certificate in $ImportedCertificates) {
            if ($Certificate.Thumbprint) {
                Remove-Item `
                    -LiteralPath ("Cert:\CurrentUser\My\" + $Certificate.Thumbprint) `
                    -Force -ErrorAction SilentlyContinue
            }
        }
        $SigningPassword = $null
        $SecurePassword = $null
    }
}

$oldQt = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
$oldAppData = [Environment]::GetEnvironmentVariable("APPDATA", "Process")
$oldCanonicalDataDir = [Environment]::GetEnvironmentVariable("LANSETSPBOT_DATA_DIR", "Process")
$oldLegacyDataDir = [Environment]::GetEnvironmentVariable("MARLEN_DATA_DIR", "Process")
$RelocationRoot = $null
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    Write-BuildStage "Running packaged self-test"
    Invoke-PackagedProcess `
        -Executable $BuiltExe `
        -Arguments @("--self-test") `
        -Label "Packaged self-test"

    $RelocationRoot = Join-Path $env:TEMP ("LansetSpBot Проверка " + [guid]::NewGuid().ToString("N"))
    $RelocatedDir = Join-Path $RelocationRoot $AppName
    Copy-DirectoryTree -Source $BuiltDir -Destination $RelocatedDir
    $RelocatedExe = Join-Path $RelocatedDir ($AppName + ".exe")

    Write-BuildStage "Running relocated packaged self-test"
    Invoke-PackagedProcess `
        -Executable $RelocatedExe `
        -Arguments @("--self-test") `
        -Label "Relocated packaged self-test"

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

    $ExpectedDatabaseHash = Get-Sha256Hex -LiteralPath (Join-Path $LegacyProfile "marlen.db")
    $ExpectedSessionHash = Get-Sha256Hex -LiteralPath (Join-Path $LegacySessions "build.session")

    $env:APPDATA = $MigrationAppData
    Remove-Item Env:LANSETSPBOT_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:MARLEN_DATA_DIR -ErrorAction SilentlyContinue

    Invoke-PackagedProcess `
        -Executable $RelocatedExe `
        -Arguments @("--migrate-profile") `
        -Label "Packaged profile migration command" `
        -TimeoutMilliseconds 30000
    if (Test-Path -LiteralPath $LegacyProfile) {
        throw "Packaged profile migration left the legacy profile behind."
    }
    if (-not (Test-Path -LiteralPath $CanonicalProfile -PathType Container)) {
        throw "Packaged profile migration did not create the canonical profile."
    }

    $ActualDatabaseHash = Get-Sha256Hex -LiteralPath (Join-Path $CanonicalProfile "marlen.db")
    $ActualSessionHash = Get-Sha256Hex -LiteralPath (
        Join-Path $CanonicalProfile "sessions\build.session"
    )

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
Copy-DirectoryTree -Source $BuiltDir -Destination $ReleaseRoot
Copy-Item -LiteralPath $ReleaseReadme -Destination (Join-Path $ReleaseRoot "WINDOWS_X64_README.txt")
Set-Content -LiteralPath (Join-Path $ReleaseRoot "1_START_LANSETSPBOT.bat") -Encoding Ascii -Value "@echo off`r`ncd /d `"%~dp0`"`r`nstart `"`" `"%~dp0$AppName.exe`"`r`n"

$MigrationLauncher = @(
    "@echo off"
    "setlocal"
    "cd /d `"%~dp0`""
    "echo LansetSpBot legacy profile migration"
    "echo."
    "if not exist `"%~dp0$AppName.exe`" ("
    "  echo ERROR: $AppName.exe was not found next to this launcher."
    "  exit /b 2"
    ")"
    "tasklist /FI `"IMAGENAME eq $AppName.exe`" /NH 2^>nul | find /I `"$AppName.exe`" ^>nul"
    "if not errorlevel 1 ("
    "  echo ERROR: $AppName.exe is running. Close the application and retry."
    "  exit /b 2"
    ")"
    "echo Migrating %%APPDATA%%\Marlen to %%APPDATA%%\LansetSpBot..."
    "`"%~dp0$AppName.exe`" --migrate-profile"
    "set `"EXIT_CODE=%%ERRORLEVEL%%`""
    "echo."
    "if `"%%EXIT_CODE%%`"==`"0`" echo SUCCESS: migration completed or was already complete."
    "if `"%%EXIT_CODE%%`"==`"2`" echo NOT CHANGED: migration was refused because the state is unsupported or unsafe."
    "if `"%%EXIT_CODE%%`"==`"3`" echo FAILED: migration started but could not complete safely."
    "if not `"%%EXIT_CODE%%`"==`"0`" if not `"%%EXIT_CODE%%`"==`"2`" if not `"%%EXIT_CODE%%`"==`"3`" echo FAILED: unexpected exit code %%EXIT_CODE%%."
    "echo."
    "pause"
    "exit /b %%EXIT_CODE%%"
) -join "`r`n"
Set-Content -LiteralPath (Join-Path $ReleaseRoot "2_MIGRATE_OLD_PROFILE.bat") -Encoding Ascii -Value ($MigrationLauncher + "`r`n")

& $BuildPython build\generate_sbom.py --version $AppVersion --requirements requirements-runtime.lock --requirements requirements-openai.lock --name $AppName --output $SbomPath
if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed." }
Copy-Item -LiteralPath $SbomPath -Destination $ReleaseRoot
Write-BuildStage "Creating release archive"
Compress-Archive -LiteralPath $ReleaseRoot -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path -LiteralPath $ZipPath -PathType Leaf)) { throw "Release ZIP was not created." }
$hash = (Get-Sha256Hex -LiteralPath $ZipPath)
Set-Content -LiteralPath $ChecksumsPath -Encoding Ascii -Value "$hash  $([System.IO.Path]::GetFileName($ZipPath))"

Assert-CleanCheckout "after-packaging"
$Commit = (& git rev-parse HEAD).Trim()
$Ref = if ($env:GITHUB_REF) { $env:GITHUB_REF } else { (& git symbolic-ref --quiet --short HEAD 2>$null).Trim() }
$Repository = if ($env:GITHUB_REPOSITORY) { $env:GITHUB_REPOSITORY } else { (& git remote get-url origin 2>$null).Trim() }
$InstructionMetadataPath = Join-Path $InstructionAssets "capture_metadata.json"
$InstructionMetadata = Get-Content -LiteralPath $InstructionMetadataPath -Raw | ConvertFrom-Json
$AssetHashes = [ordered]@{}
Get-ChildItem -LiteralPath $InstructionAssets -File | Sort-Object Name | ForEach-Object {
    $AssetHashes[$_.Name] = (Get-Sha256Hex -LiteralPath $_.FullName)
}
$Proof = [ordered]@{
    format = 1
    repository = $Repository
    commit = $Commit
    ref = $Ref
    runner = [ordered]@{
        os = [Environment]::OSVersion.VersionString
        architecture = if ([Environment]::Is64BitOperatingSystem) { "x64" } else { "x86" }
        python = (& $BuildPython -c "import sys; print(sys.version.replace(chr(10), ' '))").Trim()
        qt = (& $BuildPython -c "from PySide6.QtCore import qVersion; print(qVersion())").Trim()
        pyinstaller = (& $BuildPython -c "import PyInstaller; print(PyInstaller.__version__)").Trim()
    }
    source_manifest = [ordered]@{
        path = $SourceManifest
        sha256 = (Get-Sha256Hex -LiteralPath $SourceManifest)
    }
    instruction_assets = [ordered]@{
        directory = $InstructionAssets
        source_fingerprint = $InstructionMetadata.source_fingerprint
        hashes = $AssetHashes
    }
    artifacts = [ordered]@{
        exe_sha256 = (Get-Sha256Hex -LiteralPath $BuiltExe)
        zip = [System.IO.Path]::GetFileName($ZipPath)
        zip_sha256 = $hash
        sbom = [System.IO.Path]::GetFileName($SbomPath)
        sbom_sha256 = (Get-Sha256Hex -LiteralPath $SbomPath)
        checksums = [System.IO.Path]::GetFileName($ChecksumsPath)
    }
    gates = [ordered]@{
        tests_skipped = [bool]$SkipTests
        source_self_test = -not [bool]$SkipTests
        packaged_self_test = $true
        relocated_self_test = $true
        migration_smoke_test = $true
        authenticode_signed = $ReleaseSigned
        clean_tree = $true
    }
    output_root = $OutputRoot
}
$ProofPath = Join-Path $ProofRoot "release-proof.json"
$Proof | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ProofPath -Encoding UTF8
Assert-CleanCheckout "final"

Write-Host "[LansetSpBot build] Windows x64 release created:" -ForegroundColor Green
Write-Host "  $ZipPath"
Write-Host "  $ChecksumsPath"
Write-Host "  $SbomPath"
