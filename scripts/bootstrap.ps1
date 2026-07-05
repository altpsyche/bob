#requires -Version 7
# One-shot setup: submodules -> build engine + proxy -> Python venvs -> fetch models.
# Re-runnable. Heavy steps are skippable via flags.
#   .\scripts\bootstrap.ps1                 # full
#   .\scripts\bootstrap.ps1 -SkipModels     # everything except the multi-GB downloads
param([switch]$SkipModels, [switch]$SkipBuild, [string]$Profile, [switch]$WithWebui)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"

function Have($n) { [bool](Get-Command $n -ErrorAction SilentlyContinue) }
function Step($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }

# Profile selection. Explicit -Profile wins; otherwise suggest one from detected VRAM (never forces).
if ($Profile) {
  Step "Select profile '$Profile'"; Set-ActiveProfile $Profile
} else {
  $vram = Get-GpuVramGB
  $sug  = Get-SuggestedProfile -VramGB $vram
  $active = (Get-ModelsConfig).activeProfile
  if ($sug -and $sug -ne $active) {
    Step "VRAM check — auto-selecting profile"
    Write-Host "Detected ~$vram GB VRAM -> switching profile '$active' -> '$sug'" -ForegroundColor Cyan
    Set-ActiveProfile $sug
  } elseif ($sug) {
    Write-Host "VRAM ~$vram GB -> profile '$active' (good fit)." -ForegroundColor DarkGray
  }
}

# GPU architecture + compatible CUDA root (used for prereq report and passed to build-llama).
$gpuArch  = Get-GpuArch
$cudaRoot = if ($gpuArch) { Get-BestCudaRoot -CudaArch $gpuArch.CudaArch } else { Get-BestCudaRoot -CudaArch 120 }

# --- prereq report ---
Step "Prereqs"
if (-not (Have git)) { throw "git missing" }
"git    : ok"
if (Have cmake) { "cmake  : ok" } else { "cmake  : not on PATH (VS cmake or auto-install handles this)" }
if ($gpuArch) { "GPU    : $($gpuArch.Gen) (sm_$($gpuArch.CudaArch))" }
"CUDA   : $(if ($cudaRoot) { "ok — $cudaRoot" } else { 'MISSING — install CUDA 12.x before building' })"
"go     : $(if (Have go) { 'ok' } else { 'missing — will need llama-swap release binary instead' })"

# locate a venv-compatible Python (3.11/3.12; pinned deps like open-webui cap at <3.13). Resolution
# (scoop/py/PATH on Windows, uv-provisioned on Linux) lives in the Get-BobVenvPython seam — shared with
# New-BobVenv so there's one interpreter-selection path, not a hand-rolled copy per bootstrap script.
$py = Get-BobVenvPython
$pyHint = if ((Get-BobOS) -eq 'windows') { 'scoop install python312' } else { 'install Python 3.12, or ensure uv is available so bob can provision it' }
"python : $(if ($py) { $py } else { "MISSING — $pyHint" })"

# --- submodules ---
Step "Submodules"
git -C $repo submodule update --init --recursive
if ($LASTEXITCODE -ne 0) { throw "submodule init failed" }

# --- build engine + proxy ---
if (-not $SkipBuild) {
  if ($cudaRoot) {
    $label = if ($gpuArch) { "$($gpuArch.Gen) sm_$($gpuArch.CudaArch)" } else { 'sm_120 (default)' }
    Step "Build llama.cpp ($label)"
    $buildArgs = @{ CudaRoot = $cudaRoot }
    if ($gpuArch) { $buildArgs['Arch'] = $gpuArch.CudaArch }
    & "$PSScriptRoot\build-llama.ps1" @buildArgs
  } else {
    # NC8 — no CUDA toolkit: build the CPU-only tier instead of skipping, so a GPU-less box still
    # gets a working (if slow) llama-server. `bob profile auto` then selects the 'cpu' profile.
    Step "Build llama.cpp (CPU-only — no CUDA toolkit found)"
    & "$PSScriptRoot\build-llama.ps1" -Cpu
  }

  Step "Build llama-swap"
  if (Have go) { & "$PSScriptRoot\build-llama-swap.ps1" }
  else { Write-Warning "Skipping llama-swap build — Go missing. Download the llama-swap release binary into bin/ (llama-swap.exe on Windows, llama-swap on Linux)." }

  Step "Build fabric"
  if (Have go) { & "$PSScriptRoot\setup-fabric.ps1" }
  else { Write-Warning "Skipping fabric build — Go missing." }
} else { Write-Host "Skipping builds (-SkipBuild)" -ForegroundColor DarkGray }

# --- Python tools: ISOLATED venvs (open-webui & aider have conflicting dep pins) ---
# venv-aider + venv-litellm are the default clients. venv-webui (open-webui pulls torch/transformers,
# multi-GB) is opt-in via -WithWebui. venv-eval (lm-eval + transformers, benchmarking-only) is NOT built
# here — it's lazy: `bob eval` / scripts\bootstrap-eval.ps1 own it. All go through the New-BobVenv seam
# (self-heal + .lock/.txt selection + loud failure live in one place).
Step "Python venvs (3.12+) + tools"
if ($py) {
  $venvs = @(
    @{n='venv-aider';   base='aider-requirements'},
    @{n='venv-litellm'; base='litellm-requirements'}   # sqlite-utils (bob memory) is pinned in the requirements
  )
  if ($WithWebui) { $venvs += @{n='venv-webui'; base='webui-requirements'} }
  else { Write-Host "  skipping venv-webui (open-webui is opt-in — re-run with -WithWebui to install)" -ForegroundColor DarkGray }
  foreach ($t in $venvs) {
    New-BobVenv -Name $t.n -RequirementsBase $t.base -Python $py | Out-Null
  }
} else { Write-Warning "Skipping venvs — Python 3.12+ not found." }

# --- runtime config (generated from config/models.psd1; runs even with -SkipModels) ---
Step "Generate llama-swap config"
& "$PSScriptRoot\gen-llama-swap.ps1"

# --- models ---
if (-not $SkipModels) { Step "Fetch models (multi-GB)"; & "$PSScriptRoot\fetch-models.ps1" }
else { Write-Host "Skipping model downloads (-SkipModels). Run scripts\fetch-models.ps1 later." -ForegroundColor DarkGray }

Step "Done"
Write-Host "Next: bob up   (endpoint :8080 + LiteLLM proxy :8081 start automatically — see docs\USAGE.md)" -ForegroundColor Green
