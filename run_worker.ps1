$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv")) { throw "Run .\setup.ps1 first." }
$env:PYTHONPATH = $PSScriptRoot
$env:NETHOME_ALLOW_WRITES = "true"
& ".\.venv\Scripts\python.exe" worker.py
