# Install the agents and the story-ctl controller for VS Code and the Copilot CLI (Windows).
# usage: powershell -ExecutionPolicy Bypass -File tools\install.ps1     (from the root of this repository)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$ag = Join-Path $env:USERPROFILE '.copilot\agents'
$sc = Join-Path $env:USERPROFILE '.story-ctl'
New-Item -ItemType Directory -Force -Path $ag, $sc | Out-Null
Copy-Item -Path '*.agent.md' -Destination $ag -Force
Write-Host "agents copied to $ag"
Copy-Item -Path 'tools\story-ctl.py' -Destination (Join-Path $sc 'story-ctl.py') -Force
$pk = Join-Path $sc 'storyctl'
if (Test-Path $pk) { Remove-Item -Recurse -Force $pk }
New-Item -ItemType Directory -Force -Path $pk | Out-Null
Copy-Item -Path 'tools\storyctl\*.py' -Destination $pk -Force
Write-Host "controller copied to $sc (story-ctl.py and the storyctl package)"
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { Write-Warning "The Python launcher 'py' was not found; the story agent runs 'py'. Install Python from python.org (it includes py)." }
if (-not (Get-Command copilot -ErrorAction SilentlyContinue)) { Write-Warning "The Copilot CLI (copilot) is not on PATH; the story agent runs the agents through it." }
if (-not (Get-Command code -ErrorAction SilentlyContinue)) { Write-Host "NOTE: 'code' is not on PATH, so stops will not open in an editor tab by themselves; use the link in the chat instead." }
& py (Join-Path $sc 'story-ctl.py') --help | Out-Null
Write-Host "OK. In VS Code, open a new chat, choose the story agent and paste a story."
