$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" setup_credentials.py
