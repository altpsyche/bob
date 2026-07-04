#!/usr/bin/env pwsh
# N8 — install Bob's versioned git hooks into .git/hooks (which git does not track).
# Run once per clone:  pwsh -File scripts\install-hooks.ps1
$repo = Split-Path $PSScriptRoot -Parent
# Build paths component-by-component so they resolve on every OS (backslash literals break on Linux/macOS).
$src = Join-Path $repo 'scripts' 'hooks' 'pre-commit'
$hooksDir = Join-Path $repo '.git' 'hooks'
if (-not (Test-Path $hooksDir)) {
  Write-Host "No .git/hooks directory found — is this a git checkout?" -ForegroundColor Red
  exit 1
}
$dest = Join-Path $hooksDir 'pre-commit'
Copy-Item $src $dest -Force
# Ensure the installed hook is executable (git ignores a non-executable hook on Linux/macOS).
if ($IsLinux -or $IsMacOS) { & chmod +x $dest }
Write-Host "Installed pre-commit hook -> $dest" -ForegroundColor Green
Write-Host "It runs scripts/check.ps1 (py_compile + PowerShell parse + unittest) and blocks on failure." -ForegroundColor DarkGray
