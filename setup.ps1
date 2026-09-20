$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) { py -m venv .venv }

$python = ".\.venv\Scripts\python.exe"

function Invoke-Pip {
    param([string[]]$Args)
    & $python -m pip @Args
    if ($LASTEXITCODE -ne 0) {
        throw "pip failed with exit code $LASTEXITCODE"
    }
}

function Test-Msmart {
    & $python -c "import msmart; print('msmart-ng import OK')"
    return ($LASTEXITCODE -eq 0)
}

try {
    Invoke-Pip @("install", "--upgrade", "pip")
}
catch {
    Write-Host "Skipping pip upgrade because this Python 3.14 install rejects the PyPI certificate chain."
}

try {
    Invoke-Pip @("install", "-r", "requirements.txt")
}
catch {
    Write-Host ""
    Write-Host "Normal pip TLS verification failed on this Windows/Python setup."
    Write-Host "Retrying against the official PyPI hosts with trusted-host for this install only."
    Write-Host ""

    Invoke-Pip @(
        "install",
        "--trusted-host", "pypi.org",
        "--trusted-host", "files.pythonhosted.org",
        "-r", "requirements.txt"
    )
}

# Python 3.14 on this machine can report a certificate error without leaving
# msmart-ng installed. Verify the import and repair it explicitly if needed.
if (-not (Test-Msmart)) {
    Write-Host ""
    Write-Host "msmart-ng is still missing. Installing it explicitly from official PyPI..."
    Invoke-Pip @(
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
