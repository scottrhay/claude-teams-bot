# install_shortcut.ps1 - adds "Claude-Teams-Bot" to the Desktop and the Start menu.
# Usage (in this folder):  powershell -ExecutionPolicy Bypass -File .\install_shortcut.ps1
$here = $PSScriptRoot
$pythonw = Join-Path $here '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { Write-Error "Run setup.ps1 first (.venv not found)."; exit 1 }
$ws = New-Object -ComObject WScript.Shell
$targets = @([Environment]::GetFolderPath('Desktop'),
             (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'))
foreach ($dir in $targets) {
    $lnk = $ws.CreateShortcut((Join-Path $dir 'Claude-Teams-Bot.lnk'))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '"' + (Join-Path $here 'app.py') + '"'
    $lnk.WorkingDirectory = $here
    $lnk.IconLocation = (Join-Path $here 'claude_teams_bot.ico')
    $lnk.Description = 'Claude meeting assistant for Microsoft Teams'
    $lnk.Save()
    Write-Host "Shortcut created: $dir\Claude-Teams-Bot.lnk"
}
