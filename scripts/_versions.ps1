#requires -Version 7
# ND1 (contract C2) — versions.lock: the neutral, GENERATED reproducibility lock read by BOTH
# PowerShell (here) and Python (scripts/bob/versions.py). It pins the whole stack as one coherent,
# verifiable set: submodule commits, per-venv requirements lock, minimum toolchain versions, and the
# model manifest (repo -> revision -> sha256, including the NC8 CPU-tier GGUF).
#
# GENERATED, NEVER HAND-EDITED (same discipline as config/verbs.json <- registry.py). Every value is
# derived from an existing single source, so you never keep it in sync by hand:
#   submodules   <- the git superproject gitlink (`git rev-parse HEAD:<path>`), the real pin. Read
#                   from the tree, so it works even when the submodule isn't checked out (CI core-suite).
#   models       <- config/models.psd1 (repo/path/gguf/sizeGB) UNION models/manifest.json (sha256).
#   requirements <- tools/*.lock (pip freeze of each venv).
#   release      <- the VERSION file (ND3).
# Regenerate with `bob lock`; `bob update` regenerates it after an upgrade; and `bob lock --check`
# (wired into check.ps1) fails the gate if the on-disk lock drifts from the sources — bump a submodule
# without regenerating and the gate says "run: bob lock".
#
# Dot-source AFTER _models.ps1 (needs Get-ModelsConfig + the repo root it sets).

$script:VersionsRepo   = Split-Path $PSScriptRoot -Parent
$script:VersionsFile   = Join-Path $script:VersionsRepo 'versions.lock'
$script:VersionFile    = Join-Path $script:VersionsRepo 'VERSION'
$script:ManifestFile   = Join-Path $script:VersionsRepo 'models\manifest.json'
# The submodules ND pins (all four declared in .gitmodules). external/llama-swap is included even
# though the ND doc listed three by example — it builds the llama-swap binary and must be pinned too.
$script:LockSubmodules = @('external/llama.cpp', 'external/llama-swap', 'external/whisper.cpp', 'external/fabric')
# Minimum toolchain versions (floors, not the live installed versions). Recorded so an install can
# assert "you have at least this" and so a release documents its build floor. NOTE: `cuda` is the
# GLOBAL floor (older GPUs); the Blackwell-specific floor (sm_120 needs >= 12.8, accepts 13.x) is
# enforced per-arch in _platform.ps1 (Resolve-CudaRootCandidates MinVer / Get-CudaRoot), not here.
$script:LockToolchain  = [ordered]@{ python = '3.12'; cmake = '3.24'; cuda = '12.0' }
$script:LockRequirements = [ordered]@{ 'venv-litellm' = 'tools/litellm-requirements.lock' }

function Get-BobVersion {
  # Release identity: the checked-in VERSION file (ND3). '0.0.0' if absent (pre-first-release).
  if (Test-Path $script:VersionFile) { return (Get-Content -Raw -LiteralPath $script:VersionFile).Trim() }
  return '0.0.0'
}

function Get-SubmoduleCommits {
  # The superproject gitlink commit per submodule — the real pin, read from `git rev-parse HEAD:<path>`
  # so it resolves without the submodule being checked out (the CI core-suite checks out no submodules).
  $out = [ordered]@{}
  foreach ($p in $script:LockSubmodules) {
    $sha = & git -C $script:VersionsRepo rev-parse "HEAD:$p" 2>$null
    $out[$p] = if ($LASTEXITCODE -eq 0 -and $sha) { "$sha".Trim() } else { $null }
  }
  return $out
}

function Get-LockModelManifest {
  # Union of every gguf referenced by config/models.psd1 profiles, keyed by Bob's local filename
  # (`gguf`). repo/path/revision/sizeGB come from models.psd1; sha256 from models/manifest.json when
  # the model has already been fetched, else $null (captured on first fetch — TOFU-then-lock). This is
  # NOT agent-only: it covers planner/coder/chat/fim/embed/agent across all profiles.
  $cfg = Get-ModelsConfig
  $manifest = if (Test-Path $script:ManifestFile) {
    Get-Content -Raw -LiteralPath $script:ManifestFile | ConvertFrom-Json -AsHashtable
  } else { @{} }
  # sha256 is TOFU-then-LOCK: once a model's checksum is locked it lives in versions.lock, which IS a
  # committed source. models/manifest.json is a machine-local capture buffer (gitignored, carries
  # per-fetch `verifiedAt` timestamps) — absent in CI and fresh clones. So prefer the manifest's sha
  # (a fresh re-fetch that must re-lock) but FALL BACK to the sha already in the lock; otherwise the
  # gate would demand a manifest that is never committed and go red on every machine that hasn't
  # fetched every model (ND1 reproducibility: the lock must regenerate from committed sources alone).
  $lockedModels = if (Test-Path $script:VersionsFile) {
    try { (Get-Content -Raw -LiteralPath $script:VersionsFile | ConvertFrom-Json -AsHashtable).models } catch { @{} }
  } else { @{} }
  if (-not $lockedModels) { $lockedModels = @{} }
  $models = [ordered]@{}
  foreach ($profName in ($cfg.profiles.Keys | Sort-Object)) {
    $prof = $cfg.profiles[$profName]
    foreach ($roleName in ($prof.Keys | Where-Object { -not $_.StartsWith('_') } | Sort-Object)) {
      $m = $prof[$roleName]
      if (-not $m.gguf) { continue }
      $gguf = $m.gguf
      if ($models.Contains($gguf)) { continue }   # first profile wins; the gguf is identical across profiles
      $sha = if ($manifest.Contains($gguf) -and $manifest[$gguf].sha256) {
        "$($manifest[$gguf].sha256)".ToLower()
      } elseif ($lockedModels.Contains($gguf) -and $lockedModels[$gguf].sha256) {
        "$($lockedModels[$gguf].sha256)".ToLower()
      } else { $null }
      $entry = [ordered]@{
        repo     = $m.repo
        path     = $m.path
        revision = if ($m.Contains('revision')) { $m.revision } else { 'main' }
        sha256   = $sha
        sizeGB   = $m.sizeGB
      }
      if ($m.mmproj) { $entry['mmproj'] = $m.mmproj }
      $models[$gguf] = $entry
    }
  }
  return $models
}

function New-VersionsLockObject {
  # Build the full lock object from the single sources (ordered so serialization is deterministic).
  return [ordered]@{
    lockVersion  = 1
    release      = (Get-BobVersion)
    submodules   = (Get-SubmoduleCommits)
    toolchain    = $script:LockToolchain
    requirements = $script:LockRequirements
    models       = (Get-LockModelManifest)
  }
}

function Get-VersionsLockText {
  # Canonical serialization used by BOTH the writer and the sync gate, so a clean regenerate always
  # byte-matches the on-disk file. Depth 6 covers models.<gguf>.<field>.
  return (New-VersionsLockObject | ConvertTo-Json -Depth 6)
}

function Write-VersionsLock {
  # (Re)generate versions.lock from the single sources. Atomic write (CONTRIBUTING §5).
  param([string]$Path = $script:VersionsFile)
  $text = Get-VersionsLockText
  $tmp  = "$Path.$PID.tmp"
  Set-Content -LiteralPath $tmp -Value ($text + "`n") -Encoding utf8 -NoNewline
  Move-Item -LiteralPath $tmp -Destination $Path -Force
  return $Path
}

function Get-VersionsLock {
  # Read + parse the lock. Fail loud if missing — it is generated, so the fix is `bob lock`.
  param([string]$Path = $script:VersionsFile)
  if (-not (Test-Path $Path)) { throw "versions.lock not found at $Path — run: bob lock" }
  return Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json -AsHashtable
}

function Test-VersionsLockSync {
  # ND1 gate (mirrors `python -m bob.registry --check`): is the on-disk versions.lock in sync with the
  # sources it is generated from? Returns 0 in sync, 1 stale/missing. Compares canonical text, and the
  # only writer uses that same canonical text, so an in-sync file matches exactly.
  param([string]$Path = $script:VersionsFile)
  if (-not (Test-Path $Path)) {
    Write-Host "versions.lock missing at $Path — run: bob lock" -ForegroundColor Red
    return 1
  }
  $want = (Get-VersionsLockText).Trim()
  $have = (Get-Content -Raw -LiteralPath $Path).Trim()
  if ($want -ne $have) {
    Write-Host "versions.lock is STALE (out of sync with submodules/models.json) — run: bob lock" -ForegroundColor Red
    return 1
  }
  return 0
}

function Test-BobReproducibility {
  # ND1 — installed state vs versions.lock, for `bob doctor`. Returns @( @{ label; ok; fix } ).
  # Unlike the sync gate (lock-vs-sources), this compares the lock to what is actually INSTALLED:
  #   - each submodule's CHECKED-OUT HEAD == the locked commit (detects a locally-moved submodule);
  #   - each present + pinned model's manifest sha == the locked sha (cheap; no re-hash).
  # Unpinned (sha256=null) or not-downloaded models are skipped — they are not drift.
  $results = [System.Collections.Generic.List[hashtable]]::new()
  $lock = try { Get-VersionsLock } catch { $null }
  if (-not $lock) {
    $results.Add(@{ label = 'versions.lock present'; ok = $false; fix = 'bob lock' })
    return $results
  }
  foreach ($p in $lock.submodules.Keys) {
    $want = "$($lock.submodules[$p])".Trim()
    if (-not $want) { continue }
    $full = Join-Path $script:VersionsRepo $p
    $head = if (Test-Path $full) { "$(& git -C $full rev-parse HEAD 2>$null)".Trim() } else { '' }
    $short = $want.Substring(0, [Math]::Min(8, $want.Length))
    $results.Add(@{
      label = "submodule $p @ $short"
      ok    = ($head -and $head -eq $want)
      fix   = 'git submodule update --init, or `bob lock` if the move was intentional'
    })
  }
  $manifest = if (Test-Path $script:ManifestFile) {
    Get-Content -Raw -LiteralPath $script:ManifestFile | ConvertFrom-Json -AsHashtable
  } else { @{} }
  foreach ($gguf in $lock.models.Keys) {
    $lockSha = "$($lock.models[$gguf].sha256)".ToLower()
    if (-not $lockSha) { continue }                                   # unpinned — nothing to verify
    $file = Join-Path $script:VersionsRepo "models\$gguf"
    if (-not (Test-Path $file)) { continue }                          # not downloaded here — not drift
    $haveSha = if ($manifest.Contains($gguf)) { "$($manifest[$gguf].sha256)".ToLower() } else { '' }
    $results.Add(@{
      label = "model $gguf checksum"
      ok    = ($haveSha -eq $lockSha)
      fix   = 're-fetch (bob fetch), or `bob lock` if the pin moved'
    })
  }
  return $results
}
