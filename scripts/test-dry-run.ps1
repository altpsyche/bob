#requires -Version 7
# Dry-run validation of GPU detection logic, CUDA selection, profile suggestions, and model lists.
# Nothing is downloaded, installed, or sent over the network. Temporary directories are created
# to simulate different CUDA toolkit installations and removed when the script exits.
#
#   .\scripts\test-dry-run.ps1            # show failures only
#   .\scripts\test-dry-run.ps1 -Verbose   # show every test result
param([switch]$Verbose)

$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"

$pass = 0; $fail = 0

function Assert {
  param([string]$Label, [bool]$Ok, [string]$Got = '', [string]$Expected = '')
  if ($Ok) {
    $script:pass++
    if ($Verbose) { Write-Host "  pass  $Label" -ForegroundColor DarkGreen }
  } else {
    $script:fail++
    Write-Host "  FAIL  $Label" -ForegroundColor Red
    if ($Got)      { Write-Host "        got:      $Got"      -ForegroundColor DarkRed }
    if ($Expected) { Write-Host "        expected: $Expected" -ForegroundColor DarkRed }
  }
}

# --------------------------------------------------------------------------
Write-Host "`n[1] compute_cap -> arch + generation mapping" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Tests the conversion logic from Get-GpuArch without requiring nvidia-smi.

function ConvertCap($cap) {
  $arch = [int]($cap -replace '\.', '')
  $gen = switch ($arch) {
    { $_ -ge 120 }                 { 'Blackwell';    break }
    { $_ -ge 89 -and $_ -lt 120 } { 'Ada Lovelace'; break }
    { $_ -ge 80 -and $_ -lt 89 }  { 'Ampere';       break }
    { $_ -ge 75 -and $_ -lt 80 }  { 'Turing';       break }
    default                        { "sm_$arch" }
  }
  return @{ arch = $arch; gen = $gen }
}

@(
  @{ cap = '12.0'; arch = 120; gen = 'Blackwell'    }   # RTX 5080
  @{ cap = '8.9';  arch = 89;  gen = 'Ada Lovelace' }   # RTX 4090 / 4080 / 4070 etc.
  @{ cap = '8.6';  arch = 86;  gen = 'Ampere'       }   # RTX 3090 / 3080 / 3070 / 3060
  @{ cap = '8.0';  arch = 80;  gen = 'Ampere'       }   # A100
  @{ cap = '7.5';  arch = 75;  gen = 'Turing'       }   # RTX 2080
  @{ cap = '6.1';  arch = 61;  gen = 'sm_61'        }   # GTX 1080 (no special label)
) | ForEach-Object {
  $r = ConvertCap $_.cap
  Assert "cap $($_.cap)  -> arch $($_.arch) ($($_.gen))" `
    ($r.arch -eq $_.arch -and $r.gen -eq $_.gen) `
    "arch=$($r.arch) gen=$($r.gen)" "arch=$($_.arch) gen=$($_.gen)"
}

# --------------------------------------------------------------------------
Write-Host "`n[2] CUDA toolkit selection across GPU generations" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Mirrors Get-BestCudaRoot logic but against temporary directories so no real
# CUDA installation is needed. Cleaned up in the finally block.

function Select-CudaRoot([string]$Base, [int]$CudaArch) {
  if (-not (Test-Path $Base)) { return $null }
  if ($CudaArch -ge 120) {
    $p = Join-Path $Base 'v12.8'
    if (Test-Path $p) { return $p }
    return $null
  }
  $minMajor = if ($CudaArch -ge 75) { 11 } else { 10 }
  $installed = Get-ChildItem $Base -Directory | ForEach-Object {
    if ($_.Name -match '^v(\d+)\.(\d+)$') {
      [pscustomobject]@{ Path = $_.FullName; Major = [int]$Matches[1]; Minor = [int]$Matches[2] }
    }
  } | Where-Object { $_ -and $_.Major -ge $minMajor } | Sort-Object Major, Minor -Descending
  if ($installed) { return $installed[0].Path }
  return $null
}

$tmp = Join-Path $env:TEMP "llm-test-$([int](Get-Random))"
try {
  # Scenario A: only CUDA 12.8 installed
  $a = Join-Path $tmp 'A'; New-Item -ItemType Directory -Force (Join-Path $a 'v12.8') | Out-Null
  Assert "Blackwell + only 12.8  -> picks 12.8"   ((Select-CudaRoot $a 120) -eq (Join-Path $a 'v12.8'))
  Assert "Ada (89) + only 12.8   -> picks 12.8"   ((Select-CudaRoot $a 89)  -eq (Join-Path $a 'v12.8'))
  Assert "Ampere (86) + only 12.8 -> picks 12.8"  ((Select-CudaRoot $a 86)  -eq (Join-Path $a 'v12.8'))

  # Scenario B: only CUDA 12.1 (not 12.8)
  $b = Join-Path $tmp 'B'; New-Item -ItemType Directory -Force (Join-Path $b 'v12.1') | Out-Null
  Assert "Blackwell + only 12.1  -> null (needs 12.8 exactly)" ($null -eq (Select-CudaRoot $b 120))
  Assert "Ada (89) + only 12.1   -> picks 12.1"  ((Select-CudaRoot $b 89) -eq (Join-Path $b 'v12.1'))
  Assert "Ampere (86) + only 12.1 -> picks 12.1" ((Select-CudaRoot $b 86) -eq (Join-Path $b 'v12.1'))

  # Scenario C: CUDA 11.8 and 12.1 both installed
  $c = Join-Path $tmp 'C'
  New-Item -ItemType Directory -Force (Join-Path $c 'v11.8') | Out-Null
  New-Item -ItemType Directory -Force (Join-Path $c 'v12.1') | Out-Null
  Assert "Ada (89) + 11.8 and 12.1   -> picks 12.1 (newer)"  ((Select-CudaRoot $c 89) -eq (Join-Path $c 'v12.1'))
  Assert "Ampere (86) + 11.8 and 12.1 -> picks 12.1 (newer)" ((Select-CudaRoot $c 86) -eq (Join-Path $c 'v12.1'))

  # Scenario D: only CUDA 11.8 (Ada/Ampere fallback, but not Blackwell)
  $d = Join-Path $tmp 'D'; New-Item -ItemType Directory -Force (Join-Path $d 'v11.8') | Out-Null
  Assert "Ada (89) + only 11.8    -> picks 11.8"                   ((Select-CudaRoot $d 89)  -eq (Join-Path $d 'v11.8'))
  Assert "Ampere (86) + only 11.8 -> picks 11.8"                   ((Select-CudaRoot $d 86)  -eq (Join-Path $d 'v11.8'))
  Assert "Blackwell + only 11.8   -> null (11.8 not enough)"       ($null -eq (Select-CudaRoot $d 120))

  # Scenario E: CUDA 12.8 and 12.1 both installed
  $e = Join-Path $tmp 'E'
  New-Item -ItemType Directory -Force (Join-Path $e 'v12.8') | Out-Null
  New-Item -ItemType Directory -Force (Join-Path $e 'v12.1') | Out-Null
  Assert "Blackwell + 12.8 and 12.1 -> picks 12.8 (exact match)"  ((Select-CudaRoot $e 120) -eq (Join-Path $e 'v12.8'))
  Assert "Ada (89) + 12.8 and 12.1  -> picks 12.8 (newest 12.x)"  ((Select-CudaRoot $e 89)  -eq (Join-Path $e 'v12.8'))

  # Scenario F: nothing installed
  $f = Join-Path $tmp 'F'; New-Item -ItemType Directory -Force $f | Out-Null
  Assert "No CUDA installed -> null for Blackwell" ($null -eq (Select-CudaRoot $f 120))
  Assert "No CUDA installed -> null for Ada"       ($null -eq (Select-CudaRoot $f 89))
  Assert "No CUDA installed -> null for Ampere"    ($null -eq (Select-CudaRoot $f 86))

} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------
Write-Host "`n[3] CUDA_PATH env var tag derivation" -ForegroundColor Cyan
# --------------------------------------------------------------------------
@(
  @{ path = 'C:\CUDA\v12.8'; tag = 'CUDA_PATH_V12_8' }
  @{ path = 'C:\CUDA\v12.1'; tag = 'CUDA_PATH_V12_1' }
  @{ path = 'C:\CUDA\v11.8'; tag = 'CUDA_PATH_V11_8' }
) | ForEach-Object {
  $derived = 'CUDA_PATH_V' + ((Split-Path $_.path -Leaf) -replace '^v', '' -replace '\.', '_')
  Assert "$($_.path) -> $($_.tag)" ($derived -eq $_.tag) $derived $_.tag
}

# --------------------------------------------------------------------------
Write-Host "`n[4] DLL name derivation per CUDA major version" -ForegroundColor Cyan
# --------------------------------------------------------------------------
@(
  @{ path = 'C:\CUDA\v12.8'; major = '12' }
  @{ path = 'C:\CUDA\v12.1'; major = '12' }
  @{ path = 'C:\CUDA\v11.8'; major = '11' }
) | ForEach-Object {
  $leaf  = Split-Path $_.path -Leaf
  $major = if ($leaf -match '^v(\d+)') { $Matches[1] } else { '12' }
  $dlls  = @("cublas64_$major.dll", "cublasLt64_$major.dll", "cudart64_$major.dll")
  Assert "$leaf -> major $($_.major), DLLs use _$($_.major).dll suffix" `
    ($major -eq $_.major) $major $_.major
  Assert "$leaf -> three DLLs named correctly" `
    ($dlls.Count -eq 3 -and $dlls[0] -eq "cublas64_$major.dll")
}

# --------------------------------------------------------------------------
Write-Host "`n[5] Profile suggestion for various VRAM amounts" -ForegroundColor Cyan
# --------------------------------------------------------------------------
@(
  @{ vram = 6;  expected = '8gb'  }   # below smallest profile -> suggests smallest
  @{ vram = 8;  expected = '8gb'  }
  @{ vram = 10; expected = '8gb'  }   # 10GB fits 8gb but not 12gb
  @{ vram = 12; expected = '12gb' }
  @{ vram = 14; expected = '12gb' }   # 14GB fits 12gb but not 16gb
  @{ vram = 16; expected = '16gb' }
  @{ vram = 20; expected = '16gb' }   # 20GB fits 16gb but not 24gb
  @{ vram = 24; expected = '24gb' }   # 24gb profile now exists
  @{ vram = 28; expected = '24gb' }   # 28GB fits 24gb but not 32gb
  @{ vram = 32; expected = '32gb' }   # 32gb profile now exists
  @{ vram = 48; expected = '32gb' }   # larger than any profile -> biggest available
) | ForEach-Object {
  $sug = Get-SuggestedProfile -VramGB $_.vram
  Assert "$($_.vram) GB -> suggests '$($_.expected)'" ($sug -eq $_.expected) $sug $_.expected
}

# --------------------------------------------------------------------------
Write-Host "`n[6] models.psd1 structure — all profiles, all roles, all required fields" -ForegroundColor Cyan
# --------------------------------------------------------------------------
$cfg           = Get-ModelsConfig
$requiredRoles = @('planner', 'coder', 'chat', 'fim', 'embed')
$requiredKeys  = @('repo', 'path', 'gguf', 'sizeGB')

foreach ($pname in ($cfg.profiles.Keys | Sort-Object)) {
  $prof = $cfg.profiles[$pname]

  # NC8 — the CPU tier is a deliberate minimal profile (chat + agent only); it does not carry the
  # sized-profile role set. Validated separately below.
  if ($prof._cpuTier) { continue }

  foreach ($role in $requiredRoles) {
    $m = $prof[$role]
    Assert "[$pname] role '$role' exists" ($null -ne $m)
    if (-not $m) { continue }

    foreach ($key in $requiredKeys) {
      Assert "[$pname][$role] has '$key'" ($m.ContainsKey($key) -and "$($m[$key])" -ne '')
    }

    if ($role -ne 'embed') {
      Assert "[$pname][$role] ctx > 0" ($m.ContainsKey('ctx') -and [int]$m['ctx'] -gt 0) `
        "$($m['ctx'])" "> 0"
    }

    # gguf: lowercase, no spaces, ends in .gguf
    Assert "[$pname][$role] gguf filename looks valid" `
      ($m['gguf'] -match '^[a-z0-9._-]+\.gguf$') $m['gguf']

    # repo: 'owner/repo-name' with no spaces
    Assert "[$pname][$role] HF repo format valid" `
      ($m['repo'] -match '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$') $m['repo']

    # sizeGB: positive number
    Assert "[$pname][$role] sizeGB > 0" ([float]$m['sizeGB'] -gt 0) "$($m['sizeGB'])"
  }

  # pinned models must have ttl = 0
  foreach ($role in @('fim', 'embed')) {
    $m = $prof[$role]
    if ($m) {
      Assert "[$pname][$role] ttl = 0 (pinned)" ($m.ContainsKey('ttl') -and $m['ttl'] -eq 0)
      Assert "[$pname][$role] pinned = true"    ($m.ContainsKey('pinned') -and $m['pinned'] -eq $true)
    }
  }
}

# NC8 — CPU tier: dedicated minimal validation (chat + agent, one tiny shared GGUF). Not pinned in
# llama-swap terms (they're swap-group members), so no ttl/pinned assertions here.
$cpu = $cfg.profiles['cpu']
if ($cpu) {
  foreach ($role in @('chat', 'agent')) {
    $m = $cpu[$role]
    Assert "[cpu] role '$role' exists" ($null -ne $m)
    if (-not $m) { continue }
    foreach ($key in $requiredKeys) {
      Assert "[cpu][$role] has '$key'" ($m.ContainsKey($key) -and "$($m[$key])" -ne '')
    }
    Assert "[cpu][$role] ctx > 0"        ($m.ContainsKey('ctx') -and [int]$m['ctx'] -gt 0) "$($m['ctx'])" "> 0"
    Assert "[cpu][$role] gguf valid"     ($m['gguf'] -match '^[a-z0-9._-]+\.gguf$') $m['gguf']
    Assert "[cpu][$role] repo valid"     ($m['repo'] -match '^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$') $m['repo']
    Assert "[cpu][$role] tiny (<= 2 GB)" ([float]$m['sizeGB'] -gt 0 -and [float]$m['sizeGB'] -le 2.0) "$($m['sizeGB'])" "0 < GB <= 2"
  }
  Assert "[cpu] chat + agent share one gguf" ($cpu.chat.gguf -eq $cpu.agent.gguf)
}

# --------------------------------------------------------------------------
Write-Host "`n[7] Model list dry-run for all profiles (no downloads)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
foreach ($pname in ($cfg.profiles.Keys | Sort-Object)) {
  Write-Host "  profile '$pname':" -ForegroundColor DarkGray
  try {
    # Let fetch-models render its own table; only capture errors for the assert.
    & "$PSScriptRoot\fetch-models.ps1" -Profile $pname -ListOnly
    Assert "[$pname] fetch --list completes without error" $true
  } catch {
    Assert "[$pname] fetch --list completes without error" $false "$_"
  }
}

# --------------------------------------------------------------------------
Write-Host "`n[8] End-to-end setup scenarios across hardware profiles" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Simulates the full setup decision chain for 8 real-world hardware configs:
# VRAM -> profile selection, GPU arch + installed CUDA -> toolkit resolution.
# Uses temp directories — no real GPU or CUDA installation needed.

$scenarios = @(
  @{ name='RTX 5080 (Blackwell, 16GB) + CUDA 12.8';      vram=16; arch=120; cuda=@('v12.8'); expectProfile='16gb'; expectLeaf='v12.8'; cudaOk=$true  }
  @{ name='RTX 5080 (Blackwell, 16GB) + only CUDA 12.1';  vram=16; arch=120; cuda=@('v12.1'); expectProfile='16gb'; expectLeaf=$null;   cudaOk=$false }
  @{ name='RTX 4090 (Ada, 24GB) + CUDA 12.8';             vram=24; arch=89;  cuda=@('v12.8'); expectProfile='24gb'; expectLeaf='v12.8'; cudaOk=$true  }
  @{ name='RTX 4090 (Ada, 24GB) + only CUDA 12.1';        vram=24; arch=89;  cuda=@('v12.1'); expectProfile='24gb'; expectLeaf='v12.1'; cudaOk=$true  }
  @{ name='RTX 3070 (Ampere, 8GB) + CUDA 12.1';           vram=8;  arch=86;  cuda=@('v12.1'); expectProfile='8gb';  expectLeaf='v12.1'; cudaOk=$true  }
  @{ name='RTX 3060 12GB (Ampere) + CUDA 12.1';           vram=12; arch=86;  cuda=@('v12.1'); expectProfile='12gb'; expectLeaf='v12.1'; cudaOk=$true  }
  @{ name='RTX 3060 12GB (Ampere) + only CUDA 11.8';      vram=12; arch=86;  cuda=@('v11.8'); expectProfile='12gb'; expectLeaf='v11.8'; cudaOk=$true  }
  @{ name='RTX 4060 Ti (Ada, 8GB) + CUDA 12.1';           vram=8;  arch=89;  cuda=@('v12.1'); expectProfile='8gb';  expectLeaf='v12.1'; cudaOk=$true  }
  @{ name='RTX 4070 12GB (Ada, 12GB) + CUDA 12.1';        vram=12; arch=89;  cuda=@('v12.1'); expectProfile='12gb'; expectLeaf='v12.1'; cudaOk=$true  }
  @{ name='RTX 5080 (Blackwell, 16GB) + no CUDA at all';  vram=16; arch=120; cuda=@();        expectProfile='16gb'; expectLeaf=$null;   cudaOk=$false }
  @{ name='No GPU detected';                               vram=0;  arch=0;   cuda=@();        expectProfile=$null;  expectLeaf=$null;   cudaOk=$true  }
)

$tmp8 = Join-Path $env:TEMP "llm-test8-$([int](Get-Random))"
try {
  $si = 0
  foreach ($s in $scenarios) {
    $si++
    $base8 = Join-Path $tmp8 "s$si"
    New-Item -ItemType Directory -Force $base8 | Out-Null
    foreach ($v in $s.cuda) { New-Item -ItemType Directory -Force (Join-Path $base8 $v) | Out-Null }

    # 1. Profile selection from VRAM (skip simulated no-GPU — Get-SuggestedProfile falls back to
    #    nvidia-smi when VramGB is 0/falsy, which would read real hardware on this machine).
    if ($s.vram -gt 0) {
      $prof8 = Get-SuggestedProfile -VramGB $s.vram
      Assert "[$($s.name)] VRAM $($s.vram) GB -> profile '$($s.expectProfile)'" `
        ($prof8 -eq $s.expectProfile) $prof8 "$($s.expectProfile)"

      # 3. Chosen profile has all required roles in models.psd1
      if ($prof8) {
        $pd = $cfg.profiles[$prof8]
        foreach ($role in @('planner','coder','chat','fim','embed')) {
          Assert "[$($s.name)] profile '$prof8' has role '$role'" ($null -ne $pd[$role])
        }
      }
    }

    # 2. CUDA selection for GPU arch against the simulated temp directory
    if ($s.arch -gt 0) {
      $cudaPath8 = Select-CudaRoot $base8 $s.arch
      $leaf8     = if ($cudaPath8) { Split-Path $cudaPath8 -Leaf } else { $null }
      Assert "[$($s.name)] arch $($s.arch) + ($($s.cuda -join ',')) -> CUDA '$($s.expectLeaf)'" `
        ($leaf8 -eq $s.expectLeaf) "$leaf8" "$($s.expectLeaf)"
      if (-not $s.cudaOk) {
        Assert "[$($s.name)] incompatible combo yields null CUDA (setup would warn user)" `
          ($null -eq $cudaPath8)
      }
    }
  }
} finally {
  Remove-Item -Recurse -Force $tmp8 -ErrorAction SilentlyContinue
}

# --------------------------------------------------------------------------
Write-Host "`n[9] Installed CUDA vs GPU compatibility (live check)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Reads real nvidia-smi and the real CUDA toolkit directory.
# Skipped entirely (no pass/fail) when no GPU is detected.

$gpuInfo9 = Get-GpuArch
if (-not $gpuInfo9) {
  Write-Host "  skip  nvidia-smi not found or no GPU detected — CUDA check skipped" -ForegroundColor DarkGray
} else {
  $arch9 = $gpuInfo9.CudaArch
  $gen9  = $gpuInfo9.Gen
  Write-Host "  GPU: $gen9 (sm_$arch9)" -ForegroundColor DarkGray

  $cudaBase9  = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA'
  $installed9 = @()
  if (Test-Path $cudaBase9) {
    $installed9 = @(Get-ChildItem $cudaBase9 -Directory | ForEach-Object {
      if ($_.Name -match '^v(\d+)\.(\d+)$') {
        [pscustomobject]@{ Name = $_.Name; Major = [int]$Matches[1]; Minor = [int]$Matches[2] }
      }
    } | Where-Object { $_ })
  }

  Assert "[live] CUDA toolkit directory exists"       (Test-Path $cudaBase9)
  Assert "[live] At least one CUDA version installed" ($installed9.Count -gt 0) `
    "$($installed9.Count) found" '>= 1'

  $nameList9 = if ($installed9.Count -gt 0) { ($installed9 | ForEach-Object { $_.Name }) -join ', ' } else { 'none' }
  if ($arch9 -ge 120) {
    $has128 = $installed9 | Where-Object { $_.Major -eq 12 -and $_.Minor -eq 8 }
    Assert "[live] Blackwell GPU requires CUDA 12.8 — installed" ($null -ne $has128) $nameList9 'v12.8'
    $extra9 = @($installed9 | Where-Object { -not ($_.Major -eq 12 -and $_.Minor -eq 8) })
    if ($extra9.Count -gt 0) {
      Write-Host "  warn  Extra CUDA versions (unused on Blackwell): $(($extra9 | ForEach-Object { $_.Name }) -join ', ')" -ForegroundColor Yellow
    }
  } else {
    $has12x = $installed9 | Where-Object { $_.Major -eq 12 }
    Assert "[live] $gen9 GPU — CUDA 12.x installed" ($null -ne $has12x) $nameList9 'any v12.x'
  }

  $bestRoot9 = Get-BestCudaRoot -CudaArch $arch9
  $bestRoot9Diag = if ($bestRoot9) { $bestRoot9 } else { 'null (no compatible toolkit found)' }
  Assert "[live] Get-BestCudaRoot resolves a usable toolkit for sm_$arch9" ($null -ne $bestRoot9) $bestRoot9Diag
}

# --------------------------------------------------------------------------
Write-Host "`n[10] Downloaded model file validation (active profile, live check)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Checks files in models/ against models.psd1 for the active profile.
# Skipped entirely when no files are present (partial downloads are fine — only
# present files are validated; absent ones emit a -Verbose note, not a failure).

$cfg10   = Get-ModelsConfig
$pname10 = Resolve-ProfileName -Config $cfg10
$prof10  = $cfg10.profiles[$pname10]
$mdir    = Join-Path $repo 'models'
Write-Host "  profile: $pname10  |  models dir: $mdir" -ForegroundColor DarkGray

$anyPresent10 = $false
foreach ($role in @('planner','coder','chat','fim','embed')) {
  $m10 = $prof10[$role]; if (-not $m10) { continue }
  if (Test-Path (Join-Path $mdir $m10['gguf'])) { $anyPresent10 = $true; break }
}

if (-not $anyPresent10) {
  Write-Host "  skip  No model files found in models/ for profile '$pname10' — file checks skipped" -ForegroundColor DarkGray
} else {
  foreach ($role in @('planner','coder','chat','fim','embed')) {
    $m10 = $prof10[$role]; if (-not $m10) { continue }
    $f10   = Join-Path $mdir $m10['gguf']
    $name10 = $m10['gguf']
    if (-not (Test-Path $f10)) {
      if ($Verbose) { Write-Host "  skip  [$pname10][$role] $name10 not downloaded" -ForegroundColor DarkGray }
      continue
    }
    $expGB10 = [float]$m10['sizeGB']
    $actGB10 = (Get-Item $f10).Length / 1GB
    $lo10    = [math]::Round($expGB10 * (1 - $script:SizeTolPct), 2)
    $hi10    = [math]::Round($expGB10 * (1 + $script:SizeTolPct), 2)
    Assert "[$pname10][$role] $name10 size within 10% of $($expGB10)GB" `
      ($actGB10 -ge $lo10 -and $actGB10 -le $hi10) `
      "$([math]::Round($actGB10,2))GB" "${lo10}–${hi10}GB"
    Assert "[$pname10][$role] $name10 no .part leftover" (-not (Test-Path "$f10.part"))
  }
}

# --------------------------------------------------------------------------
Write-Host "`n[11] Python unit tests (agent harness — tools, routing, context, memory, sessions, loop)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Runs the stdlib-unittest suite in tests/ (M13). Uses pytest if it's installed, else the
# stdlib runner — the tests are written to work under both. Skipped (not failed) if the venv
# is missing so a fresh checkout without venv-litellm still completes the dry-run.
$venvPy10 = Get-VenvExe -Venv 'venv-litellm' -Exe 'python'   # NC4: OS-aware (Scripts\python.exe | bin/python)
$testsDir = Join-Path $repo 'tests'
if (-not (Test-Path $venvPy10)) {
  Write-Host "  skip  venv-litellm not found — Python tests skipped (run scripts/bootstrap-litellm.ps1)" -ForegroundColor DarkGray
} elseif (-not (Test-Path $testsDir)) {
  Write-Host "  skip  tests/ dir not found" -ForegroundColor DarkGray
} else {
  & $venvPy10 -c "import pytest" 2>$null
  $hasPytest = ($LASTEXITCODE -eq 0)
  $env:PYTHONIOENCODING = 'utf-8'
  if ($hasPytest) {
    & $venvPy10 -m pytest $testsDir -q
  } else {
    & $venvPy10 -m unittest discover -s $testsDir -p 'test_*.py'
  }
  $testExit = $LASTEXITCODE
  $env:PYTHONIOENCODING = $null
  Assert "Python unit tests pass ($(if ($hasPytest) {'pytest'} else {'unittest'}))" ($testExit -eq 0) "exit code $testExit"
}

# --------------------------------------------------------------------------
Write-Host "`n[12] Static checks (py_compile + PowerShell AST parse — via scripts\check.ps1)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# The same gate the git pre-commit hook runs (N8). -NoTests skips the suite here since [11]
# already ran it. Single source of the static-check logic lives in scripts\check.ps1.
$checkPs = Join-Path $repo 'scripts\check.ps1'
if (-not (Test-Path $venvPy10)) {
  Write-Host "  skip  venv-litellm not found" -ForegroundColor DarkGray
} elseif (-not (Test-Path $checkPs)) {
  Write-Host "  skip  scripts\check.ps1 not found" -ForegroundColor DarkGray
} else {
  & $checkPs -NoTests
  Assert "Static checks pass (py_compile + PowerShell parse)" ($LASTEXITCODE -eq 0) "exit code $LASTEXITCODE"
}

# --------------------------------------------------------------------------
Write-Host "`n[13] versions.lock (ND1 — reproducibility pin)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# _versions.ps1 is dot-sourced transitively via _models.ps1 (line 12). Dry-run: parse the committed
# lock, confirm it is in sync with its sources (the gate check.ps1 runs), prove the sync gate REJECTS
# a drifted lock, and that the reproducibility check runs without throwing.
try {
  $lk = Get-VersionsLock
  Assert "versions.lock parses" ($null -ne $lk.models -and $lk.submodules.Count -ge 1) "unexpected shape"
} catch { Assert "versions.lock parses" $false "$_" }

Assert "versions.lock in sync with sources" ((Test-VersionsLockSync) -eq 0) "stale — run: bob lock"

# A deliberately-wrong lock (zeroed submodule pin) must fail the sync gate — the 'wrong pin' case.
$tmpLock = Join-Path ([System.IO.Path]::GetTempPath()) "bob-vlock-$PID.lock"
try {
  '{ "lockVersion": 1, "release": "0.0.0", "submodules": { "external/llama.cpp": "0000000000000000000000000000000000000000" }, "toolchain": {}, "requirements": {}, "models": {} }' |
    Set-Content -LiteralPath $tmpLock -Encoding utf8
  Assert "sync gate rejects a drifted lock" ((Test-VersionsLockSync -Path $tmpLock) -eq 1) "a mismatched lock should fail"
} finally { Remove-Item -LiteralPath $tmpLock -Force -ErrorAction SilentlyContinue }

$reproOk = $true
try { $null = @(Test-BobReproducibility) } catch { $reproOk = $false }
Assert "Test-BobReproducibility runs without error" $reproOk "$($Error[0])"

# --------------------------------------------------------------------------
Write-Host "`n[14] build-output rollback (ND3 — bob update .bak swap)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# Backup-/Restore-BuildOutput are the generalized Module-B .bak swap `bob update` uses to roll back a
# failed rebuild. Dry-run with a temp dir: snapshot v1, overwrite with v2 (a failed "rebuild"), restore.
$tmpBin = Join-Path ([System.IO.Path]::GetTempPath()) "bob-bin-$PID"
try {
  New-Item -ItemType Directory -Force -Path $tmpBin | Out-Null
  Set-Content -LiteralPath (Join-Path $tmpBin 'server') -Value 'v1' -NoNewline
  $bak = Backup-BuildOutput -Path $tmpBin
  Assert "backup created (.bak exists)" ($bak -and (Test-Path $bak)) "no .bak"
  Set-Content -LiteralPath (Join-Path $tmpBin 'server') -Value 'v2-broken' -NoNewline   # simulated bad rebuild
  Assert "restore returns true" (Restore-BuildOutput -Path $tmpBin -BakPath $bak) "restore reported nothing"
  $restored = Get-Content -LiteralPath (Join-Path $tmpBin 'server') -Raw
  Assert "restore rolled content back to v1" ($restored -eq 'v1') "got '$restored'"
  Assert "restore consumed the .bak" (-not (Test-Path $bak)) ".bak still present"
  # discard path: re-snapshot then remove
  $bak2 = Backup-BuildOutput -Path $tmpBin
  Remove-BuildOutputBackup -Path $tmpBin -BakPath $bak2
  Assert "Remove-BuildOutputBackup clears the .bak" (-not (Test-Path $bak2)) ".bak lingered"
  # backing up a nonexistent dir is a no-op (fresh build) -> $null
  Assert "backup of absent path returns null" ($null -eq (Backup-BuildOutput -Path (Join-Path $tmpBin 'nope'))) "expected null"
} finally { Remove-Item -LiteralPath $tmpBin -Recurse -Force -ErrorAction SilentlyContinue; Remove-Item -LiteralPath "$tmpBin.bak" -Recurse -Force -ErrorAction SilentlyContinue }

# --------------------------------------------------------------------------
Write-Host "`n[15] Generators single-source their derived values (REPRO-DEBT items 3,4,5,8)" -ForegroundColor Cyan
# --------------------------------------------------------------------------
# gen-litellm.ps1 must derive api_key/master_key from the litellmKey seam and request_timeout from
# agent.requestTimeout (no hardcoded 'sk-local' / '600'). gen-continue.ps1 must emit a portable config
# (apiKey from the seam, templated MCP paths, ${GITHUB_TOKEN}) — never a committed personal path.
# Both outputs are gitignored + regenerated, so running the generators here is safe.
$bc         = Get-BobConfig
$expectKey  = Get-Secret -Name 'litellmKey' -Default ($bc.litellmKey ?? 'sk-local')
$expectTmo  = $bc.agent.requestTimeout ?? 600
$expectPort = $bc.litellmPort ?? (Get-BobPortDefault 'litellmPort')

& "$PSScriptRoot\gen-litellm.ps1" | Out-Null
$litellmYaml = Get-Content -Raw -LiteralPath (Join-Path $repo 'config\litellm.yaml')
Assert "gen-litellm: request_timeout derives from agent.requestTimeout" `
  ($litellmYaml -match "request_timeout:\s*$expectTmo") "expected request_timeout: $expectTmo"
Assert "gen-litellm: master_key derives from litellmKey seam" `
  ($litellmYaml -match "master_key:\s*$([regex]::Escape($expectKey))\b") "expected master_key: $expectKey"
Assert "gen-litellm: local model api_key derives from litellmKey seam" `
  ($litellmYaml -match "api_key:\s*$([regex]::Escape($expectKey))\b") "expected api_key: $expectKey"

& "$PSScriptRoot\gen-continue.ps1" | Out-Null
$contYaml = Get-Content -Raw -LiteralPath (Join-Path $repo 'config\continue\config.yaml')
Assert "gen-continue: emits models from Get-Models"           ($contYaml -match '(?m)^\s*-\s*name:\s*coder\b') "no coder model"
Assert "gen-continue: apiBase uses the litellm port"          ($contYaml -match "apiBase:\s*http://localhost:$expectPort/v1") "wrong apiBase port"
Assert "gen-continue: apiKey derives from litellmKey seam"    ($contYaml -match "apiKey:\s*$([regex]::Escape($expectKey))\b") "expected apiKey: $expectKey"
Assert "gen-continue: GitHub token is templated, not inlined" ($contYaml -match '\$\{GITHUB_TOKEN\}') "expected `${GITHUB_TOKEN}` placeholder"
# The generator escapes backslashes for YAML (C:\repo -> "C:\\repo"), so match the escaped form.
$repoEsc = $repo.Replace('\', '\\')
Assert "gen-continue: MCP filesystem points at the repo root" ($contYaml.Contains($repoEsc)) "repo root not templated into MCP args"

# --------------------------------------------------------------------------
$total = $pass + $fail
$col   = if ($fail -eq 0) { 'Green' } else { 'Red' }
Write-Host "`n$pass / $total passed" -ForegroundColor $col
if ($fail -gt 0) { Write-Host "$fail test(s) failed" -ForegroundColor Red; exit 1 }
