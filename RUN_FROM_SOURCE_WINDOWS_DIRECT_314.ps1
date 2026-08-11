[CmdletBinding()]
param(
    [switch]$RecreateVenv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$VenvPath = Join-Path $ProjectRoot ".venv-windows-x64"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$VenvPythonw = Join-Path $VenvPath "Scripts\pythonw.exe"
$MarkerPath = Join-Path $VenvPath ".marlen-runtime-lock.sha256"
$RuntimeLock = Join-Path $ProjectRoot "requirements-runtime.lock"
$BootstrapLock = Join-Path $ProjectRoot "requirements-bootstrap.txt"
$OpenAILock = Join-Path $ProjectRoot "requirements-openai.lock"
$MainScript = Join-Path $ProjectRoot "main.py"
$PyLauncher = if ($env:WINDIR) { Join-Path $env:WINDIR "py.exe" } else { "py.exe" }

function Write-Step([string]$Message) {
    Write-Host "[LansetSpBot] $Message" -ForegroundColor Cyan
}

function Assert-LastExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Message Error code: $LASTEXITCODE"
    }
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "LansetSpBot requires 64-bit Windows 10 or Windows 11."
}
foreach ($required in @($RuntimeLock, $BootstrapLock, $OpenAILock, $MainScript)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Missing required project file: $required"
    }
}
if (-not (Test-Path -LiteralPath $PyLauncher -PathType Leaf)) {
    $command = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        throw "Python Launcher py.exe was not found."
    }
    $PyLauncher = [string]$command.Source
}


$fastHashes = @(
    (Get-FileHash -Algorithm SHA256 -LiteralPath $RuntimeLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $BootstrapLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $OpenAILock).Hash
)
if ((Test-Path -LiteralPath $VenvPythonw -PathType Leaf) -and
    (Test-Path -LiteralPath $MarkerPath -PathType Leaf)) {
    $markerLines = @(Get-Content -LiteralPath $MarkerPath)
    $fastMarkerValid = (
        $markerLines.Count -ge 4 -and
        $markerLines[0].Trim() -eq $fastHashes[0].Trim() -and
        $markerLines[1].Trim() -eq $fastHashes[1].Trim() -and
        $markerLines[2].Trim() -eq $fastHashes[2].Trim() -and
        $markerLines[3].Trim() -eq 'windows-direct-python-314-v1'
    )
    if ($fastMarkerValid) {
        Write-Step "Starting LansetSpBot"
        $quotedMain = '"' + $MainScript + '"'
        Start-Process -FilePath $VenvPythonw -ArgumentList $quotedMain -WorkingDirectory $ProjectRoot
        exit 0
    }
}

Write-Step "Using the installed Python Launcher directly: py -3.14"
& $PyLauncher -3.14 --version
Assert-LastExitCode "Python 3.14 could not be started."

$expectedMarker = @(
    (Get-FileHash -Algorithm SHA256 -LiteralPath $RuntimeLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $BootstrapLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $OpenAILock).Hash,
    "windows-direct-python-314-v1"
) -join "`n"

$markerMatches = $false
if (Test-Path -LiteralPath $MarkerPath -PathType Leaf) {
    $markerMatches = ((Get-Content -LiteralPath $MarkerPath -Raw).Trim() -eq $expectedMarker.Trim())
}
$venvUsable = (Test-Path -LiteralPath $VenvPython -PathType Leaf) -and (Test-Path -LiteralPath $VenvPythonw -PathType Leaf)

if ($RecreateVenv -or -not $venvUsable -or -not $markerMatches) {
    if (Test-Path -LiteralPath $VenvPath) {
        $resolvedVenv = [System.IO.Path]::GetFullPath($VenvPath)
        if (-not $resolvedVenv.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
            [System.IO.Path]::GetFileName($resolvedVenv) -ne ".venv-windows-x64") {
            throw "Unsafe virtual-environment path refused: $resolvedVenv"
        }
        Write-Step "Removing the incomplete local environment"
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }

    Write-Step "Creating the local Windows x64 environment with Python 3.14"
    & $PyLauncher -3.14 -m venv $VenvPath
    Assert-LastExitCode "Python could not create the virtual environment."
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Virtual environment was created without Scripts\python.exe."
    }

    Write-Step "Installing verified bootstrap dependencies"
    & $VenvPython -m ensurepip --upgrade
    Assert-LastExitCode "ensurepip failed."
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r $BootstrapLock
    Assert-LastExitCode "Bootstrap dependency installation failed."

    Write-Step "Installing verified LansetSpBot runtime dependencies"
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r $RuntimeLock
    Assert-LastExitCode "Runtime dependency installation failed."
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r $OpenAILock
    Assert-LastExitCode "OpenAI SDK installation failed."
    & $VenvPython (Join-Path $ProjectRoot "tools\generate_openai_lock.py") --output $OpenAILock --check
    Assert-LastExitCode "OpenAI lock verification failed."

    Set-Content -LiteralPath $MarkerPath -Value $expectedMarker -Encoding Ascii -NoNewline
}

Write-Step "Running the local startup self-test"
$oldQtPlatform = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $VenvPython $MainScript --self-test
    Assert-LastExitCode "LansetSpBot self-test failed."
}
finally {
    if ($null -eq $oldQtPlatform) {
        Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
    }
    else {
        $env:QT_QPA_PLATFORM = $oldQtPlatform
    }
}

Write-Step "Starting LansetSpBot"
$quotedMain = '"' + $MainScript + '"'
Start-Process -FilePath $VenvPythonw -ArgumentList $quotedMain -WorkingDirectory $ProjectRoot
exit 0
