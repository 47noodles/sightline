<#
.SYNOPSIS
    Sets up Sightline in this folder: a private Python environment (.venv) holding Sightline and its packages.

.DESCRIPTION
    1. Finds 64-bit Python 3.13 through the py launcher, or says how to get it.
    2. Creates .venv in this folder (or reuses one already made with 64-bit Python 3.13).
    3. Upgrades pip inside .venv.
    4. Installs Sightline and its packages into .venv (pip install -e .).

    Nothing is installed outside this folder and no Windows settings are changed.
    pip downloads the packages (about 1.2 GB once installed).  The speech and face
    models (about 620 MB) are downloaded later, by Sightline's set-up wizard.

.PARAMETER DryRun
    Only print the steps.  Nothing is created or installed.  (It still asks the py
    launcher which Pythons exist, which changes nothing.)

.PARAMETER Dev
    Also install the test tools: pip install -e ".[dev]" instead of pip install -e .

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -DryRun
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$Dev
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$Wanted = '3.13'
$Root = $PSScriptRoot
$Venv = Join-Path $Root '.venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'

function Format-Command([string]$Exe, [string[]]$Arguments) {
    $parts = @($Exe) + @($Arguments) | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
    return ($parts -join ' ')
}

function Invoke-Step([string]$Title, [string]$Exe, [string[]]$Arguments) {
    Write-Host ''
    Write-Host "==> $Title"
    Write-Host ('    ' + (Format-Command $Exe $Arguments))
    if ($DryRun) { return }
    # pip writes warnings to stderr; judge the step by its exit code, not by that.
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) { throw "This step failed (exit code $code): $Title" }
}

function Get-PythonInfo([string[]]$Command) {
    # Asks a Python for its version, bitness and path.  $null when that Python is not there.
    # Single quotes only inside the Python code: Windows PowerShell 5.1 mangles embedded double quotes.
    $probe = "import struct, sys; print('%d.%d' % sys.version_info[:2]); print(struct.calcsize('P') * 8); print(sys.executable)"
    $exe = $Command[0]
    $rest = @()
    if ($Command.Count -gt 1) { $rest = $Command[1..($Command.Count - 1)] }
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $exe @rest -c $probe 2>$null
        $code = $LASTEXITCODE
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $saved
    }
    $lines = @($out)
    if ($code -ne 0 -or $lines.Count -lt 3) { return $null }
    $bits = 0
    if (-not [int]::TryParse(([string]$lines[1]).Trim(), [ref]$bits)) { return $null }
    return [pscustomobject]@{ Version = ([string]$lines[0]).Trim(); Bits = $bits; Path = ([string]$lines[2]).Trim() }
}

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($null -eq $py) { return $null }
    # The tag form first (py launcher 3.11 and later, and the Python install manager), then the older forms.
    foreach ($selector in @("-V:$Wanted", "-$Wanted-64", "-$Wanted")) {
        $info = Get-PythonInfo @($py.Source, $selector)
        if ($null -ne $info -and $info.Version -eq $Wanted -and $info.Bits -eq 64) { return $info }
    }
    return $null
}

Write-Host 'Sightline set-up'
if ($DryRun) { Write-Host 'Dry run: the steps are printed, and nothing is created or installed.' }

if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Host ''
    Write-Host 'Sightline needs 64-bit Windows 10 or 11.'
    exit 1
}

$python = Find-Python
if ($null -eq $python) {
    Write-Host @"

Sightline needs 64-bit Python $Wanted, found through the py launcher, and this PC does not have it.

To get it:
  1. Go to https://www.python.org/downloads/windows/ and download the latest
     Python $Wanted "Windows installer (64-bit)".
  2. Run it, and leave "py launcher" ticked.
  3. Open a new PowerShell window and run this script again.

Or, if you use winget:  winget install --id Python.Python.$Wanted
"@
    exit 1
}
Write-Host ''
Write-Host "Found Python $($python.Version) ($($python.Bits)-bit): $($python.Path)"

$reuse = $false
if (Test-Path -LiteralPath $VenvPython) {
    $existing = Get-PythonInfo @($VenvPython)
    if ($null -ne $existing -and $existing.Version -eq $Wanted -and $existing.Bits -eq 64) {
        $reuse = $true
        Write-Host "Using the existing environment in $Venv"
    } else {
        $what = 'a Python that does not start'
        if ($null -ne $existing) { $what = "Python $($existing.Version) ($($existing.Bits)-bit)" }
        Write-Host ''
        Write-Host "The folder $Venv already holds $what, not 64-bit Python $Wanted."
        Write-Host 'Delete the .venv folder, then run this script again.'
        exit 1
    }
} elseif (Test-Path -LiteralPath $Venv) {
    Write-Host ''
    Write-Host "The folder $Venv exists but has no Python in it.  Delete it, then run this script again."
    exit 1
}

$target = '.'
if ($Dev) { $target = '.[dev]' }

Push-Location -LiteralPath $Root
try {
    if (-not $reuse) {
        Invoke-Step 'Create the environment in .venv' $python.Path @('-m', 'venv', $Venv)
    }
    Invoke-Step 'Upgrade pip' $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
    Invoke-Step 'Install Sightline and its packages (this takes a few minutes)' $VenvPython @('-m', 'pip', 'install', '-e', $target)
} finally {
    Pop-Location
}

Write-Host ''
if ($DryRun) {
    Write-Host 'Dry run finished: nothing was created or installed.  After a real run:'
} else {
    Write-Host 'Sightline is installed.'
}
Write-Host @'

For a first test, start it without the watchdog (closing its console window stops it):
    .venv\Scripts\sightline-console.exe

For everyday use, start it under the watchdog, which starts it again after a crash:
    .venv\Scripts\sightline.exe

The first start opens the set-up wizard, which downloads about 620 MB of models.
To stop Sightline, click its icon beside the clock and choose Quit Sightline.
'@
