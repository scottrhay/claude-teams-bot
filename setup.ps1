# setup.ps1 - one-time install into a private virtual environment (nothing global changes)
# Usage (PowerShell, in this folder):  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3.11" } else { "python" }
if (-not (Test-Path .venv)) { Invoke-Expression "$py -m venv .venv" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
& .\.venv\Scripts\python.exe -m playwright install chromium
Write-Host "`nInstalled. Next:  .\.venv\Scripts\python.exe -m pytest -q   then   .\run.ps1 -Check"
