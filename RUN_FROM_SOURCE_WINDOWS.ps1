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
$OpenAIRequirements = Join-Path $ProjectRoot "requirements-openai.txt"
$MainScript = Join-Path $ProjectRoot "main.py"
$script:PythonProbeErrors = New-Object System.Collections.Generic.List[string]

function Write-Step([string]$Message) {
    Write-Host "[LansetSpBot] $Message" -ForegroundColor Cyan
}

function Add-ProbeError([string]$Message) {
    if (-not [string]::IsNullOrWhiteSpace($Message)) {
        $script:PythonProbeErrors.Add($Message)
    }
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$File,
        [string[]]$PrefixArgs = @(),
        [string]$Label = "Python candidate"
    )

    try {
        # Windows PowerShell 5.1 can strip embedded double quotes while
        # forwarding native-process arguments. Keep this Python snippet free of
        # double quotes so py.exe/python.exe receives it byte-for-byte.
        $probe = "import struct,sys;print(sys.version_info.major,sys.version_info.minor,8*struct.calcsize('P'),sys.executable,sep='|')"
        $arguments = @($PrefixArgs) + @("-c", $probe)
        $raw = @(& $File @arguments 2>&1)
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0 -or $raw.Count -eq 0) {
            Add-ProbeError "$Label failed (exit $exitCode): $($raw -join ' ')"
            return $null
        }

        $probeLine = [string]($raw | Select-Object -Last 1)
        $parts = @($probeLine.Trim() -split '\|', 4)
        if ($parts.Count -ne 4) {
            Add-ProbeError "$Label returned an unexpected response: $($raw -join ' ')"
            return $null
        }

        [int]$major = $parts[0]
        [int]$minor = $parts[1]
        [int]$bits = $parts[2]
        if ($major -ne 3 -or $minor -notin @(13, 14) -or $bits -ne 64) {
            Add-ProbeError "$Label is unsupported: Python $major.$minor, $bits-bit; LansetSpBot requires Python 3.13 or 3.14 x64"
            return $null
        }

        return [pscustomobject]@{
            File = $File
            PrefixArgs = [string[]]@($PrefixArgs)
            Version = "3.$minor"
            Executable = [string]$parts[3]
            Label = $Label
        }
    }
    catch {
        Add-ProbeError "$Label probe error: $($_.Exception.Message)"
        return $null
    }
}

function Add-PathCandidate {
    param(
        [Parameter(Mandatory = $true)]$List,
        [string]$Path,
        [string]$Label
    )
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $List.Add([pscustomobject]@{ File = $Path; Args = [string[]]@(); Label = $Label })
    }
}

function Find-LansetSpBotPython {
    $candidates = New-Object System.Collections.Generic.List[object]

    # Prefer the Python Launcher because it already knows the installed runtime.
    # Use version tags without an architecture suffix first: this works with both
    # the legacy py.exe and the newer Python install manager launcher.
    $pyPaths = New-Object System.Collections.Generic.List[string]
    $pyCommand = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $pyCommand -and -not [string]::IsNullOrWhiteSpace([string]$pyCommand.Source)) {
        $pyPaths.Add([string]$pyCommand.Source)
    }
    if ($env:WINDIR) {
        $windowsPy = Join-Path $env:WINDIR "py.exe"
        if ((Test-Path -LiteralPath $windowsPy -PathType Leaf) -and -not $pyPaths.Contains($windowsPy)) {
            $pyPaths.Add($windowsPy)
        }
    }

    foreach ($pyPath in $pyPaths) {
        foreach ($tag in @("-3.13", "-3.13-64", "-3.14", "-3.14-64")) {
            $direct = Test-PythonCandidate -File $pyPath -PrefixArgs @($tag) -Label "Python Launcher $tag"
            if ($null -ne $direct) {
                return $direct
            }
        }

        # Also collect exact executable paths reported by the launcher.
        try {
            $inventory = @(& $pyPath -0p 2>&1)
            if ($LASTEXITCODE -eq 0) {
                foreach ($line in $inventory) {
                    $text = ([string]$line).Trim()
                    if ($text -match '^(?:Installed Pythons found by .+)?$') { continue }
                    if ($text -match '^-(?:V:)?[^\s]+\s+\*?\s*(?<path>[A-Za-z]:\\.+?python(?:w)?\.exe)\s*$') {
                        Add-PathCandidate -List $candidates -Path $Matches['path'] -Label "Python Launcher inventory"
                    }
                }
            }
        }
        catch {
            Add-ProbeError "Python Launcher inventory error: $($_.Exception.Message)"
        }
    }

    # Official python.org registry keys.
    foreach ($scope in @("HKCU:", "HKLM:")) {
        foreach ($view in @("Software\Python\PythonCore", "Software\WOW6432Node\Python\PythonCore")) {
            foreach ($version in @("3.13", "3.14")) {
                $installKey = Join-Path $scope "$view\$version\InstallPath"
                try {
                    if (Test-Path -LiteralPath $installKey) {
                        $key = Get-Item -LiteralPath $installKey
                        $exeProperty = $key.GetValue("ExecutablePath", $null)
                        if ($exeProperty) {
                            Add-PathCandidate -List $candidates -Path ([string]$exeProperty) -Label "Registry Python $version"
                        }
                        $installDir = $key.GetValue("", $null)
                        if ($installDir) {
                            Add-PathCandidate -List $candidates -Path (Join-Path ([string]$installDir) "python.exe") -Label "Registry Python $version"
                        }
                    }
                }
                catch {
                    Add-ProbeError "Registry probe $installKey failed: $($_.Exception.Message)"
                }
            }
        }
    }

    # Conventional and Python install-manager paths.
    foreach ($minor in @("313", "314")) {
        if ($env:LOCALAPPDATA) {
            Add-PathCandidate -List $candidates -Path (Join-Path $env:LOCALAPPDATA "Programs\Python\Python$minor\python.exe") -Label "LocalAppData Python$minor"
        }
        if ($env:ProgramFiles) {
            Add-PathCandidate -List $candidates -Path (Join-Path $env:ProgramFiles "Python$minor\python.exe") -Label "Program Files Python$minor"
        }
    }
    if ($env:LOCALAPPDATA) {
        foreach ($managed in @(
            "Python\pythoncore-3.13-64\python.exe",
            "Python\pythoncore-3.14-64\python.exe",
            "Microsoft\WindowsApps\python3.13.exe",
            "Microsoft\WindowsApps\python3.14.exe"
        )) {
            Add-PathCandidate -List $candidates -Path (Join-Path $env:LOCALAPPDATA $managed) -Label "Managed Python"
        }
    }

    foreach ($commandName in @("python.exe", "python3.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($null -ne $command) {
            Add-PathCandidate -List $candidates -Path ([string]$command.Source) -Label "$commandName from PATH"
        }
    }

    $seen = @{}
    foreach ($candidate in $candidates) {
        $file = [string]$candidate.File
        if ([string]::IsNullOrWhiteSpace($file) -or $seen.ContainsKey($file)) {
            continue
        }
        $seen[$file] = $true
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            continue
        }
        $result = Test-PythonCandidate -File $file -PrefixArgs ([string[]]$candidate.Args) -Label ([string]$candidate.Label)
        if ($null -ne $result) {
            return $result
        }
    }
    return $null
}

if (-not [Environment]::Is64BitOperatingSystem) {
    throw "LansetSpBot requires 64-bit Windows 10 or Windows 11."
}
if (-not (Test-Path -LiteralPath $RuntimeLock -PathType Leaf)) {
    throw "Missing requirements-runtime.lock. Extract the complete archive first."
}
if (-not (Test-Path -LiteralPath $BootstrapLock -PathType Leaf)) {
    throw "Missing requirements-bootstrap.txt. Extract the complete archive first."
}
if (-not (Test-Path -LiteralPath $MainScript -PathType Leaf)) {
    throw "Missing main.py. Extract the complete archive first."
}

$Python = Find-LansetSpBotPython
if ($null -eq $Python) {
    $details = if ($script:PythonProbeErrors.Count -gt 0) {
        ($script:PythonProbeErrors | Select-Object -First 12) -join "`n - "
    }
    else {
        "No Python candidates were returned by the launcher, registry, or PATH."
    }
    throw @"
Python 3.13 or Python 3.14 x64 could not be started.
Detection details:
 - $details

Run this check in PowerShell:
  py -3.13 -c "import struct,sys;print(sys.executable,8*struct.calcsize('P'))"
  py -3.14 -c "import struct,sys;print(sys.executable,8*struct.calcsize('P'))"
"@
}
Write-Step "Using Python $($Python.Version) x64 via $($Python.Label): $($Python.Executable)"

$expectedMarker = @(
    (Get-FileHash -Algorithm SHA256 -LiteralPath $RuntimeLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $BootstrapLock).Hash,
    (Get-FileHash -Algorithm SHA256 -LiteralPath $OpenAIRequirements).Hash,
    "windows-source-launcher-v5-python-$($Python.Version)"
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
        Write-Step "Recreating the local Windows environment"
        Remove-Item -LiteralPath $resolvedVenv -Recurse -Force
    }

    Write-Step "Creating the local Windows x64 environment"
    $venvArguments = @($Python.PrefixArgs) + @("-m", "venv", $VenvPath)
    & $Python.File @venvArguments
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Python could not create the virtual environment."
    }

    Write-Step "Installing verified runtime dependencies"
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { throw "ensurepip failed." }
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --retries 8 --timeout 60 -r $BootstrapLock
    if ($LASTEXITCODE -ne 0) { throw "Bootstrap dependency installation failed." }
    & $VenvPython -m pip install --disable-pip-version-check --require-hashes --no-build-isolation --retries 8 --timeout 60 -r $RuntimeLock
    if ($LASTEXITCODE -ne 0) { throw "Runtime dependency installation failed." }
    & $VenvPython -m pip install --disable-pip-version-check --retries 8 --timeout 60 -r $OpenAIRequirements
    if ($LASTEXITCODE -ne 0) { throw "OpenAI SDK installation failed." }

    Set-Content -LiteralPath $MarkerPath -Value $expectedMarker -Encoding Ascii -NoNewline
}

Write-Step "Running the local startup self-test"
$oldQtPlatform = [Environment]::GetEnvironmentVariable("QT_QPA_PLATFORM", "Process")
try {
    $env:QT_QPA_PLATFORM = "offscreen"
    & $VenvPython $MainScript --self-test
    if ($LASTEXITCODE -ne 0) {
        throw "LansetSpBot self-test failed. See the error above."
    }
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
