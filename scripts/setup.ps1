#requires -Version 7
# Master setup orchestrator (invoked by setup.bat). Idempotent; safe to re-run.
# Prerequisites must be installed first via install_prereqs.bat.
#
#   .\scripts\setup.ps1                        # full
#   .\scripts\setup.ps1 -SkipModels            # skip the ~38GB model downloads
#   .\scripts\setup.ps1 -SkipModels -SkipBuild # skip models + binary compilation (venvs + config only)
#   .\scripts\setup.ps1 -Profile 12gb          # smaller models for ~12GB VRAM (see config/models.psd1)
#   .\scripts\setup.ps1 -Launch                # start the stack (up.ps1) when finished
param([switch]$SkipModels, [switch]$SkipBuild, [switch]$SkipVoice, [switch]$Launch, [string]$Profile)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"
. "$PSScriptRoot\_common.ps1"   # Have, Install-WithWinget

$script:stepTotal   = 12
$script:stepCurrent = 0
$script:stepSw      = $null
$setupStart         = [Diagnostics.Stopwatch]::StartNew()
$isWin              = ((Get-BobOS) -eq 'windows')   # NC2 — gate the Windows-only provisioning steps

function Step {
    param([string]$Name, [string]$Hint = '')
    if ($script:stepCurrent -gt 0 -and $script:stepSw) {
        Write-Host "    done in $([int]$script:stepSw.Elapsed.TotalSeconds)s" -ForegroundColor DarkGray
    }
    $script:stepCurrent++
    $script:stepSw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "`n=== Step $script:stepCurrent/$script:stepTotal: $Name ===" -ForegroundColor Cyan
    if ($Hint) { Write-Host "  ($Hint)" -ForegroundColor DarkGray }
}

Step "System check"
try { & "$PSScriptRoot\diagnose.ps1" } catch { Write-Warning "diagnose: $_" }

Step "Core tooling"
if (-not (Have git)) { throw "git not found. Install Git, then re-run setup." }
if ($isWin) {
    if (-not (Have scoop)) { throw "scoop not found. Install it:  irm get.scoop.sh | iex   then re-run setup.bat." }
    Write-Host "git ok; scoop ok"
} else {
    Write-Host "git ok"
}

Step "Prerequisite check"
$missing = @()
if (-not (Have 'node')) { $missing += 'Node.js' }
if (-not (Have 'go'))   { $missing += 'Go' }
if ($isWin) {
    if (-not (Have 'uvx')) { $missing += 'uv' }
    $hasPy = $false; try { scoop prefix python312 *>$null; $hasPy = ($LASTEXITCODE -eq 0) } catch {}
    if (-not $hasPy)       { $missing += 'Python 3.12' }
} else {
    # Linux: prereqs installed by install_prereqs.sh (python3 via apt/dnf/pacman).
    if (-not (Have 'python3')) { $missing += 'Python 3.12' }
}
if ($missing) {
    Write-Host "`nMissing prerequisites: $($missing -join ', ')" -ForegroundColor Red
    $prereqCmd = if ($isWin) { 'install_prereqs.bat' } else { './install_prereqs.sh' }
    Write-Host "Run $prereqCmd first, then re-run setup." -ForegroundColor Yellow
    exit 1
}
Write-Host "  Prerequisites ok." -ForegroundColor DarkGray

# Refresh PATH so shims installed by scoop/winget are visible to bootstrap (Windows registry PATH;
# on Linux the process PATH already has everything install_prereqs.sh installed).
if ($isWin) {
    $env:PATH = [Environment]::GetEnvironmentVariable('PATH', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('PATH', 'User')
}

$vswhereExe = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'

Step "Prereq: C++ toolchain (compiler required for llama.cpp build)"
$serverExe = Get-BinExe -Base 'llama-server'   # NC2/NC3 — OS-aware output name
if (-not $SkipBuild -and -not (Test-Path $serverExe)) {
    if ($isWin) {
        $vsInstall = if (Test-Path $vswhereExe) {
            & $vswhereExe -latest -products * -requires Microsoft.VisualStudio.Workload.NativeDesktop -property installationPath 2>$null
        }
        if ($vsInstall) {
            Write-Host "MSVC ok" -ForegroundColor DarkGray
        } else {
            throw @"
VS2022 'Desktop development with C++' workload not found — required to compile llama.cpp.
  Install VS2022:  winget install Microsoft.VisualStudio.2022.Community
  Then open VS Installer -> Modify -> add workload: 'Desktop development with C++'
  Re-run setup.bat when done.  (Pass -SkipBuild if you have a prebuilt bin\llama-server.exe.)
"@
        }
    } else {
        if ((Have 'g++') -or (Have 'gcc') -or (Have 'cc')) {
            Write-Host "gcc/g++ ok" -ForegroundColor DarkGray
        } else {
            throw "No C++ compiler found — run ./install_prereqs.sh (installs build-essential), then re-run ./setup.sh. (Pass -SkipBuild if you have a prebuilt bin/llama-server.)"
        }
    }
} else {
    Write-Host "C++ toolchain check skipped (build not needed)" -ForegroundColor DarkGray
}

Step "Prereq: cmake 3.x (cmake 4.x excluded by llama.cpp version range)"
$cmakeOk = $false
$pathCmakeCmd = Get-Command cmake -ErrorAction SilentlyContinue
if ($pathCmakeCmd) {
    $cmakeVer = (& cmake --version 2>&1 | Select-Object -First 1) -replace 'cmake version\s+', ''
    if ([version]$cmakeVer -lt [version]'4.0') {
        $cmakeOk = $true
        Write-Host "cmake ok ($cmakeVer)" -ForegroundColor DarkGray
    } else {
        Write-Host "PATH cmake is $cmakeVer (4.x) — checking VS bundled cmake..." -ForegroundColor Yellow
    }
}
if (-not $cmakeOk -and $isWin -and (Test-Path $vswhereExe)) {
    $vsI = & $vswhereExe -latest -products * -requires Microsoft.VisualStudio.Component.VC.CMake.Project -property installationPath 2>$null
    if ($vsI -and (Test-Path (Join-Path $vsI 'Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe'))) {
        $cmakeOk = $true
        Write-Host "cmake ok (VS bundled 3.31.x)" -ForegroundColor DarkGray
    }
}
if (-not $cmakeOk) {
    if ($isWin) {
        Write-Host "Installing cmake 3.31.7 via winget..." -ForegroundColor Cyan
        Install-WithWinget 'Kitware.CMake' @('--version', '3.31.7')
    } else {
        # Rolling distros (Arch/CachyOS) ship only cmake 4.x, which llama.cpp rejects. Provision the
        # pinned Kitware 3.x now so it's cached in tools/ before the build (build-llama/whisper reuse it).
        $cm = Get-LinuxCmake3 -Repo $repo
        Write-Host "cmake 3.x ready: $cm" -ForegroundColor DarkGray
    }
}

Step "Bootstrap: submodules -> build engine+proxy -> venvs+tools -> models" "first build takes 5-15 min"
$ba = @{}
if ($SkipModels) { $ba['SkipModels'] = $true }
if ($SkipBuild)  { $ba['SkipBuild']  = $true }
if ($Profile)    { $ba['Profile']    = $Profile }
& "$PSScriptRoot\bootstrap.ps1" @ba

Step "Wire clients (Continue + aider)"
& "$PSScriptRoot\setup-clients.ps1"

Step "fabric (shell AI patterns)"
& "$PSScriptRoot\setup-fabric.ps1"

Step "Install 'llm' CLI command"
& "$PSScriptRoot\install-cli.ps1"

Step "Voice + Vision setup (Phase 2)" "builds whisper.cpp, downloads STT model + TTS voice + mmproj"
if ($SkipVoice) {
    Write-Host "  Skipped (-SkipVoice)." -ForegroundColor DarkGray
} else {
    & "$PSScriptRoot\setup-voice.ps1"
}

Step "Memory lock (mlock)"
$cfgForMlock = Get-ModelsConfig
$mlockBigEnabled = ($cfgForMlock.defaults.mlockBig -eq $true)
$ram = Get-SystemRamGB
if (-not $isWin) {
    # Linux: --mlock needs a raised RLIMIT_MEMLOCK (ulimit -l unlimited / /etc/security/limits.conf),
    # not a Windows privilege. Nothing to grant here; the flag just works when the limit allows it.
    Write-Host "  Linux: raise 'ulimit -l' (memlock) if you enable mlockBig; nothing to grant." -ForegroundColor DarkGray
} else {
$mlockGranted = $false
try { & "$PSScriptRoot\grant-mlock.ps1" -Check 2>&1 | Out-Null; $mlockGranted = ($LASTEXITCODE -eq 0) } catch {}

if ($mlockBigEnabled) {
    if ($mlockGranted) {
        Write-Host "  SeLockMemoryPrivilege: already granted  (mlockBig = true)." -ForegroundColor Green
    } else {
        Write-Host "  mlockBig = true in config — granting SeLockMemoryPrivilege (UAC prompt)..." -ForegroundColor Cyan
        & "$PSScriptRoot\grant-mlock.ps1"
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  mlock skipped — grant cancelled or failed. Run: bob mlock" -ForegroundColor Yellow
        } else {
            Write-Host "  NOTE: open a new terminal after setup completes for mlock to take effect." -ForegroundColor Yellow
        }
    }
} elseif ($ram -and $ram.FreeGB -ge 32) {
    Write-Host "  $($ram.TotalGB) GB RAM detected ($($ram.FreeGB) GB free) — mlockBig not enabled." -ForegroundColor DarkGray
    Write-Host "  Tip: add  mlockBig = `$true  to config/user.psd1, then run: bob mlock" -ForegroundColor DarkGray
    Write-Host "  This pins the planner's CPU-offloaded layers in RAM, preventing pagefile eviction." -ForegroundColor DarkGray
} else {
    $reason = if ($ram) { "$($ram.FreeGB) GB free RAM (need 32+)" } else { "RAM detection unavailable" }
    Write-Host "  mlock skipped — $reason.  Enable manually with: bob mlock" -ForegroundColor DarkGray
}
}   # end: Windows-only mlock branch (NC2)

Step "Docker services (Langfuse + SearXNG + n8n)"
$dockerExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
if ((Have 'docker') -or ($isWin -and (Test-Path $dockerExe))) {
    & "$PSScriptRoot\setup-docker.ps1"
} else {
    Write-Host "  Docker not installed — skipping Docker services." -ForegroundColor DarkGray
    Write-Host "  To add later: install docker (Linux: your package manager; Windows: install_prereqs.bat), then re-run setup." -ForegroundColor DarkGray
}

if ($script:stepSw) { Write-Host "    done in $([int]$script:stepSw.Elapsed.TotalSeconds)s" -ForegroundColor DarkGray }
Write-Host "`nSetup complete in $([int]$setupStart.Elapsed.TotalMinutes)m$($setupStart.Elapsed.Seconds)s." -ForegroundColor Green
Write-Host "Open a new terminal, then:  bob up   (or  bob help  for all commands)" -ForegroundColor Green

# Onboarding: run if no bob section in user.psd1 yet
$userCfg = Join-Path $repo 'config\user.psd1'
$needsOnboard = $true
if (Test-Path $userCfg) {
  $cfgRaw = Get-Content $userCfg -Raw -ErrorAction SilentlyContinue
  if ($cfgRaw -match '\bbob\s*=') { $needsOnboard = $false }
}
if ($needsOnboard) { & "$PSScriptRoot\onboard.ps1" }

if ($Launch) { & "$PSScriptRoot\up.ps1" }
