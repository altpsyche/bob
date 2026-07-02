#requires -Version 7
# Download the GGUF models for the active profile (config/models.psd1) into models/.
# Single source of truth: config/models.psd1 (see also scripts/gen-llama-swap.ps1).
# Uses curl.exe (built into Windows) with resume (-C -). Re-run any time — existing files skipped.
# Public repos need no token; for gated repos set $env:HF_TOKEN and it'll be sent as a bearer header.
#   .\scripts\fetch-models.ps1                 # active profile
#   .\scripts\fetch-models.ps1 -Profile 12gb   # a specific profile (does not persist)
#   .\scripts\fetch-models.ps1 -ListOnly       # dry-run: list files + sizes, download nothing
param([string]$Profile, [switch]$ListOnly)
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\_models.ps1"

function Update-Manifest {
    # Record the (already-computed) SHA256 for a downloaded model. Atomic write (CONTRIBUTING §5) —
    # models/manifest.json is read concurrently by `bob show`, diagnose.ps1 and the ND1 doctor check.
    param([string]$ModelsDir, [string]$Gguf, [string]$Url, [double]$SizeGB, [Parameter(Mandatory)][string]$Sha)
    $manifestPath = Join-Path $ModelsDir 'manifest.json'
    $manifest = if (Test-Path $manifestPath) {
        Get-Content $manifestPath -Raw | ConvertFrom-Json -AsHashtable
    } else { @{} }
    $manifest[$Gguf] = @{ sha256=$Sha; sizeGB=$SizeGB; url=$Url; verifiedAt=(Get-Date -Format 'o') }
    $tmp = "$manifestPath.$PID.tmp"
    $manifest | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $tmp -Encoding utf8
    Move-Item -LiteralPath $tmp -Destination $manifestPath -Force
    Write-Host "  SHA256: $($Sha.Substring(0,16))... -> models/manifest.json" -ForegroundColor DarkGray
}

function Confirm-Download {
    # ND1 verify-on-install: hash the freshly-downloaded file, compare to the versions.lock pin.
    #   pinned + mismatch -> delete the bad file and THROW (loud-fail, CONTRIBUTING error convention);
    #   pinned + match    -> ok;  unpinned (sha256 null) -> capture TOFU + warn to run `bob lock`.
    # Returns the computed lowercase hash so the caller records it without re-hashing a multi-GB file.
    param([string]$File, [string]$Gguf, [hashtable]$Lock)
    Write-Host "  Computing SHA256 for $Gguf (large files ~15s)..."
    $sha = (Get-FileHash -LiteralPath $File -Algorithm SHA256).Hash.ToLower()
    $expected = if ($Lock -and $Lock.models.Contains($Gguf)) { "$($Lock.models[$Gguf].sha256)".ToLower() } else { '' }
    if ($expected) {
        if ($sha -ne $expected) {
            Remove-Item -LiteralPath $File -Force -ErrorAction SilentlyContinue
            throw "Checksum mismatch for $Gguf — versions.lock pins $expected but the download is $sha. Deleted the bad file. (ND1 verify-on-install)"
        }
        Write-Host "  verified against versions.lock" -ForegroundColor DarkGray
    } else {
        Write-Warning "$Gguf is not pinned in versions.lock (sha256 null) — recording the downloaded hash (TOFU). Run 'bob lock' to pin it."
    }
    return $sha
}

function Get-ModelRevision {
    # The pinned HuggingFace revision for a gguf from versions.lock; 'main' when unpinned/absent.
    param([string]$Gguf, [hashtable]$Lock)
    if ($Lock -and $Lock.models.Contains($Gguf) -and $Lock.models[$Gguf].revision) { return $Lock.models[$Gguf].revision }
    return 'main'
}

$repo   = Split-Path $PSScriptRoot -Parent
$outDir = Join-Path $repo "models"

$resolved = Get-Models -Profile $Profile
$name     = $resolved.profile
$models   = $resolved.models
$totalGB  = ($models | Measure-Object -Property sizeGB -Sum).Sum

Write-Host "Profile '$name': $($models.Count) models, ~$([math]::Round($totalGB,1)) GB total" -ForegroundColor Cyan

if ($ListOnly) {
  $rows = [System.Collections.Generic.List[pscustomobject]]::new()
  foreach ($m in $models) {
    $dest    = Join-Path $outDir $m.gguf
    $present = if (Test-Path $dest) { "present" } else { "MISSING" }
    $rows.Add([pscustomobject]@{ model = $m.role; file = $m.gguf; GB = $m.sizeGB; status = $present; from = "$($m.repo)/$($m.path)" })
    if ($m.mmproj) {
      $mDest    = Join-Path $outDir $m.mmproj
      $mPresent = if (Test-Path $mDest) { "present" } else { "MISSING" }
      $rows.Add([pscustomobject]@{ model = "$($m.role)/mmproj"; file = $m.mmproj; GB = '~0.6'; status = $mPresent; from = "$($m.repo)/$($m.mmproj)" })
    }
  }
  $rows | Format-Table -AutoSize
  Write-Host "(dry run — nothing downloaded)" -ForegroundColor DarkGray
  return
}

$curl = Get-CurlExe   # NC5: curl.exe on Windows (built-in from Win10 1803+), curl elsewhere
if (-not (Get-Command $curl -ErrorAction SilentlyContinue)) { throw "$curl not found (install curl, or on Windows needs Win10 1803+)." }
# ND1 — install FROM the lock, not 'latest': pin the HF revision and verify the checksum on download.
# Best-effort: if the lock is missing we fall back to 'main' + TOFU (with a warning in Confirm-Download).
$lock = try { Get-VersionsLock } catch { $null }
$hdr = @()
if ($env:HF_TOKEN) { $hdr = @("-H", "Authorization: Bearer $env:HF_TOKEN") }

# disk-space pre-check (advisory only — never fatal, and cross-platform). `Split-Path -Qualifier` is
# Windows-only: a Linux path (/home/...) has no drive qualifier and it throws, so branch by OS and
# swallow any provider quirk.
$missing  = @($models | Where-Object { -not (Test-Path (Join-Path $outDir $_.gguf)) })
$neededGB = ($missing | Measure-Object -Property sizeGB -Sum).Sum
if ($neededGB -gt 0) {
  try {
    $freeGB = $null
    if ($IsWindows) {
      $drive = Split-Path $outDir -Qualifier   # 'C:' — string-only parse, works before dir exists
      $drv   = Get-PSDrive ($drive -replace ':','') -ErrorAction SilentlyContinue
      if ($drv) { $freeGB = $drv.Free / 1GB }
    } else {
      # Linux/macOS: pick the FileSystem PSDrive whose root prefixes $outDir (usually '/').
      $drv = Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
             Where-Object { $_.Root -and $outDir.StartsWith($_.Root) } |
             Sort-Object { $_.Root.Length } -Descending | Select-Object -First 1
      if ($drv -and $null -ne $drv.Free) { $freeGB = $drv.Free / 1GB }
    }
    if ($null -ne $freeGB -and $freeGB -lt $neededGB * 1.2) {
      Write-Warning ("Low disk space: {0:N1} GB free, ~{1:N1} GB needed (+20% buffer = {2:N1} GB)" -f `
        $freeGB, $neededGB, ($neededGB * 1.2))
    }
  } catch { }   # the pre-check is advisory; a resolution quirk must not fail the fetch
}
$staleParts = @(Get-ChildItem $outDir -Filter '*.gguf.part' -ErrorAction SilentlyContinue)
if ($staleParts.Count -gt 0) {
  Write-Warning "$($staleParts.Count) stale .part file(s) in $outDir — curl will resume them."
}

$fail = 0
foreach ($m in $models) {
  $dest = Join-Path $outDir $m.gguf
  if (Test-Path $dest) { Write-Host "exists  $($m.gguf)" -ForegroundColor DarkGray }
  else {
    $rev = Get-ModelRevision -Gguf $m.gguf -Lock $lock
    $url = "https://huggingface.co/$($m.repo)/resolve/$rev/$($m.path)"
    Write-Host "fetch   $($m.gguf)  <-  $($m.repo) / $($m.path) @ $rev" -ForegroundColor Cyan
    $dlSw = [Diagnostics.Stopwatch]::StartNew()
    & $curl -L -C - --fail-with-body --progress-bar @hdr -o "$dest.part" $url
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "FAILED $url  (verify repo/filename on huggingface.co)"; $fail++; continue
    }
    Move-Item "$dest.part" $dest -Force
    $sha = Confirm-Download -File $dest -Gguf $m.gguf -Lock $lock   # throws + deletes on a pinned mismatch
    Update-Manifest -ModelsDir $outDir -Gguf $m.gguf -Url $url -SizeGB $m.sizeGB -Sha $sha
    Write-Host "done    $($m.gguf)  ($($m.sizeGB) GB in $([int]$dlSw.Elapsed.TotalMinutes)m$($dlSw.Elapsed.Seconds)s)" -ForegroundColor Green
  }

  # Download mmproj (multimodal projector) when present — same repo, different file.
  if ($m.mmproj) {
    $mmprojDest = Join-Path $outDir $m.mmproj
    if (Test-Path $mmprojDest) { Write-Host "exists  $($m.mmproj)" -ForegroundColor DarkGray }
    else {
      $rev = Get-ModelRevision -Gguf $m.gguf -Lock $lock   # mmproj rides the model's pinned revision
      $mmprojUrl = "https://huggingface.co/$($m.repo)/resolve/$rev/$($m.mmproj)"
      Write-Host "fetch   $($m.mmproj)  <-  $($m.repo) / $($m.mmproj) @ $rev" -ForegroundColor Cyan
      $dlSw2 = [Diagnostics.Stopwatch]::StartNew()
      & $curl -L -C - --fail-with-body --progress-bar @hdr -o "$mmprojDest.part" $mmprojUrl
      if ($LASTEXITCODE -ne 0) {
        Write-Warning "FAILED $mmprojUrl  (verify mmproj filename on huggingface.co)"; $fail++
      } else {
        Move-Item "$mmprojDest.part" $mmprojDest -Force
        Write-Host "done    $($m.mmproj) in $([int]$dlSw2.Elapsed.TotalMinutes)m$($dlSw2.Elapsed.Seconds)s" -ForegroundColor Green
      }
    }
  }
}

Write-Host "`nModels in $outDir :" -ForegroundColor Green
Get-ChildItem $outDir -Filter *.gguf | Select-Object Name, @{n='GB';e={[math]::Round($_.Length/1GB,1)}} | Format-Table -AutoSize
if ($fail) { Write-Warning "$fail model(s) failed — fix config\models.psd1 and re-run." }
