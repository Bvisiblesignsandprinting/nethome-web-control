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

try {
    Invoke-Pip @("install", "--upgrade", "pip")
    Invoke-Pip @("install", "-r", "requirements.txt")
}
catch {
    Write-Host ""
    Write-Host "Normal pip TLS verification failed on this Windows/Python setup."
    Write-Host "Retrying only against the official PyPI hosts using trusted-host."
    Write-Host ""

    Invoke-Pip @(
        "install",
        "--trusted-host", "pypi.org",
        "--trusted-host", "files.pythonhosted.org",
        "-r", "requirements.txt"
    )
}

Write-Host ""
Write-Host "Setup complete."
Write-Host "Next run:"
Write-Host "  .\run_worker.ps1"
