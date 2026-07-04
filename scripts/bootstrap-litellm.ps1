#requires -Version 7
# Create the LiteLLM proxy venv (tools/venv-litellm/).
# Run once. After this, 'bob litellm' starts the proxy on port 8081.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: Get-VenvExe (Scripts\ on Windows, bin/ on Linux)
if (-not (Test-PythonVersionAtLeast -Exe python -MinVer '3.12')) {
    $pyVer = & python --version 2>&1
    $hint = if ((Get-BobOS) -eq 'windows') { 'scoop install python312' } else { 'your package manager (e.g. apt install python3, pacman -S python)' }
    throw "Python 3.12+ required (found: $pyVer). Install: $hint"
}
$repo = Split-Path $PSScriptRoot -Parent
$venv = Join-Path (Join-Path $repo 'tools') 'venv-litellm'   # not 'tools\venv-litellm' — backslash is a literal filename char on Linux

if (Test-Path $venv) {
    Write-Host "venv-litellm already exists — skipping. Delete it to reinstall." -ForegroundColor DarkGray
    return
}

Write-Host "Creating LiteLLM venv..." -ForegroundColor Cyan
python -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "python -m venv failed. Is Python 3.12 installed?" }

# Use the venv's own python + `-m pip` (portable) rather than the pip console script, whose name and
# location differ by OS (Scripts\pip.exe vs bin/pip). Get-VenvExe resolves the right python per OS.
$venvPy = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'

Write-Host "Installing litellm[proxy]..." -ForegroundColor Cyan
& $venvPy -m pip install -r (Join-Path (Join-Path $repo 'tools') 'litellm-requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

Write-Host "Installing sqlite-utils (bob memory)..." -ForegroundColor Cyan
& $venvPy -m pip install sqlite-utils --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install sqlite-utils failed." }

Write-Host "LiteLLM installed at tools/venv-litellm/" -ForegroundColor Green
Write-Host "Start proxy: bob litellm   (listens on http://localhost:8081/v1)" -ForegroundColor DarkGray
