#!/usr/bin/env pwsh

<#
.SYNOPSIS
    Start diaMCP natively on Windows (no Docker, no WSL).
.DESCRIPTION
    Launches server.py with the Python interpreter already on your PATH.
    Use -Install once (or after changing requirements.txt) to install dependencies.
    Inside a conda environment -Install uses conda-forge; add -ForcePip to override.
.EXAMPLE
    .\start.ps1
.EXAMPLE
    .\start.ps1 -Install
.EXAMPLE
    .\start.ps1 -BindHost 0.0.0.0 -Port 8000
#>

[CmdletBinding()]
param(
    [string]$BindHost = "127.0.0.1",
    [int]$Port = 8000,
    [string]$Python = "",
    [switch]$Install,
    [switch]$ForcePip
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Resolve-Python {
    param([string]$Preferred)

    if ($Preferred) {
        if (Get-Command $Preferred -ErrorAction SilentlyContinue) { return $Preferred }
        throw "Python executable '$Preferred' was not found."
    }
    foreach ($candidate in @("python", "python3", "py")) {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    throw "Python was not found on PATH. Install Python 3.10+ from https://python.org or the Microsoft Store."
}

$pythonExe = Resolve-Python -Preferred $Python

$versionCheck = & $pythonExe -c "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "diaMCP requires Python 3.10 or newer (found $versionCheck)."
}
Write-Host "Using Python $versionCheck ($pythonExe)" -ForegroundColor Cyan

if ($Install) {
    $inConda = $env:CONDA_PREFIX -and (Get-Command conda -ErrorAction SilentlyContinue)
    if ($inConda -and -not $ForcePip) {
        # pip into a conda env shadows conda-managed packages; prefer conda-forge.
        Write-Host "Conda environment detected ($env:CONDA_PREFIX) - installing from conda-forge..." -ForegroundColor Cyan
        conda install -y -c conda-forge --file requirements-conda.txt
        if ($LASTEXITCODE -ne 0) { throw "conda install failed. Use -ForcePip to install with pip instead." }
    }
    else {
        Write-Host "Installing dependencies from requirements.txt..." -ForegroundColor Cyan
        & $pythonExe -m pip install --upgrade pip
        & $pythonExe -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $PSScriptRoot "workspace\tools") | Out-Null

$env:MCP_HOST = $BindHost
$env:MCP_PORT = "$Port"
$env:PYTHONPATH = $PSScriptRoot

Write-Host "Starting diaMCP on http://${BindHost}:${Port}/mcp" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray

& $pythonExe server.py
exit $LASTEXITCODE
