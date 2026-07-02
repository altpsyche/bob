# Module B — Automation Hardening

**Priority:** Implement after Module A. All sub-items are independent of each other.

## Overview

Six targeted fixes to make setup, builds, and maintenance more robust:

| Sub | Name | Risk addressed |
|-----|------|----------------|
| B1 | Shared port-conflict fn | `up.ps1` starts on occupied port silently |
| B2 | URL verifier | Stale HF URLs cause confusing download failures |
| B3 | Disk-space pre-check | Large downloads fail mid-way with no warning |
| B4 | Atomic build + rollback | Failed rebuild clobbers working binaries |
| B5 | Pip failure fatal | Broken venv silently continues setup |
| B6 | winget exit codes | CUDA install failure goes unnoticed |

---

## B1 — Shared Port-Conflict Detection

### Problem
`scripts/start.ps1` has a TCP port-in-use check (inline code, not a function).
`scripts/up.ps1` launches on port 8080 without checking — if `start.ps1` was already
running, the second launch silently fails or produces a confusing bind error.

### Change: extract to `scripts/_models.ps1`

Add after the existing shared functions:

```powershell
function Test-PortInUse {
    param([int]$Port, [string]$Host = '127.0.0.1')
    try {
        $t = [System.Net.Sockets.TcpClient]::new($Host, $Port)
        $t.Close()
        $true
    } catch {
        $false
    }
}
```

**`scripts/start.ps1`** — replace the inline check (current lines ~14-17) with:

```powershell
if (Test-PortInUse -Port $port) {
    Write-Warning "Port $port already in use. Endpoint may already be running."
    Write-Host "  Check: bob status"
    Write-Host "  Stop:  bob stop"
    return
}
```

**`scripts/up.ps1`** — add at the top of the launch sequence (after loading `_models.ps1`):

```powershell
if (Test-PortInUse -Port $port) {
    Write-Warning "Port $port already in use — skipping endpoint launch (already running)."
} else {
    Start-Process powershell -ArgumentList "-NoProfile -File `"$PSScriptRoot\start.ps1`""
}
```

---

## B2 — URL Verifier Script

### Problem
`config/models.psd1` line 36 has a comment: "verified live on 2026-06-24". This timestamp
rots. When a HuggingFace repo renames a file (common with model updates), users hit a 404
mid-download with no clear indication of which URL is stale.

### New file: `scripts/verify-urls.ps1`

```powershell
#Requires -Version 7.0
<#
.SYNOPSIS
    Checks that all model download URLs in config/models.psd1 return HTTP 200.
.DESCRIPTION
    Sends a HEAD request per model per profile. Reports OK / REDIRECT / GATED / MISSING / ERROR.
    Exits non-zero if any URL is MISSING or ERROR.
    Respects $env:HF_TOKEN for gated repos (returns 401 without token).
.PARAMETER Profile
    Check only this profile (e.g. '16gb'). Default: all profiles.
.EXAMPLE
    bob verify-urls
    bob verify-urls -Profile 16gb
    $env:HF_TOKEN = 'hf_...' ; bob verify-urls
#>
param([string]$Profile)

. "$PSScriptRoot\_models.ps1"
$cfg = Get-ModelsConfig
$HfBase = 'https://huggingface.co'
$headers = @{ 'User-Agent' = 'bob-url-check/1.0' }
if ($env:HF_TOKEN) { $headers['Authorization'] = "Bearer $env:HF_TOKEN" }

$profiles = if ($Profile) { @($Profile) } else { $cfg.profiles.Keys }
$issues = 0
$results = [System.Collections.Generic.List[pscustomobject]]::new()

foreach ($pname in ($profiles | Sort-Object)) {
    $models = Get-Models -Profile $pname
    foreach ($m in $models.models) {
        if (-not $m.repo -or -not $m.path) { continue }
        # Handle whisper-style models that use a direct path
        $url = if ($m.path -match '^https?://') { $m.path }
               else { "$HfBase/$($m.repo)/resolve/main/$($m.path)" }

        $status = 'UNKNOWN'
        $code   = 0
        try {
            $resp = Invoke-WebRequest -Uri $url -Method Head -Headers $headers `
                    -MaximumRedirection 0 -SkipHttpErrorCheck -TimeoutSec 15
            $code = $resp.StatusCode
            $status = switch ($code) {
                200     { 'OK' }
                { $_ -in 301,302,307,308 } { 'REDIRECT' }
                401     { 'GATED' }
                403     { 'FORBIDDEN' }
                404     { 'MISSING'; $issues++ }
                default { "HTTP$code"; $issues++ }
            }
        } catch {
            $status = 'ERROR'
            $code   = -1
            $issues++
        }

        # Also check mmproj if present (vision models)
        if ($m.mmproj -and $m.mmproj.repo -and $m.mmproj.path) {
            $mpUrl = "$HfBase/$($m.mmproj.repo)/resolve/main/$($m.mmproj.path)"
            try {
                $mpResp = Invoke-WebRequest -Uri $mpUrl -Method Head -Headers $headers `
                          -MaximumRedirection 0 -SkipHttpErrorCheck -TimeoutSec 15
                $mpCode = $mpResp.StatusCode
                $mpStatus = if ($mpCode -eq 200) { 'OK' }
                            elseif ($mpCode -eq 401) { 'GATED' }
                            elseif ($mpCode -eq 404) { 'MISSING'; $issues++ }
                            else { "HTTP$mpCode"; $issues++ }
            } catch { $mpStatus = 'ERROR'; $issues++ }
            $results.Add([pscustomobject]@{
                Profile = $pname; Role = "$($m.role)/mmproj"; Status = $mpStatus; URL = $mpUrl
            })
        }

        $results.Add([pscustomobject]@{
            Profile = $pname; Role = $m.role; Status = $status; URL = $url
        })
    }
}

# Print table
$pad = ($results.Role | Measure-Object -Maximum -Property Length).Maximum
Write-Host "`nURL Verification Results`n$('-' * 70)"
foreach ($r in $results) {
    $color = switch ($r.Status) {
        'OK'       { 'Green' }
        'REDIRECT' { 'Yellow' }
        'GATED'    { 'Cyan' }
        default    { 'Red' }
    }
    Write-Host ("{0,-8} {1,-$($pad+2)} " -f $r.Profile, $r.Role) -NoNewline
    Write-Host $r.Status -ForegroundColor $color
    if ($r.Status -notin 'OK','REDIRECT') {
        Write-Host "         → $($r.URL)" -ForegroundColor DarkGray
    }
}
Write-Host ""

if ($issues -gt 0) {
    Write-Host "$issues URL(s) need attention. Update repo/path in config/models.psd1." -ForegroundColor Red
    exit 1
} else {
    Write-Host "All URLs verified OK." -ForegroundColor Green
}
```

### Add to `scripts/llm.ps1`

In the switch statement, add:

```powershell
'verify-urls' { & "$PSScriptRoot\verify-urls.ps1" @rest }
```

And in the help text:

```
  bob verify-urls [-Profile <name>]  Check all HuggingFace download URLs for 200 OK
```

### Status meanings

| Status | Meaning | Action |
|--------|---------|--------|
| OK | URL returns 200 | None |
| REDIRECT | 3xx — URL moved | Follow redirect manually; update path in PSD1 |
| GATED | 401 — requires HF_TOKEN | Set `$env:HF_TOKEN` and retry |
| FORBIDDEN | 403 — access denied | Repo may be private or requires agreement |
| MISSING | 404 — file not found | File was renamed/removed on HF; update PSD1 |
| ERROR | Network/timeout | Retry; check internet |

---

## B3 — Disk-Space Pre-Check in `fetch-models.ps1`

### Problem
No check before starting 38 GB of downloads. Mid-download failure leaves `.part` files
and gives a confusing curl error rather than "you're out of disk space".

### Change: add to `scripts/fetch-models.ps1`

Add after the model list is built (before the download loop):

```powershell
# Disk space pre-check
$missing = $models | Where-Object { -not (Test-Path (Join-Path $modelsDir $_.gguf)) }
if ($missing) {
    $neededGB = ($missing | Measure-Object -Property sizeGB -Sum).Sum
    $drive    = Split-Path -Qualifier $modelsDir
    try {
        $freeGB = (Get-PSDrive ($drive.TrimEnd(':'))).Free / 1GB
        if ($freeGB -lt $neededGB * 1.2) {
            Write-Warning ("Disk space low: need {0:F1} GB (with 20% margin), have {1:F1} GB free on {2}" `
                -f ($neededGB * 1.2), $freeGB, $drive)
            Write-Warning "Downloads will proceed but may fail mid-file. Free up space first."
        } else {
            Write-Host ("Disk OK: need {0:F1} GB, have {1:F1} GB free" -f $neededGB, $freeGB)
        }
    } catch {
        # Drive query failed (network drive, etc.) — skip silently
    }
}
```

Also check for stale `.part` files and warn:

```powershell
$staleParts = Get-ChildItem $modelsDir -Filter '*.part' -ErrorAction SilentlyContinue
if ($staleParts) {
    Write-Warning "$($staleParts.Count) stale .part file(s) found in models/:"
    $staleParts | ForEach-Object { Write-Warning "  $($_.Name) ($([math]::Round($_.Length/1GB, 2)) GB)" }
    Write-Warning "These will be resumed. Delete them manually if the source URL changed."
}
```

---

## B4 — Atomic Build with Rollback in `build-llama.ps1`

### Problem
If `build-llama.ps1` fails after partially overwriting `bin/`, the existing working
`llama-server.exe` may be corrupted. Re-running skips the build (exe exists) — user
is stuck with a broken binary until they find and use `-Force`.

### Change: stage to temp dir, atomic move on success

Replace the section that copies binaries to `bin/` (currently near end of script):

```powershell
# BEFORE: copies directly to bin/
# Copy-Item "build/bin/llama-server.exe" "$repo/bin/" -Force

# AFTER: atomic with rollback
$tmpDir = Join-Path $repo 'bin\_build_tmp'
$binDir = Join-Path $repo 'bin'
if (Test-Path $tmpDir) { Remove-Item $tmpDir -Recurse -Force }
New-Item -ItemType Directory -Path $tmpDir | Out-Null

# Build outputs to $tmpDir
cmake --build build --config Release --target llama-server llama-bench llama-swap 2>&1
if ($LASTEXITCODE -ne 0) {
    Remove-Item $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    throw "Build failed. Previous bin/ is untouched. Use -Force to clean and retry."
}

# Copy build outputs into tmpDir
$buildBin = Join-Path $repo 'external\llama.cpp\build\bin\Release'
Get-ChildItem $buildBin -Filter '*.exe' | Copy-Item -Destination $tmpDir
# Copy CUDA DLLs
$cudaDlls = @('cublas64_12.dll','cublasLt64_12.dll','cudart64_12.dll')
foreach ($dll in $cudaDlls) {
    $src = Join-Path $cudaRoot "bin\$dll"
    if (Test-Path $src) { Copy-Item $src $tmpDir }
}

# Backup existing server binary (keep one .bak)
$serverExe = Join-Path $binDir 'llama-server.exe'
if (Test-Path $serverExe) {
    Copy-Item $serverExe (Join-Path $binDir 'llama-server.exe.bak') -Force
}

# Atomic move: replace bin/ contents
Get-ChildItem $tmpDir | Move-Item -Destination $binDir -Force
Remove-Item $tmpDir -Force

Write-Host "Build complete. Previous binary backed up as bin/llama-server.exe.bak"
Write-Host "Rollback if needed: Copy-Item bin/llama-server.exe.bak bin/llama-server.exe"
```

---

## B5 — Pip Failure Fatal in `bootstrap.ps1`

### Problem
Current `bootstrap.ps1` (~line 83) uses `Write-Warning` after pip install failure.
Setup continues with a broken venv, then fails later with a confusing import error.

### Change: make pip failure throw

Locate the pip install invocations (there are two: webui and aider venvs).
Pattern currently: `pip install ... ; if ($LASTEXITCODE -ne 0) { Write-Warning "..." }`

Replace with:

```powershell
# For webui venv:
& "$repo\tools\venv-webui\Scripts\python.exe" -m pip install -r $webuiReqs --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Open WebUI pip install failed. Check internet connection and try again: .\setup.bat"
}

# For aider venv:
& "$repo\tools\venv-aider\Scripts\python.exe" -m pip install -r $aiderReqs --quiet
if ($LASTEXITCODE -ne 0) {
    throw "aider pip install failed. Check internet connection and try again: .\setup.bat"
}
```

Also add a pre-flight check that the venv Python is usable:

```powershell
# After venv creation, before pip install:
& "$repo\tools\venv-webui\Scripts\python.exe" --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "venv-webui Python is not executable. Delete tools/venv-webui and re-run setup."
}
```

---

## B6 — winget Exit Code Checks in `setup.ps1`

### Problem
`scripts/setup.ps1` calls `winget install` for CUDA Toolkit without checking `$LASTEXITCODE`.
If winget fails (no internet, already installed with different version, policy restriction),
setup continues silently and the build fails later with a cryptic CUDA path error.

### Change: wrap each winget call

```powershell
function Install-WithWinget {
    param([string]$PackageId, [string]$Version, [string]$Description)
    Write-Host "Installing $Description via winget..."
    if ($Version) {
        winget install --id $PackageId --version $Version --accept-source-agreements --accept-package-agreements -e
    } else {
        winget install --id $PackageId --accept-source-agreements --accept-package-agreements -e
    }
    if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne -1978335189) {
        # -1978335189 = APPINSTALLER_ERROR_ALREADY_INSTALLED (winget exit code for "already installed")
        Write-Warning "winget install failed for $Description (exit $LASTEXITCODE)."
        Write-Warning "Install manually and re-run: .\setup.bat"
        Write-Warning "Download: https://developer.nvidia.com/cuda-downloads"
    }
}

# Usage:
Install-WithWinget 'Nvidia.CUDA' '12.8' 'CUDA Toolkit 12.8'
```

**Note on exit code -1978335189:** winget returns this specific code when the package is already
installed at the requested version. This is not a failure — treat as success.

---

## Shared: Size Tolerance Constant

Currently `0.10` (±10%) appears verbatim in three files. Extract to `_models.ps1`:

```powershell
# Add near top of _models.ps1, after $script:ModelsFile definition:
$script:SizeTolPct = 0.10   # Acceptable ± fraction for model file size validation
```

Then in each consuming script, replace `0.90` / `1.10` / `0.10` literals with
`(1 - $SizeTolPct)` / `(1 + $SizeTolPct)` / `$SizeTolPct` respectively.

Files to update: `scripts/fetch-models.ps1`, `scripts/diagnose.ps1`, `scripts/test-dry-run.ps1`.

---

## Verification

```powershell
# B1: port conflict
bob serve        # Start endpoint
bob serve        # Second time — should print warning, not error, return gracefully
bob stop

# B2: URL verifier
bob verify-urls  # All should be OK
# Test MISSING detection:
# Temporarily change one path in models.psd1 to a bad value, re-run, should show RED MISSING

# B3: disk space (manual)
# Set $neededGB to a huge number in fetch-models.ps1 temporarily and run bob fetch --list
# Should print warning about disk space

# B4: atomic build
# Corrupt build by stopping it mid-way (Ctrl+C during build-llama.ps1)
# Verify bin/llama-server.exe still works (original preserved)
# Verify build-llama.ps1 -Force cleanly retries

# B5: pip failure
# Temporarily break pip by installing an impossible requirement, verify setup fails with throw

# B6: winget (hard to test in isolation; verify the function handles code -1978335189 as success)
.\scripts\test-dry-run.ps1  # All 10 groups should still pass
```

## Files Modified

| File | Change |
|------|--------|
| `scripts/_models.ps1` | Add `Test-PortInUse`, `$SizeTolPct` |
| `scripts/start.ps1` | Use `Test-PortInUse` |
| `scripts/up.ps1` | Use `Test-PortInUse` |
| `scripts/verify-urls.ps1` | New file |
| `scripts/llm.ps1` | Add `verify-urls` command |
| `scripts/fetch-models.ps1` | Disk-space check, stale-part warning, use `$SizeTolPct` |
| `scripts/build-llama.ps1` | Atomic build, .bak preservation |
| `scripts/bootstrap.ps1` | Pip failure throws |
| `scripts/setup.ps1` | winget exit code checks |
| `scripts/diagnose.ps1` | Use `$SizeTolPct` |
| `scripts/test-dry-run.ps1` | Use `$SizeTolPct` |
| `.gitignore` | Add `config/user.psd1`, `models/manifest.json`, `logs/` |

## Estimated Effort

- B1: 30 min
- B2: 2 hours (PowerShell HTTP + table formatting)
- B3: 30 min
- B4: 1.5 hours (test failure scenarios carefully)
- B5: 15 min
- B6: 30 min
- Shared constant: 30 min

Total: ~5.5 hours
