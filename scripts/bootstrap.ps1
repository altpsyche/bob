#requires -Version 7
# One-shot setup: submodules -> build engine + proxy -> Python venvs -> fetch models.
# Re-runnable. Heavy steps are skippable via flags.
#   .\scripts\bootstrap.ps1                 # full
#   .\scripts\bootstrap.ps1 -SkipModels     # everything except the multi-GB downloads
param([switch]$SkipModels, [switch]$SkipBuild, [string]$Profile)
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

# locate a venv-compatible Python (3.11/3.12; pinned deps like open-webui cap at <3.13).
$py = $null
if ((Get-BobOS) -eq 'windows') {
    # Windows: scoop python312 (preferred, isolated) -> py launcher -> PATH, pinned to 3.12.
    try { $p = (scoop prefix python312) 2>$null; if ($p) { $py = Join-Path $p "python.exe" } } catch {}
    if ($py -and -not (Test-Path $py)) { $py = $null }
    if (-not $py -and (Have py)) {
        try { $resolved = & py -3.12 -c "import sys; print(sys.executable)" 2>&1; if ($resolved -and (Test-Path $resolved)) { $py = $resolved } } catch {}
    }
    if (-not $py) {
        foreach ($cand in 'python3.12', 'python', 'python3') {
            if ((Have $cand) -and (Test-PythonVersionAtLeast -Exe $cand -MinVer '3.12')) { $py = $cand; break }
        }
    }
    $pyHint = 'scoop install python312'
} else {
    # Linux/macOS: an in-range interpreter, provisioning CPython 3.12 via uv when the system Python is
    # too new (Arch/CachyOS ship 3.14, which open-webui & other pinned deps reject with Requires-Python).
    $py = Get-BobPython
    $pyHint = 'install Python 3.12, or ensure uv is available so bob can provision it'
}
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
  else { Write-Warning "Skipping llama-swap build — Go missing. Download the release binary into bin\llama-swap.exe." }

  Step "Build fabric"
  if (Have go) { & "$PSScriptRoot\setup-fabric.ps1" }
  else { Write-Warning "Skipping fabric build — Go missing." }
} else { Write-Host "Skipping builds (-SkipBuild)" -ForegroundColor DarkGray }

# --- Python tools: ISOLATED venvs (open-webui & aider have conflicting dep pins) ---
Step "Python venvs (3.12+) + tools"
if ($py) {
  foreach ($t in @(
    @{n='venv-webui';   base='webui-requirements'},
    @{n='venv-aider';   base='aider-requirements'},
    @{n='venv-litellm'; base='litellm-requirements'},
    @{n='venv-eval';    base='eval-requirements'}
  )) {
    $venv = Join-Path $repo "tools\$($t.n)"
    $venvPy = Get-VenvExe -Venv $t.n -Exe 'python'   # NC4: OS-aware (Scripts\python.exe | bin/python)
    # Self-heal: recreate a venv built with an out-of-range Python (e.g. a pre-fix 3.14 venv left by a
    # failed run) so it doesn't just re-fail the pip install. In range = 3.11/3.12.
    if ((Test-Path $venv) -and (Test-Path $venvPy)) {
        $vv = (& $venvPy --version 2>&1) -replace 'Python\s+', ''
        $inRange = ($vv -match '(\d+)\.(\d+)') -and ([version]"$($Matches[1]).$($Matches[2])" -ge [version]'3.11') -and ([version]"$($Matches[1]).$($Matches[2])" -lt [version]'3.13')
        if (-not $inRange) { Write-Host "  recreating $($t.n) (was Python $vv — need 3.11/3.12)" -ForegroundColor Yellow; Remove-Item -Recurse -Force $venv }
    }
    if (-not (Test-Path $venv)) { & $py -m venv $venv }
    if (-not (Test-Path $venvPy)) { throw "venv creation failed for $($t.n) — $venvPy not found" }
    & $venvPy -m pip install --upgrade pip
    # The committed .lock files are Windows `pip freeze` snapshots (they pin pywin32 / win32_setctime),
    # so they only resolve on Windows. Use the pinned lock on Windows for reproducibility; on Linux/macOS
    # use the platform-agnostic top-level .txt and let pip resolve per-platform. (A Linux lock could be
    # frozen post-install for reproducibility — ND follow-up.)
    $lock = Join-Path $repo "tools\$($t.base).lock"
    $txt  = Join-Path $repo "tools\$($t.base).txt"
    $req  = if (((Get-BobOS) -eq 'windows') -and (Test-Path $lock)) { $lock } else { $txt }
    Write-Host "  installing $($t.n) from $(Split-Path $req -Leaf)" -ForegroundColor DarkGray
    & $venvPy -m pip install -r $req
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $($t.n) — re-run scripts\bootstrap.ps1 to retry" }
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
