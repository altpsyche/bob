#requires -Version 7
# NC1 (contracts C3 secrets, C4 data-dir) — the OS-abstraction seam for the PowerShell orchestration.
# The pwsh mirror of scripts/osenv.py: one place that knows the OS, so the rest of the .ps1 stays
# OS-neutral and the same scripts run under `pwsh` on Windows and Linux.
#
# Dot-sourced from the top of _models.ps1 (so every entry script that dot-sources _models.ps1 gets
# the seam with no per-script edits) and from _common.ps1 (for Install-Package). It must NOT
# dot-source _models.ps1 back — the single edge is _models.ps1 -> _platform.ps1 (acyclic).
#
# Design: each OS-branching capability is a PURE `Resolve-*` (takes an explicit -Os, returns a
# path / command-spec / candidate list, no side effects, no native calls) plus a thin executor that
# calls the resolver and performs the one effect. Call sites use the executor (OS auto-detected);
# tests call the resolver with -Os 'windows'|'linux' and assert on the returned spec, so the Linux
# branches are unit-testable on a Windows box (mirrors how test_osenv.py monkeypatches
# platform.system()). Real Linux execution is proven on the CI Linux runner.

$script:PlatRepo = Split-Path $PSScriptRoot -Parent

# --- OS oracle -----------------------------------------------------------------------------------
# All OS detection funnels through here (never a raw $IsWindows scattered in a function). $IsWindows/
# $IsLinux/$IsMacOS are read-only pwsh 7 automatics — untestable by reassignment — so Get-BobOS also
# honors a TEST-ONLY $env:BOB_FORCE_OS ('windows'|'linux'|'macos'); anything else warns + is ignored.
function Get-BobOS {
  $forced = $env:BOB_FORCE_OS
  if ($forced) {
    $f = $forced.ToLower()
    if ($f -in @('windows', 'linux', 'macos')) { return $f }
    Write-Warning "BOB_FORCE_OS='$forced' is not one of windows/linux/macos — ignoring."
  }
  if ($IsWindows) { return 'windows' }
  if ($IsLinux)   { return 'linux' }
  if ($IsMacOS)   { return 'macos' }
  return 'windows'   # pwsh 5.1 has no autovars, but everything here is #requires -Version 7
}

# --- data / state location (C4) ------------------------------------------------------------------
# Mirrors osenv.data_dir/cache_dir/_migrate_once: repo-relative data/ + logs/ by default (local-first,
# zero migration); BOB_DATA_DIR relocates them with a one-time .migrated-stamped copy.

function Resolve-DataDir {
  # PURE. Returns @{ Dir; Migrate; From } — the plan, no mkdir/copy. Only ~ is expanded (like
  # Path().expanduser()); no env-var expansion, matching osenv.
  param([string]$Os = (Get-BobOS), [string]$Override = $env:BOB_DATA_DIR, [string]$Repo = $script:PlatRepo)
  if (-not $Override) { return @{ Dir = (Join-Path $Repo 'data'); Migrate = $false; From = $null } }
  $dir = if ($Override.StartsWith('~')) { Join-Path $HOME ($Override.Substring(1).TrimStart('/', '\')) } else { $Override }
  return @{ Dir = $dir; Migrate = $true; From = (Join-Path $Repo 'data') }
}

function Invoke-DataDirMigration {
  # One-time copy of data/* into a freshly-used BOB_DATA_DIR; .migrated stamp so it never re-copies
  # and never clobbers newer files in dst (mirrors osenv._migrate_once).
  param([Parameter(Mandatory)][string]$Src, [Parameter(Mandatory)][string]$Dst)
  $stamp = Join-Path $Dst '.migrated'
  if ((Test-Path $stamp) -or -not (Test-Path $Src) -or
      ((Resolve-Path $Src).Path -eq (Resolve-Path $Dst).Path)) { return }
  foreach ($item in Get-ChildItem -LiteralPath $Src -Force) {
    $target = Join-Path $Dst $item.Name
    if (Test-Path $target) { continue }
    try { Copy-Item -LiteralPath $item.FullName -Destination $target -Recurse -Force -ErrorAction Stop }
    catch { }  # best-effort; a partial copy must not crash startup
  }
  Set-Content -LiteralPath $stamp -Value '' -NoNewline -Encoding utf8
}

function Get-DataDir {
  # EXECUTOR: realize the plan (mkdir + one-time migration) and return the dir.
  $plan = Resolve-DataDir
  if (-not (Test-Path $plan.Dir)) { New-Item $plan.Dir -ItemType Directory -Force | Out-Null }
  if ($plan.Migrate) { Invoke-DataDirMigration -Src $plan.From -Dst $plan.Dir }
  return $plan.Dir
}

function Get-CacheDir {
  # Log/cache dir: repo logs/ by default, <BOB_DATA_DIR>/logs when overridden (mirrors osenv.cache_dir).
  $override = $env:BOB_DATA_DIR
  $d = if ($override) {
    $base = if ($override.StartsWith('~')) { Join-Path $HOME ($override.Substring(1).TrimStart('/', '\')) } else { $override }
    Join-Path $base 'logs'
  } else { Join-Path $script:PlatRepo 'logs' }
  if (-not (Test-Path $d)) { New-Item $d -ItemType Directory -Force | Out-Null }
  return $d
}

# --- secrets (C3) --------------------------------------------------------------------------------
# Precedence identical to osenv.secret: env (exact name, then BOB_<UPPER>) -> OS keychain ->
# <data_dir>/secrets.json -> default. No secret is ever read from a git-tracked file.

function Get-SecretsFile { return (Join-Path (Get-DataDir) 'secrets.json') }

function Resolve-KeychainCmd {
  # PURE. The keychain lookup spec, or $null when there is no supported store. Windows has no
  # built-in credential API reachable from pwsh without P/Invoke (out of scope, matches osenv's
  # optional-keyring skip); Linux uses secret-tool (service 'bob') when present.
  param([Parameter(Mandatory)][string]$Name, [string]$Os = (Get-BobOS))
  if ($Os -eq 'linux') {
    return @{ Exe = 'secret-tool'; Args = @('lookup', 'service', 'bob', 'account', $Name) }
  }
  return $null
}

function Get-KeychainSecret {
  # EXECUTOR: run the keychain spec if its tool is present; best-effort, $null on any failure.
  param([Parameter(Mandatory)][string]$Name, [string]$Os = (Get-BobOS))
  $spec = Resolve-KeychainCmd -Name $Name -Os $Os
  if (-not $spec) { return $null }
  if (-not (Get-Command $spec.Exe -ErrorAction SilentlyContinue)) { return $null }
  try {
    $val = & $spec.Exe @($spec.Args) 2>$null
    if ($val) { return ("$val").Trim() }
  } catch { }  # keychain backend unavailable — fall through to the file
  return $null
}

function Get-Secret {
  param([Parameter(Mandatory)][string]$Name, $Default = $null, [string]$Os = (Get-BobOS))
  # 1. process env — exact name, then BOB_<UPPER> (case-sensitive, like os.environ).
  $val = [Environment]::GetEnvironmentVariable($Name)
  if (-not $val) { $val = [Environment]::GetEnvironmentVariable('BOB_' + $Name.ToUpper()) }
  if ($val) { return $val }
  # 2. OS keychain (optional).
  $val = Get-KeychainSecret -Name $Name -Os $Os
  if ($val) { return $val }
  # 3. <data_dir>/secrets.json (never a tracked file).
  $sf = Get-SecretsFile
  if (Test-Path $sf) {
    try {
      $data = Get-Content -Raw -LiteralPath $sf | ConvertFrom-Json -AsHashtable
      if ($data -is [hashtable] -and $data[$Name]) { return $data[$Name] }
    } catch { }  # a malformed secrets file must not leak or crash — treat as absent
  }
  # 4. default (may be a config-carried value on Windows).
  return $Default
}

# --- GPU / hardware ------------------------------------------------------------------------------

function Get-GpuInfo {
  # Unified GPU probe. Both Get-GpuVramGB and Get-GpuArch (defined in _models.ps1) already shell out
  # to nvidia-smi via Get-Command, which is identical on Windows + Linux — so this is cross-platform
  # as-is. Returns @{ VramGB; CudaArch; Gen; MinCudaMajor } or $null when no NVIDIA GPU is present.
  $vram = Get-GpuVramGB
  $arch = Get-GpuArch
  if (-not $vram -and -not $arch) { return $null }
  $info = @{ VramGB = $vram }
  if ($arch) { $info.CudaArch = $arch.CudaArch; $info.Gen = $arch.Gen; $info.MinCudaMajor = $arch.MinCudaMajor }
  return $info
}

function Resolve-CudaRootCandidates {
  # PURE. Ordered probe description for the arch+OS (no Test-Path). Windows preserves the exact
  # layout the old Get-BestCudaRoot used; Linux uses the distro CUDA layout.
  param([int]$CudaArch = 0, [string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') {
    $base = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
    if ($CudaArch -ge 120) { return @{ Base = $base; DirPrefix = 'v'; Pin = 'v12.8'; MinMajor = 12; MinVer = '12.8' } }
    return @{ Base = $base; DirPrefix = 'v'; Pin = $null; MinMajor = ($CudaArch -ge 75 ? 11 : 10); MinVer = ($CudaArch -ge 75 ? '11.0' : '10.0') }
  }
  # Linux: canonical /usr/local/cuda (NVIDIA/Ubuntu) + /opt/cuda (Arch/pacman) symlinks first, then
  # versioned /usr/local/cuda-<maj.min>, plus $CUDA_HOME / $CUDA_PATH if the caller has set them.
  # Blackwell (sm_120) needs CUDA >= 12.8 (the sm_120 + MMQ fast-path floor); the canonical 12.8 path
  # is preferred, but any newer toolkit (12.9, 13.x) also qualifies — Get-CudaRoot enforces MinVer,
  # not an exact pin (the old code rejected 13.x and never looked in /opt/cuda).
  $fixed = @('/usr/local/cuda', '/opt/cuda')
  if ($env:CUDA_HOME) { $fixed += $env:CUDA_HOME }
  if ($env:CUDA_PATH) { $fixed += $env:CUDA_PATH }
  return @{
    Base = '/usr/local'; DirPrefix = 'cuda-'; Fixed = $fixed
    Pin = ($CudaArch -ge 120 ? '/usr/local/cuda-12.8' : $null)
    MinMajor = ($CudaArch -ge 120 ? 12 : ($CudaArch -ge 75 ? 11 : 10))
    MinVer = ($CudaArch -ge 120 ? '12.8' : ($CudaArch -ge 75 ? '11.0' : '10.0'))
  }
}

function Get-CudaToolkitVersion {
  # Best-effort CUDA toolkit version at a root dir. Reads version.json (12.x/13.x layout), then the
  # older version.txt, then falls back to `bin/nvcc --version`. Returns [version] or $null. Lets
  # Get-CudaRoot enforce the arch's minimum against unversioned canonical roots (e.g. Arch's
  # /opt/cuda, whose name carries no version).
  param([string]$Root)
  if (-not $Root -or -not (Test-Path $Root)) { return $null }
  $vj = Join-Path $Root 'version.json'
  if (Test-Path $vj) {
    try {
      $v = (Get-Content $vj -Raw | ConvertFrom-Json).cuda.version
      if ($v -match '(\d+)\.(\d+)') { return [version]"$($Matches[1]).$($Matches[2])" }
    } catch {}
  }
  $vt = Join-Path $Root 'version.txt'
  if (Test-Path $vt) {
    if ((Get-Content $vt -Raw) -match '(\d+)\.(\d+)') { return [version]"$($Matches[1]).$($Matches[2])" }
  }
  $nvcc = Join-Path $Root 'bin' (Get-BobExeName 'nvcc')
  if (Test-Path $nvcc) {
    try { if (((& $nvcc --version 2>$null) -join "`n") -match 'release (\d+)\.(\d+)') { return [version]"$($Matches[1]).$($Matches[2])" } } catch {}
  }
  return $null
}

function Get-CudaRoot {
  # EXECUTOR: probe on disk for the NEWEST CUDA toolkit meeting the arch's minimum version, or $null.
  # Canonical/pinned/env roots (version read from disk) and versioned <prefix><maj.min> dirs (version
  # from the name) are pooled and ranked >= MinVer. Blackwell (sm_120) requires >= 12.8 but accepts
  # 12.9 / 13.x — the old exact-12.8 pin rejected newer toolkits and never probed Arch's /opt/cuda.
  param([int]$CudaArch = 0)
  $c = Resolve-CudaRootCandidates -CudaArch $CudaArch
  $minVer = [version]($c.MinVer ?? '0.0')
  $found = [System.Collections.Generic.List[object]]::new()

  # explicit roots: canonical pin + fixed symlinks + env vars (unversioned -> read version from disk)
  $explicit = @()
  if ($c.Pin) { $explicit += (($c.Pin -match '^[/~]|^[A-Za-z]:') ? $c.Pin : (Join-Path $c.Base $c.Pin)) }
  $explicit += @($c.Fixed)
  foreach ($p in $explicit) {
    if ($p -and (Test-Path $p)) {
      $v = Get-CudaToolkitVersion -Root $p
      if ($v) { $found.Add([pscustomobject]@{ Path = (Resolve-Path $p).Path; Ver = $v }) }
    }
  }
  # versioned dirs under the base (version comes from the directory name, no disk read)
  if (Test-Path $c.Base) {
    $pat = "^$([regex]::Escape($c.DirPrefix))(\d+)\.(\d+)$"
    Get-ChildItem $c.Base -Directory -ErrorAction SilentlyContinue | ForEach-Object {
      if ($_.Name -match $pat) { $found.Add([pscustomobject]@{ Path = $_.FullName; Ver = [version]"$($Matches[1]).$($Matches[2])" }) }
    }
  }

  $ok = $found | Where-Object { $_.Ver -ge $minVer } | Sort-Object Ver -Descending
  if ($ok) { return (@($ok)[0]).Path }
  return $null
}

function Get-CudaHostCompiler {
  # EXECUTOR (Linux CUDA): the g++ nvcc should use as its host compiler. Some distros ship a default
  # g++ newer than nvcc supports (e.g. CachyOS gcc 16 vs CUDA's ceiling) which fails the build with
  # "unsupported GNU version". Honor $NVCC_CCBIN if it resolves (Arch's /etc/profile.d/cuda.sh and the
  # fish drop-in install_prereqs writes both set it); else pick the newest versioned g++-NN older than
  # the default (Arch: g++-15 under default 16). Returns a path, or $null to use the default g++.
  if ((Get-BobOS) -eq 'windows') { return $null }
  if ($env:NVCC_CCBIN) { $cc = Get-Command $env:NVCC_CCBIN -ErrorAction SilentlyContinue; if ($cc) { return $cc.Source } }
  $defMajor = 0
  if ((Get-Command g++ -ErrorAction SilentlyContinue) -and (((& g++ -dumpversion 2>$null) -join '') -match '^(\d+)')) { $defMajor = [int]$Matches[1] }
  $cands = Get-ChildItem '/usr/bin' -Filter 'g++-*' -ErrorAction SilentlyContinue |
    ForEach-Object { if ($_.Name -match '^g\+\+-(\d+)$') { [pscustomobject]@{ Path = $_.FullName; Major = [int]$Matches[1] } } } |
    Where-Object { $_ -and ($defMajor -eq 0 -or $_.Major -lt $defMajor) } | Sort-Object Major -Descending
  if ($cands) { return (@($cands)[0]).Path }
  return $null
}

function Get-SystemRamGB {
  # OS-aware physical RAM @{ TotalGB; FreeGB }, or $null on failure. Windows uses CIM (byte-identical
  # to the pre-NC helper); Linux parses /proc/meminfo (MemTotal / MemAvailable, kB -> GB).
  if ((Get-BobOS) -eq 'windows') {
    try {
      $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
      return @{
        TotalGB = [int][math]::Round($os.TotalVisibleMemorySize / 1MB)
        FreeGB  = [int][math]::Round($os.FreePhysicalMemory   / 1MB)
      }
    } catch { return $null }
  }
  try {
    $mi = @{}
    foreach ($line in (Get-Content -LiteralPath '/proc/meminfo' -ErrorAction Stop)) {
      if ($line -match '^(MemTotal|MemAvailable):\s+(\d+)\s*kB') { $mi[$Matches[1]] = [long]$Matches[2] }
    }
    if (-not $mi.MemTotal) { return $null }
    return @{
      TotalGB = [int][math]::Round($mi.MemTotal / 1MB)      # kB / 1024^2 = GB
      FreeGB  = [int][math]::Round(($mi.MemAvailable ?? 0) / 1MB)
    }
  } catch { return $null }
}

function Stop-ProcessTree {
  # Kill a process and its direct children. Windows reaps children via CIM (as _models.ps1 did);
  # Linux uses pkill -P then kill. Best-effort — a dead PID is not an error.
  param([Parameter(Mandatory)][int]$ProcessId, [string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue |
      ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Get-Process -Id $ProcessId -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
  } else {
    & pkill -P $ProcessId 2>$null   # children first
    & kill $ProcessId    2>$null    # then the parent
  }
}

# --- notifications -------------------------------------------------------------------------------

function Send-Notification {
  # Best-effort desktop notification (mirrors osenv.notify). Windows: WinRT toast via bob-toast.ps1;
  # Linux: notify-send if present, else no-op. Returns $true if a backend fired.
  param([string]$Title = 'Bob', [string]$Body = '')
  if ((Get-BobOS) -eq 'windows') {
    try {
      . (Join-Path $PSScriptRoot 'bob-toast.ps1')   # WinRT accelerators live only in this file
      Send-BobToast -Title $Title -Body $Body
      return $true
    } catch { return $false }
  }
  if (Get-Command notify-send -ErrorAction SilentlyContinue) {
    try { & notify-send $Title $Body 2>$null; return $true } catch { return $false }
  }
  return $false
}

# --- package install -----------------------------------------------------------------------------

function Resolve-PackageCmd {
  # PURE. The install command spec for the OS (and, on Linux, the detected package manager). Callers
  # in setup/install-prereqs pass -Manager in tests; the executor auto-detects.
  param([Parameter(Mandatory)][string]$Package, [string]$Os = (Get-BobOS), [string]$Manager)
  if ($Os -eq 'windows') {
    return @{ Exe = 'winget'; Args = @('install', $Package,
      '--accept-package-agreements', '--accept-source-agreements', '--disable-interactivity'); Sudo = $false }
  }
  switch ($Manager) {
    'apt'    { return @{ Exe = 'apt-get'; Args = @('install', '-y', $Package); Sudo = $true } }
    'dnf'    { return @{ Exe = 'dnf';     Args = @('install', '-y', $Package); Sudo = $true } }
    'pacman' { return @{ Exe = 'pacman';  Args = @('-S', '--noconfirm', $Package); Sudo = $true } }
    default  { return @{ Exe = $null; Args = @(); Sudo = $false; Manager = $Manager } }
  }
}

function Get-LinuxPackageManager {
  foreach ($m in 'apt-get', 'dnf', 'pacman') {
    if (Get-Command $m -ErrorAction SilentlyContinue) {
      return @{ 'apt-get' = 'apt'; 'dnf' = 'dnf'; 'pacman' = 'pacman' }[$m]
    }
  }
  return $null
}

function Install-Package {
  # EXECUTOR. Windows: winget (tolerates the already-installed exit code, like _common.ps1
  # Install-WithWinget). Linux: detected apt/dnf/pacman via sudo.
  param([Parameter(Mandatory)][string]$Package, [string[]]$ExtraArgs = @())
  $os = Get-BobOS
  $mgr = if ($os -eq 'windows') { $null } else { Get-LinuxPackageManager }
  if ($os -ne 'windows' -and -not $mgr) { throw "Install-Package: no supported package manager (apt/dnf/pacman) found for '$Package'." }
  $spec = Resolve-PackageCmd -Package $Package -Os $os -Manager $mgr
  if ($spec.Sudo -and (Get-Command sudo -ErrorAction SilentlyContinue)) {
    & sudo $spec.Exe @($spec.Args) @ExtraArgs
  } else {
    & $spec.Exe @($spec.Args) @ExtraArgs
  }
  # -1978335189 = APPINSTALLER_CLI_ERROR_PACKAGE_ALREADY_INSTALLED (winget); treat as success.
  if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
    throw "Install-Package '$Package' failed (exit $LASTEXITCODE)"
  }
}

# --- background service launch (NC4) -------------------------------------------------------------

function Resolve-BgLaunchSpec {
  # PURE. How to launch a background pwsh process. Windows: hidden window. Linux: nohup wrapper, no
  # -WindowStyle (that param is invalid off-Windows and would throw).
  param([Parameter(Mandatory)][string[]]$PwshArgs, [string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') { return @{ Exe = 'pwsh';  Args = $PwshArgs;              Hidden = $true } }
  return @{ Exe = 'nohup'; Args = (@('pwsh') + $PwshArgs); Hidden = $false }
}

function Start-BobBackgroundProcess {
  # EXECUTOR. Launch a detached pwsh process and record its PID. Windows branch is byte-identical to
  # the old `Start-Process pwsh -WindowStyle Hidden -PassThru` + pidfile in up.ps1/start-*.ps1.
  param([Parameter(Mandatory)][string[]]$ArgList, [Parameter(Mandatory)][string]$PidFile)
  $spec = Resolve-BgLaunchSpec -PwshArgs $ArgList
  $newPid = if ($spec.Hidden) {
    (Start-Process $spec.Exe -ArgumentList $spec.Args -WindowStyle Hidden -PassThru).Id
  } else {
    (Start-Process $spec.Exe -ArgumentList $spec.Args -PassThru).Id
  }
  $newPid | Set-Content $PidFile -Encoding utf8
  return $newPid
}

# --- agent task scheduler (NC4) ------------------------------------------------------------------
# Windows = a "BobAgent" Scheduled Task (wraps today's cmdlets); Linux = a single tagged crontab line
# running bob-agent.ps1 every minute. The runner (bob-agent.ps1) + Test-CronDue already do the
# cron-expression evaluation, so the OS task only has to fire it once a minute — identical on both.

function Test-CrontabAvailable {
  # The Linux agent scheduler (NC4) shells out to a `crontab` binary, which minimal installs lack
  # (e.g. CachyOS ships no cron by default). A missing command is a terminating CommandNotFound that
  # `2>$null` does NOT swallow, so callers must guard: status/remove no-op, register throws a clear
  # error. Returns $true when crontab is on PATH.
  return [bool](Get-Command crontab -ErrorAction SilentlyContinue)
}

function Get-AgentTaskSpec {
  # PURE. The registration spec for the OS. Windows 'argument' is byte-identical to bob.ps1:1073.
  param(
    [Parameter(Mandatory)][string]$ScriptPath,
    [string]$TaskName = 'BobAgent',
    [string]$PwshPath = 'pwsh',
    [string]$Os = (Get-BobOS)
  )
  if ($Os -eq 'windows') {
    return @{
      Kind = 'schtasks'; Name = $TaskName; Execute = 'pwsh.exe'
      Argument = "-WindowStyle Hidden -NonInteractive -File `"$ScriptPath`""
      IntervalMinutes = 1; TimeLimitMinutes = 5
    }
  }
  # -WindowStyle is invalid on Linux; the trailing "# <TaskName>" tag is the removal key.
  return @{
    Kind = 'cron'; Name = $TaskName
    Crontab = "* * * * * $PwshPath -NonInteractive -File `"$ScriptPath`" # $TaskName"
  }
}

function Register-AgentTask {
  param([Parameter(Mandatory)][string]$ScriptPath, [string]$TaskName = 'BobAgent')
  $spec = Get-AgentTaskSpec -ScriptPath $ScriptPath -TaskName $TaskName
  if ($spec.Kind -eq 'schtasks') {
    $a = New-ScheduledTaskAction -Execute $spec.Execute -Argument $spec.Argument
    $t = New-ScheduledTaskTrigger -RepetitionInterval (New-TimeSpan -Minutes $spec.IntervalMinutes) -Once -At (Get-Date)
    $g = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes $spec.TimeLimitMinutes) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $spec.Name -Action $a -Trigger $t -Settings $g -RunLevel Limited -Force | Out-Null
  } else {
    if (-not (Test-CrontabAvailable)) {
      throw "cron not found — the Linux agent scheduler needs a 'crontab' binary. Install it (Arch: 'sudo pacman -S cronie' + 'sudo systemctl enable --now cronie'; Debian/Ubuntu: 'sudo apt-get install -y cron'), then re-run 'bob agent install'."
    }
    $existing = @(& crontab -l 2>$null) | Where-Object { $_ -notmatch "# $($spec.Name)$" }   # idempotent
    (@($existing) + $spec.Crontab | Where-Object { $_ -ne '' }) -join "`n" | & crontab -
  }
}

function Unregister-AgentTask {
  param([string]$TaskName = 'BobAgent')
  if ((Get-BobOS) -eq 'windows') {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  } else {
    if (-not (Test-CrontabAvailable)) { return }   # no cron installed -> nothing scheduled to remove
    (@(& crontab -l 2>$null) | Where-Object { $_ -notmatch "# $TaskName$" }) -join "`n" | & crontab -
  }
}

function Get-AgentTaskStatus {
  # Returns @{ Registered; State; NextRun }. Windows reads the scheduled task; Linux inspects crontab.
  param([string]$TaskName = 'BobAgent')
  if ((Get-BobOS) -eq 'windows') {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) { return @{ Registered = $false; State = $null; NextRun = $null } }
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
    return @{ Registered = $true; State = "$($task.State)"; NextRun = $info.NextRunTime }
  }
  if (-not (Test-CrontabAvailable)) { return @{ Registered = $false; State = $null; NextRun = $null } }
  $line = @(& crontab -l 2>$null) | Where-Object { $_ -match "# $TaskName$" } | Select-Object -First 1
  return @{ Registered = [bool]$line; State = ($line ? 'Ready' : $null); NextRun = $null }
}

# --- small path / exe portability helpers --------------------------------------------------------

function Get-BobExeName {
  # Executable file name: 'llama-server' -> 'llama-server.exe' on Windows, bare on Linux.
  param([Parameter(Mandatory)][string]$Base, [string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') { return "$Base.exe" }
  return $Base
}

function Get-CurlExe {
  # 'curl.exe' on Windows (the built-in from Win10 1803+), 'curl' elsewhere.
  param([string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') { return 'curl.exe' }
  return 'curl'
}

function Get-VenvExe {
  # Absolute path to a console script inside a repo venv. Windows: tools\<venv>\Scripts\<exe>.exe;
  # Linux/macOS: tools/<venv>/bin/<exe> (that's where `python -m venv` puts scripts).
  param([Parameter(Mandatory)][string]$Venv, [Parameter(Mandatory)][string]$Exe, [string]$Os = (Get-BobOS))
  $toolsVenv = Join-Path (Join-Path $script:PlatRepo 'tools') $Venv
  if ($Os -eq 'windows') { return (Join-Path (Join-Path $toolsVenv 'Scripts') "$Exe.exe") }
  return (Join-Path (Join-Path $toolsVenv 'bin') $Exe)
}

function Get-BinExe {
  # Absolute path to a native binary staged in repo bin/ (adds .exe on Windows).
  param([Parameter(Mandatory)][string]$Base, [string]$Os = (Get-BobOS))
  return (Join-Path (Join-Path $script:PlatRepo 'bin') (Get-BobExeName $Base -Os $Os))
}

function Get-HomeConfigDir {
  # Per-app config dir: %USERPROFILE%\.config\<app> on Windows; $XDG_CONFIG_HOME/<app> (or
  # ~/.config/<app>) on Linux.
  param([Parameter(Mandatory)][string]$App, [string]$Os = (Get-BobOS))
  if ($Os -eq 'windows') { return (Join-Path $env:USERPROFILE (Join-Path '.config' $App)) }
  $base = if ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME } else { Join-Path $HOME '.config' }
  return (Join-Path $base $App)
}

# --- build flags (NC3 / NC8) ---------------------------------------------------------------------

function Resolve-BuildCmakeFlags {
  # PURE. The cmake generator / CUDA toggle / DLL-staging decision for a build. CPU (-Cpu) => CUDA
  # off, no DLL staging, both OSes. GPU build => CUDA on; Windows uses the VS generator + stages
  # CUDA runtime DLLs, Linux uses Ninja and resolves .so via rpath/ldconfig (no staging).
  param([switch]$Cpu, [int]$Arch = 0, [string]$Os = (Get-BobOS))
  $gen = if ($Os -eq 'windows') { 'Visual Studio 17 2022' } else { 'Ninja' }
  if ($Cpu) { return @{ Cuda = $false; Generator = $gen; StageDlls = $false } }
  return @{ Cuda = $true; Generator = $gen; StageDlls = ($Os -eq 'windows') }
}

# --- build-output rollback (ND3, generalizes the Module-B .bak swap) ------------------------------
# `bob update` snapshots the whole build-output dir before rebuilding so a failed upgrade can be rolled
# back atomically (the working binaries never vanish until the new build is verified). Cross-platform:
# operates on whatever dir it's handed (repo bin/), no .exe assumptions. Copy for the snapshot so the
# rebuild overwrites the live dir in place; restore or discard the copy based on the verify result.

function Backup-BuildOutput {
  # Snapshot <Path> to <Path>.bak (clearing any stale .bak first). Returns the .bak path, or $null if
  # <Path> doesn't exist (nothing to protect — a fresh build).
  param([Parameter(Mandatory)][string]$Path)
  $bak = "$Path.bak"
  if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Recurse -Force }
  if (-not (Test-Path -LiteralPath $Path)) { return $null }
  Copy-Item -LiteralPath $Path -Destination $bak -Recurse -Force
  return $bak
}

function Restore-BuildOutput {
  # Roll <Path> back to the snapshot taken by Backup-BuildOutput. Returns $true if a restore happened.
  param([Parameter(Mandatory)][string]$Path, [string]$BakPath)
  if (-not $BakPath) { $BakPath = "$Path.bak" }
  if (-not (Test-Path -LiteralPath $BakPath)) { return $false }
  if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
  Move-Item -LiteralPath $BakPath -Destination $Path -Force
  return $true
}

function Remove-BuildOutputBackup {
  # Discard the snapshot after a verified-successful update.
  param([Parameter(Mandatory)][string]$Path, [string]$BakPath)
  if (-not $BakPath) { $BakPath = "$Path.bak" }
  if (Test-Path -LiteralPath $BakPath) { Remove-Item -LiteralPath $BakPath -Recurse -Force }
}
