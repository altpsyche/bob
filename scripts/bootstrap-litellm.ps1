#requires -Version 7
# Create the LiteLLM proxy venv (tools/venv-litellm/).
# Run once. After this, 'bob litellm' starts the proxy on port 8081.
# The `bob` runtime also runs under this venv (it imports openai/rich from here).
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot '_platform.ps1')   # NC1 seam: New-BobVenv (self-heal + .lock/.txt + OS-aware python)

Write-Host "Creating LiteLLM venv..." -ForegroundColor Cyan
# New-BobVenv resolves the interpreter (uv-provisioned on Linux when the system Python is out of range),
# self-heals a stale/out-of-range venv, and installs from litellm-requirements.lock (Windows) / .txt
# (Linux). sqlite-utils (bob memory) is pinned in those requirements — no separate install needed.
New-BobVenv -Name 'venv-litellm' -RequirementsBase 'litellm-requirements' -Quiet | Out-Null

Write-Host "LiteLLM installed at tools/venv-litellm/" -ForegroundColor Green
Write-Host "Start proxy: bob litellm   (listens on http://localhost:8081/v1)" -ForegroundColor DarkGray
