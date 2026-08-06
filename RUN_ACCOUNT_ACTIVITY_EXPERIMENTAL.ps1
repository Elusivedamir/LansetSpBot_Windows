param(
    [switch]$Execute,
    [string]$Config = "account_activity.json"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location -LiteralPath $PSScriptRoot

$configPath = Join-Path $PSScriptRoot $Config
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    $examplePath = Join-Path $PSScriptRoot "account_activity.example.json"
    if (Test-Path -LiteralPath $examplePath -PathType Leaf) {
        Copy-Item -LiteralPath $examplePath -Destination $configPath -Force
        Write-Host "Created $configPath from the example." -ForegroundColor Yellow
        Write-Host "Edit account_id and explicit targets, then run this script again." -ForegroundColor Yellow
        exit 2
    }
    throw "Configuration file not found: $configPath"
}

$runnerArgs = @("-m", "tools.account_activity_runner", "--config", $configPath)
if ($Execute) {
    $runnerArgs += "--execute"
} else {
    Write-Host "Local validation only. Telegram will not be connected." -ForegroundColor Cyan
}

$probeCode = @'
import PySide6
import telethon
from storage.sqlcipher_driver import dbapi
import tools.account_activity_runner
'@

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$Prefix = @()
    )
    try {
        & $Command @Prefix -c $probeCode 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$pythonCommand = $null
$pythonPrefix = @()
$candidates = @(
    (Join-Path $PSScriptRoot ".venv-windows-x64\Scripts\python.exe"),
    (Join-Path $PSScriptRoot ".venv\Scripts\python.exe"),
    (Join-Path $PSScriptRoot ".venv314\Scripts\python.exe"),
    (Join-Path $PSScriptRoot "venv\Scripts\python.exe")
)
foreach ($candidate in $candidates) {
    if (
        (Test-Path -LiteralPath $candidate -PathType Leaf) -and
        (Test-PythonCandidate -Command $candidate)
    ) {
        $pythonCommand = $candidate
        break
    }
}

if (-not $pythonCommand) {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        foreach ($version in @("-3.14", "-3.13")) {
            if (Test-PythonCandidate -Command $py.Source -Prefix @($version)) {
                $pythonCommand = $py.Source
                $pythonPrefix = @($version)
                break
            }
        }
    }
}

if (-not $pythonCommand) {
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($systemPython -and (Test-PythonCandidate -Command $systemPython.Source)) {
        $pythonCommand = $systemPython.Source
    }
}

if (-not $pythonCommand) {
    throw (
        "Compatible Python environment not found. Run the normal Windows source " +
        "installer first; PySide6, Telethon and SQLCipher must be available."
    )
}

# The non-mutating import probe above may try several interpreters. The actual
# runner is invoked exactly once. Never retry a failed mutating session under
# another Python version: Telegram may already have accepted an action.
& $pythonCommand @pythonPrefix @runnerArgs
exit $LASTEXITCODE
