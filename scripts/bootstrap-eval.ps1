#requires -Version 7
# Create the lm-evaluation-harness venv (tools/venv-eval/).
# Run once. After this, 'bob eval <role> [task]' benchmarks any model.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: Test-PythonVersionAtLeast, Get-BobOS
if ((Get-BobOS) -eq 'windows') {
    if (-not (Test-PythonVersionAtLeast -Exe python -MinVer '3.12')) {
        throw "Python 3.12+ required (found: $(& python --version 2>&1)). Install: scoop install python312"
    }
    $py = 'python'
} else {
    $py = Get-BobPython
    if (-not $py) { throw "No venv-compatible Python (3.11/3.12) found and couldn't provision one via uv. Install Python 3.12 and re-run." }
}
$repo = Split-Path $PSScriptRoot -Parent
$venv = Join-Path $repo 'tools\venv-eval'

if (Test-Path $venv) {
    Write-Host "venv-eval already exists — skipping. Delete it to reinstall." -ForegroundColor DarkGray
    return
}

Write-Host "Creating eval venv ($py)..." -ForegroundColor Cyan
& $py -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "python -m venv failed." }

Write-Host "Installing lm-eval (this may take a few minutes)..." -ForegroundColor Cyan
# OS-aware venv python (Windows Scripts\python.exe | Linux/macOS bin/python); use `python -m pip`.
$venvPy = if ($IsWindows) { Join-Path $venv 'Scripts\python.exe' } else { Join-Path $venv 'bin/python' }
& $venvPy -m pip install -r "$repo\tools\eval-requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

Write-Host "lm-eval installed at tools/venv-eval/" -ForegroundColor Green
Write-Host "Quick smoke test:  bob eval coder gsm8k --limit 100  (~8 min)" -ForegroundColor DarkGray
Write-Host "Full benchmark:    bob eval coder gsm8k               (~90 min)" -ForegroundColor DarkGray
