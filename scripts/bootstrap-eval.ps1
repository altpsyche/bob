#requires -Version 7
# Create the lm-evaluation-harness venv (tools/venv-eval/).
# Run once. After this, 'bob eval <role> [task]' benchmarks any model.
# This venv is NOT built by bootstrap.ps1 (it's benchmarking-only, and lm-eval + transformers are heavy)
# — it's provisioned lazily here, on first `bob eval`.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: New-BobVenv (self-heal + .lock/.txt + OS-aware python)

Write-Host "Creating eval venv (installing lm-eval — this may take a few minutes)..." -ForegroundColor Cyan
New-BobVenv -Name 'venv-eval' -RequirementsBase 'eval-requirements' -Quiet | Out-Null

Write-Host "lm-eval installed at tools/venv-eval/" -ForegroundColor Green
Write-Host "Quick smoke test:  bob eval coder gsm8k --limit 100  (~8 min)" -ForegroundColor DarkGray
Write-Host "Full benchmark:    bob eval coder gsm8k               (~90 min)" -ForegroundColor DarkGray
