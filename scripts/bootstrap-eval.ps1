#requires -Version 7
# Create the lm-evaluation-harness venv (tools/venv-eval/).
# Run once. After this, 'bob eval <role> [task]' benchmarks any model.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: Test-PythonVersionAtLeast, Get-BobOS
if (-not (Test-PythonVersionAtLeast -Exe python -MinVer '3.12')) {
    $pyVer = & python --version 2>&1
    $hint = if ((Get-BobOS) -eq 'windows') { 'scoop install python312' } else { 'your package manager (e.g. apt install python3, pacman -S python)' }
    throw "Python 3.12+ required (found: $pyVer). Install: $hint"
}
$repo = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $repo 'tools\venv-eval'

if (Test-Path $venv) {
    Write-Host "venv-eval already exists — skipping. Delete it to reinstall." -ForegroundColor DarkGray
    return
}

Write-Host "Creating eval venv..." -ForegroundColor Cyan
python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "python -m venv failed. Is Python 3.12 installed?" }

Write-Host "Installing lm-eval (this may take a few minutes)..." -ForegroundColor Cyan
# OS-aware venv python (Windows Scripts\python.exe | Linux/macOS bin/python); use `python -m pip`.
$venvPy = if ($IsWindows) { Join-Path $venv 'Scripts\python.exe' } else { Join-Path $venv 'bin/python' }
& $venvPy -m pip install -r "$repo\tools\eval-requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

Write-Host "lm-eval installed at tools/venv-eval/" -ForegroundColor Green
Write-Host "Quick smoke test:  bob eval coder gsm8k --limit 100  (~8 min)" -ForegroundColor DarkGray
Write-Host "Full benchmark:    bob eval coder gsm8k               (~90 min)" -ForegroundColor DarkGray
