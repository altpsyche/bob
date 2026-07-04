#requires -Version 7
# Build whisper.cpp (CUDA by default, CPU-only fallback). Runs on Windows and Linux under pwsh (NC3):
# Windows uses the Visual Studio generator, Linux uses Ninja. Outputs whisper-server + whisper-cli.
#   -Force      Rebuild even if both binaries already exist.
#   -CpuOnly    Skip CUDA, build CPU-only (faster; base.en is fast on CPU anyway).
param([switch]$Force, [switch]$CpuOnly)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
$src  = Join-Path $repo "external\whisper.cpp"
$bin  = Join-Path $repo "bin"
. "$PSScriptRoot\_models.ps1"

$os         = Get-BobOS
$serverName = Get-BobExeName 'whisper-server'    # whisper-server.exe (win) | whisper-server (linux)
$cliName    = Get-BobExeName 'whisper-cli'
$serverExe  = Join-Path $bin $serverName
$cliExe     = Join-Path $bin $cliName
# VS is multi-config; Ninja is single-config (needs -DCMAKE_BUILD_TYPE).
$genArgs    = if ($os -eq 'windows') { @('-G', 'Visual Studio 17 2022') } else { @('-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release') }

if (-not $Force -and (Test-Path $serverExe) -and (Test-Path $cliExe)) {
    Write-Host "$serverName + $cliName already built — skipping (use -Force to rebuild)." -ForegroundColor DarkGray
    return
}

if (-not (Test-Path (Join-Path $src "CMakeLists.txt"))) {
    throw "whisper.cpp submodule not found at $src. Run: git submodule update --init --recursive"
}

# --- resolve cmake (same logic as build-llama.ps1 to avoid cmake 4.x incompatibility) ---
$cmakeExe = $null
if ($os -eq 'windows') {
    $pathCmake = Get-Command cmake -ErrorAction SilentlyContinue
    if ($pathCmake) {
        $cmakeVer = (& cmake --version 2>&1 | Select-Object -First 1) -replace 'cmake version\s+', ''
        if ([version]$cmakeVer -lt [version]'4.0') {
            $cmakeExe = 'cmake'
        } else {
            Write-Warning "PATH cmake is $cmakeVer (4.x) — incompatible with whisper.cpp. Looking for VS bundled cmake..."
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
    if (-not $cmakeExe) { throw "cmake not found. Install: winget install Kitware.CMake --version 3.31.7" }
} else {
    # cmake must be < 4.0 (4.x breaks CUDA-arch validation) — Get-LinuxCmake3 returns system cmake if
    # 3.x, else fetches the pinned Kitware 3.x. Same helper build-llama.ps1 uses.
    $cmakeExe = Get-LinuxCmake3 -Repo $repo
    if ($genArgs -contains 'Ninja' -and -not (Get-Command ninja -ErrorAction SilentlyContinue)) {
        throw "Ninja not found. Install it: sudo apt-get install -y ninja-build (or the dnf/pacman equivalent)."
    }
}
Write-Host "cmake: $cmakeExe" -ForegroundColor DarkGray

# --- clean build dir ---
$build = Join-Path $src "build"
if (Test-Path $build) { Remove-Item -Recurse -Force $build }

# --- configure & build ---
Push-Location $src
try {
    $cudaArgs = @()
    if (-not $CpuOnly) {
        $gpuInfo  = Get-GpuArch
        $arch     = if ($gpuInfo) { $gpuInfo.CudaArch } else { 120 }
        $CudaRoot = Get-BestCudaRoot -CudaArch $arch
        if ($CudaRoot) {
            if ($os -eq 'windows') { $env:CUDA_PATH = $CudaRoot }
            $cudaArgs = @('-DWHISPER_CUDA=ON', "-DCMAKE_CUDA_ARCHITECTURES=$arch", "-DCUDAToolkit_ROOT=$CudaRoot")
            # Pin the nvcc host compiler on Linux when the default g++ is too new for nvcc (same gap as
            # build-llama.ps1 — e.g. CachyOS gcc 16). $null on Windows / when the default is fine.
            $hostCxx = if ($os -ne 'windows') { Get-CudaHostCompiler } else { $null }
            if ($hostCxx) { $cudaArgs += "-DCMAKE_CUDA_HOST_COMPILER=$hostCxx"; Write-Host "CUDA host g++: $hostCxx" -ForegroundColor DarkGray }
            Write-Host "Building whisper.cpp (CUDA sm_$arch)..." -ForegroundColor Cyan
        } else {
            Write-Warning "CUDA toolkit not found — falling back to CPU-only build."
        }
    }
    if (-not $cudaArgs) {
        $cudaArgs = @('-DWHISPER_CUDA=OFF')
        Write-Host "Building whisper.cpp (CPU-only)..." -ForegroundColor Cyan
    }
    & $cmakeExe -B build @genArgs @cudaArgs -DWHISPER_BUILD_TESTS=OFF -DWHISPER_BUILD_EXAMPLES=ON
    if ($LASTEXITCODE -ne 0) { throw "cmake configure failed" }

    $sw = [Diagnostics.Stopwatch]::StartNew()
    & $cmakeExe --build build --config Release -j
    if ($LASTEXITCODE -ne 0) { throw "cmake build failed" }
    Write-Host "Build succeeded in $([int]$sw.Elapsed.TotalMinutes)m$($sw.Elapsed.Seconds)s." -ForegroundColor Green
} finally { Pop-Location }

# --- stage into _build_tmp\, then atomic-swap into bin/ ---
# VS puts binaries in build\bin\Release; Ninja/Make in build/bin.
$releaseBin = if ($os -eq 'windows') { Join-Path $build 'bin\Release' } else { Join-Path $build 'bin' }
$tmp = Join-Path $bin "_build_tmp_whisper"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null

try {
    if (Test-Path $releaseBin) {
        Copy-Item (Join-Path $releaseBin "*") $tmp -Force
    } else {
        # fallback: search for the server binary anywhere under build
        $found = Get-ChildItem $build -Recurse -Filter $serverName -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) {
            Copy-Item (Join-Path $found.DirectoryName "*") $tmp -Force
        } else {
            throw "$serverName not found in build output — build may have failed silently"
        }
    }

    if (-not (Test-Path (Join-Path $tmp $serverName))) {
        throw "$serverName missing from staged output — aborting"
    }
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    if (Test-Path $serverExe) { Move-Item $serverExe "$serverExe.bak" -Force }

    # Copy files one-by-one: exes always, shared libs only if not already present.
    # whisper.cpp and llama.cpp share GGML libs (ggml*.dll / libggml*.so) — don't overwrite locked ones.
    foreach ($f in Get-ChildItem $tmp) {
        $dest = Join-Path $bin $f.Name
        $isSharedLib = $f.Name -match '\.(dll|so)(\.\d+)*$'
        if ($isSharedLib -and (Test-Path $dest)) {
            # Skip: compatible shared lib already in bin/ (possibly loaded by a running process)
            continue
        }
        try { Copy-Item $f.FullName $dest -Force }
        catch {
            if ($isSharedLib) {
                Write-Warning "Skipped locked shared lib: $($f.Name) — already in bin/ from llama.cpp build"
            } else { throw }
        }
    }
} catch {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    throw
}
Remove-Item -Recurse -Force $tmp

Write-Host "Built. whisper-server at: $serverExe" -ForegroundColor Green
& $serverExe --version
