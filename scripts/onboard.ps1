#requires -Version 7
# First-run onboarding: asks name, work context, DeepSeek API key.
# Writes profile to SQLite (via bob-memory.ps1) and API key to config/user.json (ONE-C C0c: the neutral
# overlay, read by both languages; was user.psd1). Invoked by setup.ps1 if there's no user.json bob section.
$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot -Parent

function Write-Bob { param($msg) Write-Host "Bob: $msg" -ForegroundColor Cyan }

Write-Host ""
Write-Bob "Hi. Let me set up your profile."
Write-Host ""

# --- Name ---
Write-Bob "What's your name?"
$userName = Read-Host ">"
if ([string]::IsNullOrWhiteSpace($userName)) { $userName = "User" }

# --- Work context ---
Write-Bob "What kind of work do you do most? (e.g. game dev, web, writing)"
$userWork = Read-Host ">"
if ([string]::IsNullOrWhiteSpace($userWork)) { $userWork = "software development" }

# --- DeepSeek API key (optional) ---
Write-Bob "Got a DeepSeek API key? Enables cloud-quality answers when you want them. (Enter to skip)"
$apiKey = Read-Host ">"
$apiKey  = $apiKey.Trim()

# --- Save profile to SQLite ---
$memPs = Join-Path $PSScriptRoot 'bob-memory.ps1'
if (Test-Path $memPs) {
  try {
    & $memPs init-profile --name $userName --work $userWork
  } catch {
    Write-Warning "Could not save profile to memory DB: $_"
  }
}

# --- Update config/user.json with bob section + optional API key ---
# ONE-C C0c: the overlay is neutral JSON now — build it structurally (ConvertFrom/To-Json) rather than
# string-splicing a psd1. ONE-A: no persona.name written here (dead key); the user's name lives in the
# SQLite profile (init-profile --name above); the bob section just marks presence.
$userCfg = Join-Path $repo 'config\user.json'
$cfg = if (Test-Path $userCfg) {
  try { Get-Content -Raw $userCfg | ConvertFrom-Json -AsHashtable } catch { @{} }
} else { @{} }
if (-not $cfg.ContainsKey('bob')) { $cfg['bob'] = @{} }

$keyAdded = $false
if ($apiKey -and $apiKey -ne '') {
  if (-not $cfg.ContainsKey('peers'))              { $cfg['peers'] = @{} }
  if (-not $cfg['peers'].ContainsKey('deepseek'))  { $cfg['peers']['deepseek'] = @{} }
  if ($cfg['peers']['deepseek']['apiKey'] -ne $apiKey) {
    $cfg['peers']['deepseek']['apiKey'] = $apiKey
    $keyAdded = $true
  }
}
$cfg | ConvertTo-Json -Depth 10 | Set-Content $userCfg -Encoding utf8

if ($keyAdded) {
  # Regenerate LiteLLM config so the key takes effect
  Write-Host "Regenerating config with API key..."
  try { & "$PSScriptRoot\bob.ps1" gen 2>$null } catch {}
}

Write-Host ""
Write-Bob "Ready, $userName. Type 'bob chat' to start."
Write-Host ""
