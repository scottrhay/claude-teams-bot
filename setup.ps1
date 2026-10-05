# setup.ps1 - one-time install into a private virtual environment (nothing global changes)
# Usage (PowerShell, in this folder):  powershell -ExecutionPolicy Bypass -File .\setup.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py -3.11" } else { "python" }
if (-not (Test-Path .venv)) { Invoke-Expression "$py -m venv .venv" }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip -q
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt -q
& .\.venv\Scripts\python.exe -c "import sys, teams_meeting_bot as t; sys.exit(0 if t.bundled_engine() else 1)"
if ($LASTEXITCODE -ne 0) { throw "The Agent SDK installed without its claude.exe engine. Needs Windows x64; see requirements.txt." }
& .\.venv\Scripts\python.exe -m playwright install chromium
Write-Host "`nInstalled. Next:  .\.venv\Scripts\python.exe -m pytest -q   then   .\run.ps1 -Check"
