[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("3.13", "3.14")]
    [string]$PythonVersion,

    [string]$EvidenceRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location -LiteralPath $ProjectRoot

$Python = (Get-Command "python.exe" -ErrorAction Stop).Source
$VersionToken = $PythonVersion.Replace(".", "")
if (-not $EvidenceRoot) {
    $EvidenceRoot = Join-Path $ProjectRoot ("dist\windows-ci\python-" + $VersionToken)
}
else {
    $EvidenceRoot = [System.IO.Path]::GetFullPath($EvidenceRoot)
}
New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null

$Results = [ordered]@{}

function Invoke-PythonGate {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$LogName
    )

    Write-Host "::group::$Name"
    $LogPath = Join-Path $EvidenceRoot $LogName
    & $Python @Arguments 2>&1 | Tee-Object -FilePath $LogPath
    $ExitCode = $LASTEXITCODE
    $script:Results[$Name] = [int]$ExitCode
    Write-Host "[$Name] exit=$ExitCode"
    Write-Host "::endgroup::"
    return [int]$ExitCode
}

function Invoke-GitGate {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$LogName
    )

    Write-Host "::group::$Name"
    $LogPath = Join-Path $EvidenceRoot $LogName
    & git @Arguments 2>&1 | Tee-Object -FilePath $LogPath
    $ExitCode = $LASTEXITCODE
    $script:Results[$Name] = [int]$ExitCode
    Write-Host "[$Name] exit=$ExitCode"
    Write-Host "::endgroup::"
    return [int]$ExitCode
}

$VersionProof = @(
    "import platform,struct,sys"
    "expected=tuple(map(int,'$PythonVersion'.split('.')))"
    "assert sys.version_info[:2] == expected, (sys.version, expected)"
    "assert struct.calcsize('P') * 8 == 64"
    "print(sys.version)"
    "print(sys.executable)"
    "print(platform.platform())"
) -join ";"

Invoke-PythonGate `
    -Name "python-version" `
    -Arguments @("-c", $VersionProof) `
    -LogName "python-version.txt" | Out-Null

Invoke-GitGate `
    -Name "git-head" `
    -Arguments @("rev-parse", "HEAD") `
    -LogName "git-head.txt" | Out-Null

Invoke-PythonGate `
    -Name "checkout-before" `
    -Arguments @(
        "tools\release_checkout.py",
        "--root", ".",
        "--stage", "windows-source-$VersionToken-before",
        "--evidence", (Join-Path $EvidenceRoot "checkout-before.json")
    ) `
    -LogName "checkout-before.txt" | Out-Null

Invoke-PythonGate `
    -Name "manifest" `
    -Arguments @("tools\generate_manifest.py", "--check") `
    -LogName "manifest.txt" | Out-Null

Invoke-PythonGate `
    -Name "openai-lock" `
    -Arguments @(
        "tools\generate_openai_lock.py",
        "--output", "requirements-openai.lock",
        "--check"
    ) `
    -LogName "openai-lock.txt" | Out-Null

Invoke-PythonGate `
    -Name "runtime-lock-coverage" `
    -Arguments @("tools\check_lock_coverage.py") `
    -LogName "runtime-lock-coverage.txt" | Out-Null

Invoke-PythonGate `
    -Name "compileall" `
    -Arguments @(
        "-m", "compileall", "-q",
        "core", "services", "storage", "workers", "gui",
        "main.py", "tests", "tools", "build"
    ) `
    -LogName "compileall.txt" | Out-Null

Invoke-PythonGate `
    -Name "ruff" `
    -Arguments @(
        "-m", "ruff", "check",
        "core", "services", "storage", "workers", "gui",
        "main.py", "tests", "tools", "build"
    ) `
    -LogName "ruff.txt" | Out-Null

Invoke-PythonGate `
    -Name "mypy" `
    -Arguments @(
        "-m", "mypy",
        "--python-version", $PythonVersion,
        "--config-file", "mypy.ini",
        "core", "services", "storage", "workers", "gui", "main.py"
    ) `
    -LogName "mypy.txt" | Out-Null

Invoke-PythonGate `
    -Name "coverage-erase" `
    -Arguments @("-m", "coverage", "erase") `
    -LogName "coverage-erase.txt" | Out-Null

$PreviousCurrentTestFile = [Environment]::GetEnvironmentVariable(
    "PYTEST_CURRENT_TEST_FILE",
    "Process"
)
try {
    $env:PYTEST_CURRENT_TEST_FILE = Join-Path $EvidenceRoot "pytest-core-current-test.txt"
    Invoke-PythonGate `
        -Name "pytest-core" `
        -Arguments @(
            "tools\run_ci_subprocess.py",
            "--label", "core-$VersionToken",
            "--log", (Join-Path $EvidenceRoot "pytest-core.log"),
            "--idle-timeout-seconds", "660",
            "--total-timeout-seconds", "3600",
            "--",
            $Python,
            "-X", "faulthandler",
            "-m", "coverage", "run", "--parallel-mode",
            "-m", "pytest",
            "-vv", "--tb=long", "--showlocals", "-ra", "--durations=30",
            "-p", "tools.pytest_ci_watchdog",
            "--junitxml", (Join-Path $EvidenceRoot "pytest-core.xml"),
            "--ignore", "tests/test_gui_v45.py",
            "tests"
        ) `
        -LogName "pytest-core-parent.txt" | Out-Null

    $env:PYTEST_CURRENT_TEST_FILE = Join-Path $EvidenceRoot "pytest-gui-current-test.txt"
    Invoke-PythonGate `
        -Name "pytest-gui" `
        -Arguments @(
            "tools\run_ci_subprocess.py",
            "--label", "gui-$VersionToken",
            "--log", (Join-Path $EvidenceRoot "pytest-gui.log"),
            "--idle-timeout-seconds", "240",
            "--total-timeout-seconds", "900",
            "--",
            $Python,
            "-X", "faulthandler",
            "-m", "coverage", "run", "--parallel-mode",
            "-m", "pytest",
            "-vv", "--tb=long", "--showlocals", "-ra", "--durations=30",
            "-p", "tools.pytest_ci_watchdog",
            "--junitxml", (Join-Path $EvidenceRoot "pytest-gui.xml"),
            "tests/test_gui_v45.py"
        ) `
        -LogName "pytest-gui-parent.txt" | Out-Null
}
finally {
    if ($null -eq $PreviousCurrentTestFile) {
        Remove-Item Env:PYTEST_CURRENT_TEST_FILE -ErrorAction SilentlyContinue
    }
    else {
        $env:PYTEST_CURRENT_TEST_FILE = $PreviousCurrentTestFile
    }
}

Invoke-PythonGate `
    -Name "coverage-combine" `
    -Arguments @("-m", "coverage", "combine") `
    -LogName "coverage-combine.txt" | Out-Null

Invoke-PythonGate `
    -Name "coverage-report" `
    -Arguments @("-m", "coverage", "report", "--fail-under=60") `
    -LogName "coverage-report.txt" | Out-Null

Invoke-PythonGate `
    -Name "coverage-json" `
    -Arguments @(
        "-m", "coverage", "json",
        "-o", (Join-Path $EvidenceRoot "coverage.json"),
        "-q"
    ) `
    -LogName "coverage-json.txt" | Out-Null

Invoke-PythonGate `
    -Name "critical-coverage" `
    -Arguments @("tools\check_critical_coverage.py") `
    -LogName "critical-coverage.txt" | Out-Null

Invoke-PythonGate `
    -Name "source-self-test" `
    -Arguments @("main.py", "--self-test") `
    -LogName "source-self-test.txt" | Out-Null

Invoke-PythonGate `
    -Name "checkout-final" `
    -Arguments @(
        "tools\release_checkout.py",
        "--root", ".",
        "--stage", "windows-source-$VersionToken-final",
        "--evidence", (Join-Path $EvidenceRoot "checkout-final.json")
    ) `
    -LogName "checkout-final.txt" | Out-Null

$Failed = @(
    $Results.GetEnumerator() |
        Where-Object { [int]$_.Value -ne 0 } |
        ForEach-Object { [string]$_.Key }
)

$Summary = [ordered]@{
    format = 1
    python_version = $PythonVersion
    python_executable = $Python
    repository = [Environment]::GetEnvironmentVariable("GITHUB_REPOSITORY", "Process")
    commit = [Environment]::GetEnvironmentVariable("GITHUB_SHA", "Process")
    runner_os = [Environment]::GetEnvironmentVariable("RUNNER_OS", "Process")
    runner_arch = [Environment]::GetEnvironmentVariable("RUNNER_ARCH", "Process")
    results = $Results
    failed = $Failed
    passed = ($Failed.Count -eq 0)
}
$Summary | ConvertTo-Json -Depth 6 |
    Set-Content -LiteralPath (Join-Path $EvidenceRoot "summary.json") -Encoding UTF8

@(
    "python_version=$PythonVersion"
    "python_executable=$Python"
    "passed=$($Failed.Count -eq 0)"
    "failed=$($Failed -join ',')"
    ($Results.GetEnumerator() | ForEach-Object { "gate.$($_.Key)=$($_.Value)" })
) | Set-Content -LiteralPath (Join-Path $EvidenceRoot "summary.txt") -Encoding UTF8

if ($Failed.Count -gt 0) {
    Write-Host "Failed gates: $($Failed -join ', ')" -ForegroundColor Red
    exit 1
}

Write-Host "All Windows source gates passed for Python $PythonVersion." -ForegroundColor Green
exit 0
