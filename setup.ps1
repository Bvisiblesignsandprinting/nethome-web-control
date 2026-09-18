$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) { py -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
Write-Host ""
Write-Host "Setup complete. Next run:"
Write-Host "  .\setup_credentials.ps1"
Write-Host "  .\run.ps1"
