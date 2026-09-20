$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    py -m venv .venv
}

$python = ".\.venv\Scripts\python.exe"

function Run-Pip {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$PipArgs
    )

    & $python -m pip @PipArgs

    if ($LASTEXITCODE -ne 0) {
        throw "pip failed with exit code $LASTEXITCODE"
    }
}

function Test-Msmart {
    & $python -c "import msmart; print('msmart-ng import OK')"
    return ($LASTEXITCODE -eq 0)
}

try {
    Run-Pip -PipArgs @("install", "--upgrade", "pip")
}
catch {
    Write-Host ""
    Write-Host "Normal pip upgrade failed on this Windows/Python setup."
    Write-Host "Continuing with the installed pip version."
}

try {
    Run-Pip -PipArgs @("install", "-r", "requirements.txt")
}
catch {
    Write-Host ""
    Write-Host "Normal pip TLS verification failed."
    Write-Host "Retrying against the official PyPI hosts with trusted-host for this install only."
    Write-Host ""

    Run-Pip -PipArgs @(
        "install",
        "--trusted-host", "pypi.org",
        "--trusted-host", "files.pythonhosted.org",
        "-r", "requirements.txt"
    )
}

if (-not (Test-Msmart)) {
    Write-Host ""
    Write-Host "msmart-ng is still missing. Installing it explicitly from official PyPI..."
    Run-Pip -PipArgs @(
        "install",
        "--trusted-host", "pypi.org",
        "--trusted-host", "files.pythonhosted.org",
        "msmart-ng==2026.9.0"
    )
}

if (-not (Test-Msmart)) {
    throw "msmart-ng installation failed; local LAN temperature control cannot start."
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next run:"
Write-Host "  .\run_worker.ps1"
