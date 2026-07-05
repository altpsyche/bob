#requires -Version 7
# Create the LiteLLM proxy venv (tools/venv-litellm/).
# Run once. After this, 'bob litellm' starts the proxy on port 8081.
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: Get-VenvExe (Scripts\ on Windows, bin/ on Linux)
# Windows: require a 3.12 on PATH. Linux/macOS: Get-BobPython returns an in-range (3.11/3.12) interpreter,
# provisioning CPython 3.12 via uv when the system Python is too new (Arch 3.14, which the deps reject).
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
$venv = Join-Path (Join-Path $repo 'tools') 'venv-litellm'   # not 'tools\venv-litellm' — backslash is a literal filename char on Linux

if (Test-Path $venv) {
    Write-Host "venv-litellm already exists — skipping. Delete it to reinstall." -ForegroundColor DarkGray
    return
}

Write-Host "Creating LiteLLM venv ($py)..." -ForegroundColor Cyan
& $py -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "python -m venv failed." }

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
