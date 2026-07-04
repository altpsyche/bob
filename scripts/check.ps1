#!/usr/bin/env pwsh
# N8 — pre-commit / CI gate. Runs, in order:
#   1. py_compile over scripts/, plugins/, tests/ (excluding external/)
#   2. PowerShell AST parse over scripts/*.ps1 and config/*.psd1
#   3. the stdlib-unittest suite in tests/   (skip with -NoTests)
# Exits non-zero on the first category that fails, so a git pre-commit hook (or CI) blocks.
param([switch]$NoTests)

$repo = Split-Path $PSScriptRoot -Parent
# NB6 — CI (no venv-litellm) sets BOB_PYTHON to its own interpreter so this one gate runs on both
# Linux + Windows. Locally BOB_PYTHON is unset and the venv python is used, unchanged.
$venvPy = if ($env:BOB_PYTHON) { $env:BOB_PYTHON }
          elseif ($IsWindows)  { Join-Path $repo 'tools\venv-litellm\Scripts\python.exe' }
          else                 { Join-Path $repo 'tools/venv-litellm/bin/python' }   # Linux/macOS venv layout
if (-not (Test-Path $venvPy) -and -not (Get-Command $venvPy -ErrorAction SilentlyContinue)) {
  Write-Host "[check] python not found at '$venvPy' — run scripts/bootstrap-litellm.ps1 (or set BOB_PYTHON)" -ForegroundColor Red
  exit 1
}
$failed = $false

# 1. py_compile ------------------------------------------------------------
Write-Host '[check] py_compile...' -ForegroundColor Cyan
$pyFiles = Get-ChildItem -Path (Join-Path $repo 'scripts'), (Join-Path $repo 'plugins'), (Join-Path $repo 'tests') `
  -Recurse -Filter *.py -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '[\\/]external[\\/]' } |
  Select-Object -ExpandProperty FullName
& $venvPy -m py_compile @pyFiles
if ($LASTEXITCODE -ne 0) { Write-Host '[check] py_compile FAILED' -ForegroundColor Red; $failed = $true }

# 2. PowerShell AST parse --------------------------------------------------
# Exclude bob-toast.ps1: it uses WinRT type accelerators
# ([Windows.UI.Notifications...,ContentType=WindowsRuntime]) which the static AST parser reports
# as errors even though PowerShell runs them fine — a known ParseFile false positive.
Write-Host '[check] PowerShell AST parse...' -ForegroundColor Cyan
$psFiles = Get-ChildItem -Path (Join-Path $repo 'scripts'), (Join-Path $repo 'config') `
  -Recurse -Include *.ps1, *.psd1 -File -ErrorAction SilentlyContinue |
  Where-Object { $_.FullName -notmatch '[\\/]external[\\/]' -and $_.Name -ne 'bob-toast.ps1' }
foreach ($f in $psFiles) {
  $errs = $null
  [System.Management.Automation.Language.Parser]::ParseFile($f.FullName, [ref]$null, [ref]$errs) | Out-Null
  if ($errs) {
    Write-Host "[check] PARSE ERROR: $($f.FullName)" -ForegroundColor Red
    $errs | ForEach-Object { Write-Host "     $($_.Message)" -ForegroundColor DarkYellow }
    $failed = $true
  }
}

# 2b. platform-seam unit tests (NC1) --------------------------------------
# Pure resolvers assert both -Os branches regardless of host, so the Linux paths are proven here on
# the ubuntu runner AND on windows. No venv/models/network — safe to run in every gate.
Write-Host '[check] platform-seam tests...' -ForegroundColor Cyan
& (Join-Path $repo 'scripts\test-platform.ps1')
if ($LASTEXITCODE -ne 0) { Write-Host '[check] platform-seam tests FAILED' -ForegroundColor Red; $failed = $true }

# 3. config/verbs.json in sync with the command registry (NB4) ------------
# verbs.json is generated from scripts/bob/registry.py and read by the shim; a registry edit that
# doesn't regenerate it would drift the front door. Static check — runs even with -NoTests.
Write-Host '[check] verbs.json in sync...' -ForegroundColor Cyan
$prevPyPath = $env:PYTHONPATH
$env:PYTHONPATH = Join-Path $repo 'scripts'
& $venvPy -m bob.registry --check
if ($LASTEXITCODE -ne 0) { Write-Host '[check] verbs.json STALE — run: python -m bob.registry' -ForegroundColor Red; $failed = $true }
$env:PYTHONPATH = $prevPyPath

# 3b. versions.lock in sync with its sources (ND1) --------------------------
# versions.lock is generated (git gitlinks + models.psd1 + manifest.json + pip freeze); a bumped
# submodule that wasn't re-locked would drift the reproducibility pin. Static, pwsh-only (models.psd1
# is PowerShell), needs no venv — submodule commits come from `git rev-parse HEAD:<path>` so it works
# even in the CI core-suite where submodules aren't checked out. Runs even with -NoTests.
Write-Host '[check] versions.lock in sync...' -ForegroundColor Cyan
. (Join-Path $repo 'scripts\_models.ps1')   # dot-sources _versions.ps1 (Test-VersionsLockSync)
if ((Test-VersionsLockSync) -ne 0) { Write-Host '[check] versions.lock STALE — run: bob lock' -ForegroundColor Red; $failed = $true }

# 3c. executable bits on the Linux entrypoints (NC2) ------------------------
# git tracks the +x bit; a file committed 100644 makes './install_prereqs.sh' die with
# 'permission denied' on every fresh Linux/macOS clone (Windows .bat ignores the bit, so it hides).
# Assert the shell entrypoints + hook source stay executable. Static, git-only (no venv), runs
# even with -NoTests, and works on Windows since it reads the tracked mode, not the filesystem.
Write-Host '[check] entrypoint exec bits...' -ForegroundColor Cyan
$mustExec = @('bob', 'install_prereqs.sh', 'setup.sh', 'scripts/hooks/pre-commit')
foreach ($f in $mustExec) {
  $entry = git -C $repo ls-files --stage -- $f
  if (-not $entry) { Write-Host "[check] MISSING from index: $f" -ForegroundColor Red; $failed = $true; continue }
  $mode = ($entry -split '\s+')[0]
  if ($mode -ne '100755') {
    Write-Host "[check] NOT EXECUTABLE: $f (git mode $mode) — run: git update-index --chmod=+x $f" -ForegroundColor Red
    $failed = $true
  }
}

# 4. unittest suite --------------------------------------------------------
if (-not $NoTests) {
  Write-Host '[check] unittest suite...' -ForegroundColor Cyan
  $env:PYTHONIOENCODING = 'utf-8'
  & $venvPy -m unittest discover -s (Join-Path $repo 'tests') -p 'test_*.py'
  if ($LASTEXITCODE -ne 0) { Write-Host '[check] tests FAILED' -ForegroundColor Red; $failed = $true }
  $env:PYTHONIOENCODING = $null
}

if ($failed) { Write-Host '[check] FAILED' -ForegroundColor Red; exit 1 }
Write-Host '[check] all green' -ForegroundColor Green
exit 0
