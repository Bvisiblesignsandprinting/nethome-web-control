$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) { throw "Run .\setup.ps1 first." }
$env:PYTHONPATH = $PSScriptRoot
& ".\.venv\Scripts\python.exe" setup_worker.py
