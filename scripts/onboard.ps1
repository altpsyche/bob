#requires -Version 7
# First-run onboarding: asks name, work context, DeepSeek API key.
# Writes profile to SQLite (via bob-memory.ps1) and API key to config/user.psd1.
# Invoked by setup.ps1 if config/user.psd1 has no [bob] section.
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

# --- Update config/user.psd1 with bob section + optional API key ---
$userCfg = Join-Path $repo 'config\user.psd1'
# ONE-A: no persona.name written here — it was a dead key (read nowhere at runtime). The user's name
# lives in the SQLite profile (init-profile --name above); this just marks the [bob] section present.
$bobSection = @"

# Per-machine bob overrides (empty by default; see config/bob.psd1 for the shape)
bob = @{
}
"@

if (Test-Path $userCfg) {
  $existing = Get-Content $userCfg -Raw
  # Only append if no [bob] section already present
  if ($existing -notmatch '\bbob\s*=') {
    # Insert bob section before closing '}'
    $updated = $existing.TrimEnd().TrimEnd('}') + $bobSection + "}`n"
    Set-Content $userCfg $updated -Encoding utf8
  }
} else {
  # Create minimal user.psd1
  @"
@{
$bobSection}
"@ | Set-Content $userCfg -Encoding utf8
}

# --- API key: append to peers section in user.psd1 ---
if ($apiKey -and $apiKey -ne '') {
  $existing = Get-Content $userCfg -Raw
  if ($existing -notmatch 'deepseek.*apiKey') {
    $peerBlock = @"

  # DeepSeek API key (added by onboard.ps1)
  peers = @{
    deepseek = @{
      apiKey = '$apiKey'
    }
  }
"@
    # Insert before closing '}'
    $updated = $existing.TrimEnd().TrimEnd('}') + $peerBlock + "`n}`n"
    Set-Content $userCfg $updated -Encoding utf8
    # Regenerate LiteLLM config so the key takes effect
    Write-Host "Regenerating config with API key..."
    try { & "$PSScriptRoot\bob.ps1" gen 2>$null } catch {}
  }
}

Write-Host ""
Write-Bob "Ready, $userName. Type 'bob chat' to start."
Write-Host ""
