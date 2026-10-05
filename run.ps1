# run.ps1 - start the meeting assistant
#   .\run.ps1 -Check                                    preflight (browser, model access, agent: files + MCP)
#   .\run.ps1 -Login                                    sign the bot account in once (saves teams_auth.json)
#   .\run.ps1 -Url "<Teams join link>"                  join (as the signed-in account, else guest)
#   .\run.ps1 -Url "<link>" -Project "C:\...\materials"  ground the agent in this meeting's materials
#   .\run.ps1 -Url "<link>" -Guest                      force guest join
# Model provider: Anthropic API by default (ANTHROPIC_API_KEY). If foundry.env.ps1 exists
# next to this script, it is loaded and the bot uses Microsoft Foundry instead.
param([string]$Url, [switch]$Check, [switch]$Login, [switch]$Guest, [string]$Project,
      [string]$Context, [string]$Model)
Set-Location $PSScriptRoot
if (Test-Path .\foundry.env.ps1) { . .\foundry.env.ps1 }
$py = ".\.venv\Scripts\python.exe"
$out = Join-Path $PSScriptRoot ("meetings\" + (Get-Date -Format "yyyy-MM-dd_HHmm"))
$a = @("teams_meeting_bot.py", "--outdir", $out)
if ($Check)   { $a += "--check" }
if ($Login)   { $a += "--login" }
if ($Url)     { $a += @("--url", $Url) }
if ($Guest)   { $a += "--guest" }
if ($Project) { $a += @("--project", $Project) }
if ($Context) { $a += @("--context", $Context) }
if ($Model)   { $a += @("--model", $Model) }
elseif ($env:CLAUDE_FOUNDRY_DEPLOYMENT) { $a += @("--model", $env:CLAUDE_FOUNDRY_DEPLOYMENT) }
& $py @a
