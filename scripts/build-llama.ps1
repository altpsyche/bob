#requires -Version 7
# Build llama.cpp for any NVIDIA GPU (or CPU-only). Auto-detects architecture and CUDA root from
# nvidia-smi. Runs on Windows and Linux under pwsh (NC3/NC8): Windows uses the Visual Studio generator
# + staged CUDA runtime DLLs; Linux uses Ninja and resolves .so via rpath/ldconfig.
#   -Arch <int>       CMake CUDA architecture (e.g. 89 for Ada, 86 for Ampere, 120 for Blackwell).
#                     Omit to auto-detect via nvidia-smi (falls back to 120 if undetectable).
#   -CudaRoot <path>  Path to the CUDA toolkit root. Omit to auto-select for the detected arch.
#   -Cpu              CPU-only build (-DGGML_CUDA=OFF). No CUDA toolkit required; no DLL staging.
#   -Force            Rebuild even if the server binary already exists.
param([int]$Arch = 0, [string]$CudaRoot = '', [switch]$Force, [switch]$Cpu)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$src  = Join-Path $repo "external\llama.cpp"
$bin  = Join-Path $repo "bin"
. "$PSScriptRoot\_models.ps1"

$os      = Get-BobOS
$exeName = Get-BobExeName 'llama-server'                       # llama-server.exe (win) | llama-server (linux)
$flags   = Resolve-BuildCmakeFlags -Cpu:$Cpu -Arch $Arch -Os $os   # @{ Cuda; Generator; StageDlls }

if (-not $Force -and (Test-Path (Join-Path $bin $exeName))) {
  Write-Host "$exeName already built — skipping (use -Force to rebuild)." -ForegroundColor DarkGray
  return
}

if (-not (Test-Path (Join-Path $src "CMakeLists.txt"))) {
  throw "llama.cpp submodule not found at $src. Run: git submodule update --init --recursive"
}

if ($flags.Cuda) {
  # --- resolve architecture ---
  if ($Arch -eq 0) {
    $gpuInfo = Get-GpuArch
    if ($gpuInfo) {
      $Arch = $gpuInfo.CudaArch
      Write-Host "Detected GPU: $($gpuInfo.Gen) (sm_$Arch)" -ForegroundColor Cyan
    } else {
      Write-Warning "Could not detect GPU via nvidia-smi. Defaulting to sm_120 (Blackwell). Pass -Arch to override, or use -Cpu."
      $Arch = 120
    }
  }

  # --- resolve CUDA root ---
  if (-not $CudaRoot) {
    $CudaRoot = Get-BestCudaRoot -CudaArch $Arch
    if (-not $CudaRoot) {
      if ($Arch -ge 120) {
        throw "CUDA Toolkit >= 12.8 not found. Blackwell (sm_120) needs CUDA 12.8 or newer (12.8, 12.9, 13.x — the sm_120 + MMQ floor). Install it (Linux: ./install_prereqs.sh installs it), pass -CudaRoot, or build CPU-only with -Cpu."
      } else {
        throw "No compatible CUDA toolkit found for sm_$Arch. Install CUDA 12.x, pass -CudaRoot, or build CPU-only with -Cpu."
      }
    }
  }

  # Discovery (Get-CudaRoot) guarantees CudaRoot >= 12.8 for Blackwell — the MMQ-capable floor — so no
  # "toolkit too old" warning here. Whether the fast MMQ path actually activated is a per-llama.cpp-commit
  # property (TUNING.md notes it can regress): verify it after building — see docs/TUNING.md
  # "Verifying the fast path" — rather than inferring it from the toolkit version.

  Write-Host "Architecture : sm_$Arch" -ForegroundColor Cyan
  Write-Host "CUDA toolkit : $CudaRoot" -ForegroundColor Cyan

  # Linux CUDA host compiler: nvcc rejects a too-new default g++ (e.g. CachyOS gcc 16). Resolve a
  # compatible one (honors $NVCC_CCBIN, else newest older g++-NN); pass it to cmake below. $null on
  # Windows / when the default is fine.
  $cudaHostCxx = if ($os -ne 'windows') { Get-CudaHostCompiler } else { $null }
  if ($cudaHostCxx) { Write-Host "CUDA host g++: $cudaHostCxx" -ForegroundColor DarkGray }

  if ($os -eq 'windows') {
    # Set env vars the VS build system uses to locate the toolkit (winget installs don't always set these).
    $env:CUDA_PATH = $CudaRoot
    $verTag = (Split-Path $CudaRoot -Leaf) -replace '^v', '' -replace '\.', '_'   # "12.8" -> "12_8"
    Set-Item "env:CUDA_PATH_V$verTag" $CudaRoot
  }
  # DLL major version for staging (cublas64_12.dll vs cublas64_11.dll etc.)
  $cudaMajor = if ((Split-Path $CudaRoot -Leaf) -match '^v?(\d+)') { $Matches[1] } else { '12' }
} else {
  Write-Host "CPU build (-DGGML_CUDA=OFF) — no GPU / CUDA toolkit required." -ForegroundColor Cyan
}

# --- clean build dir (stale cache can silently force the slow cuBLAS path) ---
$build = Join-Path $src "build"
if (Test-Path $build) { Remove-Item -Recurse -Force $build }

# --- locate cmake ---
$cmakeExe = $null
if ($os -eq 'windows') {
  # llama.cpp's cmake_minimum_required(3.14...3.28) excludes cmake 4.x, causing CUDA architecture
  # validation failures. Check PATH cmake version first; only fall back to VS's bundled cmake if
  # PATH cmake is 4.x (e.g. the Python scoop package installs cmake 4.x).
  $pathCmake = Get-Command cmake -ErrorAction SilentlyContinue
  if ($pathCmake) {
      $cmakeVer = (& cmake --version 2>&1 | Select-Object -First 1) -replace 'cmake version\s+', ''
      if ([version]$cmakeVer -lt [version]'4.0') {
          $cmakeExe = 'cmake'
      } else {
          Write-Warning "PATH cmake is $cmakeVer (4.x) — incompatible with llama.cpp. Looking for VS bundled cmake..."
      }
  }
  if (-not $cmakeExe) {
      $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
      if (Test-Path $vswhere) {
          $vsInstall = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.CMake.Project -property installationPath 2>$null
          if ($vsInstall) {
              $candidate = Join-Path $vsInstall 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'
              if (Test-Path $candidate) { $cmakeExe = $candidate }
          }
      }
  }
  if (-not $cmakeExe) {
      # scoop cmake tracks latest (4.x) — use winget to install a pinned 3.x build instead
      Write-Host "Installing cmake 3.31.7 via winget..." -ForegroundColor Cyan
      winget install Kitware.CMake --version 3.31.7 --silent --accept-package-agreements --accept-source-agreements
      if ($LASTEXITCODE -ne 0) {
          throw "cmake install failed. Fix manually: winget install Kitware.CMake --version 3.31.7"
      }
      # Refresh PATH so the newly installed cmake is visible in this process
      $env:PATH = [System.Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' +
                  [System.Environment]::GetEnvironmentVariable('PATH', 'User')
      $cmakeExe = (Get-Command cmake -ErrorAction SilentlyContinue)?.Source
      if (-not $cmakeExe) { throw "cmake still not found after install — open a new terminal and retry." }
  }
} else {
  # Linux: cmake MUST be < 4.0 (4.x fails llama.cpp's CUDA-arch validation). Get-LinuxCmake3 returns
  # the system cmake if it's 3.x, else fetches the pinned Kitware 3.x (rolling distros ship only 4.x).
  $cmakeExe = Get-LinuxCmake3 -Repo $repo
  if ($flags.Generator -eq 'Ninja' -and -not (Get-Command ninja -ErrorAction SilentlyContinue)) {
    throw "Ninja not found. Install it: sudo apt-get install -y ninja-build (or the dnf/pacman equivalent)."
  }
}
Write-Host "cmake       : $cmakeExe" -ForegroundColor DarkGray

# Add -DCMAKE_CUDA_FLAGS="-allow-unsupported-compiler" below if nvcc rejects your MSVC version.
Push-Location $src
try {
  if ($flags.Cuda -and $os -eq 'windows') {
    # Windows CUDA — unchanged from the pre-NC invocation.
    & $cmakeExe -B build -G "Visual Studio 17 2022" -T "cuda=$CudaRoot" `
      -DGGML_CUDA=ON `
      -DCMAKE_CUDA_ARCHITECTURES="$Arch" `
      -DGGML_CUDA_FORCE_CUBLAS=OFF `
      -DCUDAToolkit_ROOT="$CudaRoot"
  } elseif ($flags.Cuda) {
    # Linux CUDA — Ninja generator, single-config; no VS toolset selector. Pin the host compiler when
    # the default g++ is too new for nvcc (-DCMAKE_CUDA_HOST_COMPILER); omit the flag otherwise.
    $hostCxxArg = if ($cudaHostCxx) { @("-DCMAKE_CUDA_HOST_COMPILER=$cudaHostCxx") } else { @() }
    & $cmakeExe -B build -G $flags.Generator `
      -DGGML_CUDA=ON `
      -DCMAKE_CUDA_ARCHITECTURES="$Arch" `
      -DGGML_CUDA_FORCE_CUBLAS=OFF `
      -DCUDAToolkit_ROOT="$CudaRoot" `
      -DCMAKE_BUILD_TYPE=Release `
      @hostCxxArg
  } elseif ($os -eq 'windows') {
    # Windows CPU-only.
    & $cmakeExe -B build -G "Visual Studio 17 2022" -DGGML_CUDA=OFF
  } else {
    # Linux CPU-only.
    & $cmakeExe -B build -G $flags.Generator -DGGML_CUDA=OFF -DCMAKE_BUILD_TYPE=Release
  }
  if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

  $bsw = [Diagnostics.Stopwatch]::StartNew()
  & $cmakeExe --build build --config Release -j
  if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }
  Write-Host "Build succeeded in $([int]$bsw.Elapsed.TotalMinutes)m$($bsw.Elapsed.Seconds)s." -ForegroundColor Green
} finally { Pop-Location }

# --- stage into _build_tmp\, then atomic-swap into bin/ on success ---
# VS is multi-config (build\bin\Release); Ninja/Make is single-config (build/bin).
$outDir = if ($os -eq 'windows') { Join-Path $build 'bin\Release' } else { Join-Path $build 'bin' }
$tmp = Join-Path $bin "_build_tmp"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
  Copy-Item (Join-Path $outDir "*") $tmp -Force
  if ($flags.StageDlls) {
    # CUDA runtime DLLs are not statically linked on Windows — copy them so bin/ runs without CUDA on PATH.
    foreach ($dll in "cublas64_$cudaMajor.dll", "cublasLt64_$cudaMajor.dll", "cudart64_$cudaMajor.dll") {
      Copy-Item (Join-Path $CudaRoot "bin\$dll") $tmp -Force -ErrorAction SilentlyContinue
    }
  }
  if (-not (Test-Path (Join-Path $tmp $exeName))) {
    throw "$exeName missing from staged output — aborting swap"
  }
  New-Item -ItemType Directory -Force -Path $bin | Out-Null
  $svr = Join-Path $bin $exeName
  if (Test-Path $svr) { Move-Item $svr "$svr.bak" -Force }
  Copy-Item (Join-Path $tmp "*") $bin -Force
} catch {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
  throw
}
Remove-Item -Recurse -Force $tmp

Write-Host "Built. llama-server at: $(Join-Path $bin $exeName)" -ForegroundColor Green
& (Join-Path $bin $exeName) --version
