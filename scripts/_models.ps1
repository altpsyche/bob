#requires -Version 7
# Shared model-registry helpers. Single source of truth = config/models.psd1.
# Dot-source from another script:  . "$PSScriptRoot\_models.ps1"
#
# Exposes:
#   Get-ModelsConfig                  -> raw hashtable from the PSD1
#   Get-EnabledPeers [-Config c]      -> list of enabled peer objects (with injected .name)
#   Resolve-ProfileName [-Profile n]  -> profile name (arg -> $env:BOB_PROFILE -> activeProfile)
#   Get-Models [-Profile n]           -> @{ profile; config; models } ; models = ordered role objects
#   Set-ActiveProfile -Name n         -> rewrite the activeProfile line in place (validated)
#   Get-GpuVramGB                     -> total VRAM of GPU 0 in whole GB, or $null
#   Get-SuggestedProfile [-VramGB n]  -> best-fit profile name for the detected VRAM, or $null
#   Get-GpuArch                       -> @{ CudaArch; Gen; MinCudaMajor } for GPU 0, or $null
#   Get-BestCudaRoot [-CudaArch n]    -> best installed CUDA toolkit path for the arch, or $null
#   Test-PortInUse -Port n [-Hostname h]  -> $true if port is in use
#   $script:SizeTolPct                    -> 0.10 — ±10% size tolerance for GGUF validation
#   Get-BobConfig                         -> raw hashtable from config/bob.psd1 (+ user.psd1 [bob] overrides)
#   Get-VersionsLock / Write-VersionsLock / Test-VersionsLockSync / Test-BobReproducibility (ND1,
#     via _versions.ps1 dot-sourced below — the reproducibility lock seam)

$script:ModelsRepo   = Split-Path $PSScriptRoot -Parent
# NC1 — the OS-abstraction seam (Get-BobOS, Get-Secret, Get-DataDir, Get-CudaRoot, Get-SystemRamGB,
# Stop-ProcessTree, agent-task + background-launch primitives, …). Dot-sourced here so every entry
# script that already dot-sources _models.ps1 transitively gets the seam with no per-script edit.
. "$PSScriptRoot\_platform.ps1"
$script:ModelsFile   = Join-Path $script:ModelsRepo 'config\models.psd1'
$script:BobFile      = Join-Path $script:ModelsRepo 'config\bob.psd1'
$script:DefaultsFile = Join-Path $script:ModelsRepo 'config\defaults.json'
$script:SizeTolPct   = 0.10

# NB1 (contract C2) — the neutral single source of truth for the shared constants (ports + role
# table) is config/defaults.json, read by BOTH PowerShell (here) and Python (bob_core.load_defaults).
# No more hand-mirrored dicts. -AsHashtable so .Contains()/indexing work like the old literal did.
function Get-BobDefaults {
  if (-not (Test-Path $script:DefaultsFile)) {
    throw "config/defaults.json not found: $script:DefaultsFile (neutral source of truth for ports + roles, NB1)"
  }
  return Get-Content -Raw -LiteralPath $script:DefaultsFile | ConvertFrom-Json -AsHashtable
}

# Single source of truth for service-port defaults (M6/NB1), loaded from config/defaults.json.
# Read via Get-BobPortDefault; never re-inline a port number elsewhere.
$script:BobDefaults     = Get-BobDefaults
$script:BobPortDefaults = $script:BobDefaults.ports

function Get-BobPortDefault {
  # Return the single-source default for a service port (M6). Throws on an unknown key so a
  # typo fails loudly at author time rather than silently resolving to $null.
  param([Parameter(Mandatory)][string]$Name)
  if (-not $script:BobPortDefaults.Contains($Name)) {
    throw "Get-BobPortDefault: unknown port key '$Name'. Known: $($script:BobPortDefaults.Keys -join ', ')"
  }
  return $script:BobPortDefaults[$Name]
}

function Get-ModelsConfig {
  if (-not (Test-Path $script:ModelsFile)) { throw "models config not found: $script:ModelsFile" }
  $base = Import-PowerShellDataFile -LiteralPath $script:ModelsFile
  $userFile = Join-Path (Split-Path $script:ModelsFile) 'user.psd1'
  if (Test-Path $userFile) {
    $user = Import-PowerShellDataFile -LiteralPath $userFile
    if ($user.defaults) {
      if (-not $base.defaults) { $base.defaults = @{} }
      foreach ($k in $user.defaults.Keys) { $base.defaults[$k] = $user.defaults[$k] }
    }
    if ($user.profiles) {
      foreach ($profName in $user.profiles.Keys) {
        if (-not $base.profiles.Contains($profName)) {
          Write-Warning "user.psd1: profile '$profName' not in models.psd1 — skipped"; continue
        }
        foreach ($roleName in $user.profiles[$profName].Keys) {
          if (-not $base.profiles[$profName].Contains($roleName)) {
            Write-Warning "user.psd1: role '$roleName' in profile '$profName' not found — skipped"; continue
          }
          foreach ($key in $user.profiles[$profName][$roleName].Keys) {
            $base.profiles[$profName][$roleName][$key] = $user.profiles[$profName][$roleName][$key]
          }
        }
      }
    }
    if ($user.prompts) {
      if (-not $base.prompts) { $base.prompts = @{} }
      foreach ($role in $user.prompts.Keys) { $base.prompts[$role] = $user.prompts[$role] }
    }
    if ($user.peers) {
      if (-not $base.peers) { $base.peers = @{} }
      foreach ($peerName in $user.peers.Keys) {
        if (-not $base.peers.Contains($peerName)) {
          $base.peers[$peerName] = $user.peers[$peerName]
          continue
        }
        foreach ($k in $user.peers[$peerName].Keys) {
          if ($k -eq 'pro') {
            if (-not $base.peers[$peerName].pro) { $base.peers[$peerName].pro = @{} }
            foreach ($role in $user.peers[$peerName].pro.Keys) {
              $base.peers[$peerName].pro[$role] = $user.peers[$peerName].pro[$role]
            }
          } else {
            $base.peers[$peerName][$k] = $user.peers[$peerName][$k]
          }
        }
      }
    }
  }
  return $base
}

function Merge-BobHashtable {
  # NB7 (Option A) — deep-merge $Over into a clone of $Base (dict-into-dict; scalars/arrays replace).
  # Mirrors the Python `_deep_merge` in bob_config.py so both languages resolve the runtime layer
  # identically from config/defaults.json.runtime + the psd1 overlay.
  param([System.Collections.IDictionary]$Base, [System.Collections.IDictionary]$Over)
  $out = @{}
  foreach ($k in $Base.Keys) { $out[$k] = $Base[$k] }
  foreach ($k in $Over.Keys) {
    if (($Over[$k] -is [System.Collections.IDictionary]) -and ($out[$k] -is [System.Collections.IDictionary])) {
      $out[$k] = Merge-BobHashtable -Base $out[$k] -Over $Over[$k]
    } else {
      $out[$k] = $Over[$k]
    }
  }
  return $out
}

function Get-BobConfig {
  if (-not (Test-Path $script:BobFile)) { throw "bob config not found: $script:BobFile" }
  # NB7 (Option A, contract C2): the runtime defaults live in the neutral config/defaults.json
  # 'runtime' layer — the single default source shared with the Python resolver. bob.psd1 is now a
  # thin Windows overlay carrying only its unique keys (persona.name/style, routing, voice,
  # agent.toastAppId). Seed $base from the neutral layer, then deep-merge the psd1 overlay so both
  # OSes resolve the same runtime config (path separators come from defaults.json = forward slashes).
  $base = (Get-BobDefaults).runtime
  if (-not $base) { $base = @{} }
  $overlay = Import-PowerShellDataFile -LiteralPath $script:BobFile
  $base = Merge-BobHashtable -Base $base -Over $overlay
  $userFile = Join-Path (Split-Path $script:BobFile) 'user.psd1'
  if (Test-Path $userFile) {
    $user = Import-PowerShellDataFile -LiteralPath $userFile
    if ($user.bob) {
      foreach ($section in $user.bob.Keys) {
        if (-not $base.Contains($section)) { $base[$section] = @{} }
        foreach ($k in $user.bob[$section].Keys) { $base[$section][$k] = $user.bob[$section][$k] }
      }
    }
  }
  # Inject model defaults for unified cross-config access (ports, etc.).
  # Port fallbacks come from the one central dict (M6) — no re-inlined literals here.
  try {
    $md = (Get-ModelsConfig).defaults
    $base['litellmPort'] = [int]($md.litellmPort ?? (Get-BobPortDefault 'litellmPort'))
    $base['port']        = [int]($md.port        ?? (Get-BobPortDefault 'port'))
    $base['searxngPort'] = [int]($md.searxngPort ?? (Get-BobPortDefault 'searxngPort'))
    $base['n8nPort']     = [int]($md.n8nPort     ?? (Get-BobPortDefault 'n8nPort'))
    $base['webuiPort']   = [int]($md.webuiPort   ?? (Get-BobPortDefault 'webuiPort'))
    $base['litellmKey']  = $md.litellmKey ?? 'sk-local'
  } catch {}
  # Default allowedReadPaths to repo root if empty — avoids hardcoded paths in bob.psd1
  if ($base.agent -and -not $base.agent.allowedReadPaths) {
    $base.agent['allowedReadPaths'] = @($script:ModelsRepo)
  }
  # Export for Python tools — auto-generated, gitignored via /data/.
  # M17: only regenerate when a source (.psd1) is newer than the output — the hot paths
  #   (bob status, per-utterance voice turns, scheduled runs) then pay a cheap Test-Path +
  #   timestamp compare instead of a full re-serialize + disk write every invocation.
  # M2: write to a per-process temp file then atomically move into place, so concurrent
  #   `bob` invocations can never observe a half-written config.json.
  $jsonTmp = $null
  try {
    $jsonPath = Join-Path $script:ModelsRepo 'data\config.json'
    $jsonDir = Split-Path $jsonPath
    if (-not (Test-Path $jsonDir)) { New-Item $jsonDir -ItemType Directory -Force | Out-Null }
    # defaults.json is a real source (runtime layer incl. persona.systemPrompt) — include it so an
    # edit there marks config.json stale and regenerates (was missing → such edits went unnoticed).
    $srcFiles = @($script:ModelsFile, $script:BobFile, $userFile, $script:DefaultsFile) |
                Where-Object { $_ -and (Test-Path $_) } | Get-Item
    $srcMax = ($srcFiles | Measure-Object LastWriteTimeUtc -Maximum).Maximum
    $stale  = (-not (Test-Path $jsonPath)) -or
              ((Get-Item $jsonPath).LastWriteTimeUtc -lt $srcMax)
    if ($stale) {
      $jsonTmp = "$jsonPath.$PID.tmp"
      $base | ConvertTo-Json -Depth 10 | Set-Content $jsonTmp -Encoding UTF8
      Move-Item -LiteralPath $jsonTmp -Destination $jsonPath -Force
    }
  } catch {
    if ($jsonTmp -and (Test-Path $jsonTmp)) { Remove-Item $jsonTmp -Force -ErrorAction SilentlyContinue }
    Write-Warning "Failed to write data/config.json: $_  (Python tools may use stale config)"
  }
  return $base
}

function Get-RoleForTask {
  # M8 — single routing table for PowerShell callers. Resolve a model role from bob config
  # for a task; -Pro prefers the *-pro variant. bob.ps1 chat/vision/voice all call this so
  # adding a role means editing one place (mirrors bob_core.get_role on the Python side).
  param(
    $Config,
    [Parameter(Mandatory)][ValidateSet('chat', 'code', 'think', 'vision', 'voice')][string]$Task,
    [switch]$Pro
  )
  if (-not $Config) { $Config = Get-BobConfig }
  # NB1: task->key mapping and fallback literals come from config/defaults.json roleTable — one
  # place, shared with bob_core.get_role. Vision routing lives in its own config section.
  $table = $script:BobDefaults.roleTable
  $entry = if ($table.Contains($Task)) { $table[$Task] } else { $table['chat'] }
  $sectionName = if ($entry.Contains('section')) { $entry.section } else { 'routing' }
  $section = $Config.$sectionName
  if ($Pro) {
    return ($section.($entry.pro) ?? $section.($entry.base) ?? $entry.proFallback)
  }
  return ($section.($entry.base) ?? $entry.fallback)
}

function Assert-BobPortKeys {
  # M6 — gen-time guard: every service port must be present in the merged config after
  # injection. Catches a future config edit that drops a port key before Python trips on it.
  param($Config)
  if (-not $Config) { $Config = Get-BobConfig }
  $required = @('port', 'litellmPort', 'webuiPort', 'searxngPort', 'n8nPort')
  $missing = @($required | Where-Object { -not $Config.Contains($_) -or $null -eq $Config[$_] })
  if ($missing) { throw "bob config missing required port key(s): $($missing -join ', ')" }
}

function Stop-ServiceByPid {
  # M10 — one PID-file stop path (replaces copy-pasted read/Stop-Process/Remove-Item blocks).
  # Reaps direct children too (uvicorn workers etc.). Returns $true if a live process was killed.
  param(
    [Parameter(Mandatory)][string]$Name,
    [Parameter(Mandatory)][string]$PidFile
  )
  if (-not (Test-Path $PidFile)) { return $false }
  $stopped = $false
  $wPid = [int](Get-Content $PidFile -Raw -ErrorAction SilentlyContinue)
  if ($wPid) {
    try {
      Get-Process -Id $wPid -ErrorAction Stop | Out-Null   # verify alive before we claim a kill
      Stop-ProcessTree -ProcessId $wPid                    # NC1 seam: reaps children + parent, OS-aware
      $stopped = $true
    } catch {}
  }
  Remove-Item $PidFile -ErrorAction SilentlyContinue
  return $stopped
}

function Get-EnabledPeers {
  param($Config)
  if (-not $Config) { $Config = Get-ModelsConfig }
  if (-not $Config.peers) { return @() }
  return @(
    $Config.peers.Keys | Where-Object { $Config.peers[$_].enabled -ne $false } |
    ForEach-Object {
      $p = $Config.peers[$_].Clone()
      $p['name'] = $_
      [pscustomobject]$p
    }
  )
}

function Resolve-ProfileName {
  param([string]$Profile, $Config)
  if (-not $Config) { $Config = Get-ModelsConfig }
  $name = if     ($Profile)          { $Profile }
          elseif ($env:BOB_PROFILE)  { $env:BOB_PROFILE }
          else                       { $Config.activeProfile }
  if (-not $Config.profiles.Contains($name)) {
    throw "unknown profile '$name'. Valid: $($Config.profiles.Keys -join ', ')"
  }
  return $name
}

function Get-Models {
  # Resolve a profile and return its models as ordered role objects (roles only —
  # '_'-prefixed metadata keys are skipped). Stable order so generated output is
  # deterministic regardless of PSD1 hashtable enumeration order.
  param([string]$Profile)
  $cfg   = Get-ModelsConfig
  $name  = Resolve-ProfileName -Profile $Profile -Config $cfg
  $prof  = $cfg.profiles[$name]
  $order = @('planner', 'coder', 'chat', 'fim', 'embed')
  $roles = @($prof.Keys | Where-Object { -not $_.StartsWith('_') })

  $sorted = @()
  foreach ($r in $order) { if ($roles -contains $r) { $sorted += $r } }
  $sorted += @($roles | Where-Object { $order -notcontains $_ } | Sort-Object)

  $list = foreach ($r in $sorted) {
    $m = $prof[$r].Clone()        # shallow copy so we don't mutate the loaded config
    $m['role'] = $r
    [pscustomobject]$m
  }
  return @{ profile = $name; config = $cfg; models = @($list) }
}

function Get-GpuVramGB {
  # Total VRAM of GPU 0 in whole GB via nvidia-smi; $null if nvidia-smi is absent/unparseable.
  if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $null }
  try {
    $mib = & nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    if ("$mib".Trim() -match '^\d+$') { return [int][math]::Round([int]$mib / 1024) }
  } catch {}
  return $null
}

function Get-SuggestedProfile {
  # Largest '<N>gb' profile whose N <= detected VRAM (auto-scales as 32gb/64gb are added).
  # Falls back to the smallest sized profile if the card is below them all. $null if no GPU info.
  param([int]$VramGB)
  if (-not $VramGB) { $VramGB = Get-GpuVramGB }
  if (-not $VramGB) { return $null }
  $cfg = Get-ModelsConfig
  $sized = foreach ($p in $cfg.profiles.Keys) {
    if ($p -match '^(\d+)gb$') { [pscustomobject]@{ name = $p; gb = [int]$matches[1] } }
  }
  if (-not $sized) { return $null }
  $fit = $sized | Where-Object { $_.gb -le $VramGB } | Sort-Object gb -Descending | Select-Object -First 1
  if ($fit) { return $fit.name }
  return ($sized | Sort-Object gb | Select-Object -First 1).name
}

function Get-GpuArch {
  # Returns @{ CudaArch = int; Gen = string; MinCudaMajor = int } for GPU 0, or $null.
  # CudaArch maps directly to CMake CUDA_ARCHITECTURES (120 = Blackwell, 89 = Ada, 86 = Ampere).
  if (-not (Get-Command nvidia-smi -ErrorAction SilentlyContinue)) { return $null }
  try {
    $cap = (& nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>$null | Select-Object -First 1)
    $cap = "$cap".Trim()
    if ($cap -notmatch '^\d+\.\d+$') { return $null }
    $arch = [int]($cap -replace '\.', '')   # "8.9" -> 89, "12.0" -> 120
    $gen = switch ($arch) {
      { $_ -ge 120 }                 { 'Blackwell';    break }
      { $_ -ge 89 -and $_ -lt 120 } { 'Ada Lovelace'; break }
      { $_ -ge 80 -and $_ -lt 89 }  { 'Ampere';       break }
      { $_ -ge 75 -and $_ -lt 80 }  { 'Turing';       break }
      default                        { "sm_$arch" }
    }
    $minCudaMajor = if ($arch -ge 120) { 12 } else { 11 }
    return @{ CudaArch = $arch; Gen = $gen; MinCudaMajor = $minCudaMajor }
  } catch {}
  return $null
}

function Get-BestCudaRoot {
  # NC1 — thin forwarder to the OS-aware seam (Get-CudaRoot in _platform.ps1). Windows resolution is
  # byte-identical to the old body; Linux resolves /usr/local/cuda*. Kept as a named function so all
  # existing callers (build-llama.ps1, bootstrap.ps1, test-dry-run.ps1) work unchanged.
  param([int]$CudaArch = 0)
  return (Get-CudaRoot -CudaArch $CudaArch)
}

# Get-SystemRamGB moved to the NC1 seam (_platform.ps1) — OS-aware (CIM on Windows, /proc/meminfo on
# Linux). Callers are unchanged; the seam is dot-sourced above so the name resolves here.

function Get-NumaNodeCount {
  # Returns the number of NUMA nodes reported by Windows.
  # AM5 (7950X3D) exposes 1 node on Windows regardless of CCD count.
  try {
    $nodes = @(Get-CimInstance -ClassName Win32_NumaNode -ErrorAction Stop)
    return if ($nodes.Count -gt 0) { $nodes.Count } else { 1 }
  } catch { return 1 }
}

function Test-PortInUse {
  param([int]$Port, [string]$Hostname = '127.0.0.1')
  try {
    $c = [System.Net.Sockets.TcpClient]::new()
    $c.Connect($Hostname, $Port)
    $c.Close()
    return $true
  } catch { return $false }
}

function Test-CronDue {
  # Returns $true if a 5-field cron expression is due at $Now, given $LastRun.
  # 60-second guard prevents double-firing when the task runs multiple times per minute.
  param(
    [Parameter(Mandatory)][string]$Cron,
    [Parameter(Mandatory)][DateTime]$Now,
    [DateTime]$LastRun = [DateTime]::MinValue
  )
  if ($LastRun -ne [DateTime]::MinValue -and ($Now - $LastRun).TotalSeconds -lt 60) {
    return $false
  }
  $f = $Cron -split '\s+'
  if ($f.Count -ne 5) { Write-Warning "Test-CronDue: expected 5 fields, got $($f.Count) in '$Cron'"; return $false }
  function Test-CronField([string]$field, [int]$val) {
    if ($field -eq '*') { return $true }
    foreach ($part in $field -split ',') {
      if ($part -match '^(\d+)-(\d+)$' -and $val -ge [int]$Matches[1] -and $val -le [int]$Matches[2]) { return $true }
      elseif ($part -match '^\d+$' -and [int]$part -eq $val) { return $true }
    }
    return $false
  }
  return (Test-CronField $f[0] $Now.Minute)  -and
         (Test-CronField $f[1] $Now.Hour)    -and
         (Test-CronField $f[2] $Now.Day)     -and
         (Test-CronField $f[3] $Now.Month)   -and
         (Test-CronField $f[4] ([int]$Now.DayOfWeek))
}

function Set-ActiveProfile {
  param([Parameter(Mandatory)][string]$Name)
  $cfg = Get-ModelsConfig
  if (-not $cfg.profiles.Contains($Name)) {
    throw "unknown profile '$Name'. Valid: $($cfg.profiles.Keys -join ', ')"
  }
  if ($cfg.activeProfile -eq $Name) {
    Write-Host "activeProfile already '$Name'" -ForegroundColor DarkGray; return
  }
  $raw = Get-Content -Raw -LiteralPath $script:ModelsFile
  $new = [regex]::Replace($raw, "(?m)^(\s*activeProfile\s*=\s*)'[^']*'", "`${1}'$Name'")
  if ($new -eq $raw) { throw "no 'activeProfile = ...' line found to update in $script:ModelsFile" }
  Set-Content -LiteralPath $script:ModelsFile -Value $new -NoNewline -Encoding utf8
  Write-Host "activeProfile -> '$Name'  ($script:ModelsFile)" -ForegroundColor Green
}

# ND1 — the reproducibility-lock seam (versions.lock reader/generator/gate). Dot-sourced last so its
# functions (which call Get-ModelsConfig at runtime) resolve against the definitions above. Every
# script that already dot-sources _models.ps1 gets Get-VersionsLock / Write-VersionsLock /
# Test-VersionsLockSync / Test-BobReproducibility with no per-script edit (same pattern as _platform.ps1).
. "$PSScriptRoot\_versions.ps1"
