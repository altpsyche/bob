# Module D — CLI Feature Expansion

**Depends on:** Module A (port read from defaults). Otherwise all sub-items are independent.

All changes are confined to `scripts/llm.ps1` unless noted.

## Overview

| Sub | Command | Description |
|-----|---------|-------------|
| D1 | `bob status` | Live view: which models loaded, VRAM used |
| D2 | `bob restart` | Stop + start in one command |
| D3 | `bob logs [-n N]` | Tail the server log file |
| D4 | `bob chat` streaming | Real-time token output via SSE |
| D5 | `bob models` enhanced | Show backing model names, quant, VRAM |
| D6 | `bob up -NoOpen` | Browser auto-open; suppressible |
| D7 | `bob profile auto` | Re-detect VRAM, switch profile automatically |
| D8 | Model manifest | SHA256 computed on download, verified on reuse |
| D9 | `bob update` | Pull + rebuild llama.cpp |

---

## D1 — `bob status`

### What it shows
```
Endpoint: http://localhost:8080/v1  [running]

Role       Model                        VRAM      State
--------   --------------------------   --------  -------
planner    Qwen3-30B-A3B Q4 (17.3 GB)  17.3 GB   loaded
coder      Qwen2.5-Coder-14B Q4 (8.4)   8.4 GB   unloaded
chat       Qwen3-14B Q4 (8.4 GB)         8.4 GB   unloaded
fim        Qwen2.5-Coder-3B Q8 (3.4 GB)  3.4 GB   loaded (pinned)
embed      bge-m3 Q8 (0.6 GB)            0.6 GB   loaded (pinned)
```

### Implementation in `scripts/llm.ps1`

Add to switch:

```powershell
'status' {
    . "$PSScriptRoot\_models.ps1"
    $port = (Get-ModelsConfig).defaults.port ?? 8080
    $base = "http://localhost:$port/v1"

    # Check endpoint reachability
    try {
        $apiModels = (Invoke-RestMethod "$base/models" -TimeoutSec 3).data
    } catch {
        Write-Host "Endpoint not running. Start with: bob serve" -ForegroundColor Yellow
        return
    }

    # Build a set of loaded model ids from the API response
    $loadedIds = @{}
    foreach ($m in $apiModels) { $loadedIds[$m.id] = $true }

    # Load PSD1 for metadata
    $cfg     = Get-ModelsConfig
    $profile = Resolve-ProfileName -Config $cfg
    $models  = (Get-Models -Profile $profile).models

    Write-Host "`nEndpoint: $base  " -NoNewline
    Write-Host "[running]" -ForegroundColor Green
    Write-Host "Profile:  $profile`n"

    $header = "{0,-10} {1,-36} {2,-9} {3}" -f 'Role','Model','VRAM','State'
    Write-Host $header
    Write-Host ('-' * 70)

    foreach ($m in $models) {
        $modelLabel = "$($m.gguf -replace '\.gguf$') ($($m.sizeGB) GB)"
        $loaded     = $loadedIds.ContainsKey($m.role)
        $state      = if ($m.pinned)   { if ($loaded) { 'loaded (pinned)' } else { 'loading...' } }
                      elseif ($loaded)  { 'loaded' }
                      else              { 'unloaded' }
        $color      = if ($loaded) { 'Green' } else { 'DarkGray' }
        $vram       = if ($loaded) { "$($m.sizeGB) GB" } else { '--' }

        Write-Host ("{0,-10} {1,-36} {2,-9} " -f $m.role, $modelLabel, $vram) -NoNewline
        Write-Host $state -ForegroundColor $color
    }
    Write-Host ""
}
```

---

## D2 — `bob restart`

```powershell
'restart' {
    Write-Host "Stopping endpoint..."
    Get-Process -Name 'llama-swap','llama-server' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Milliseconds 500
    Write-Host "Starting endpoint..."
    & "$PSScriptRoot\start.ps1"
}
```

---

## D3 — `bob logs`

### `scripts/start.ps1` change

Redirect llama-swap output to a log file instead of (or in addition to) the terminal.
Modify the final launch line:

```powershell
# Create logs dir if needed
$logsDir = Join-Path $repo 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory $logsDir | Out-Null }
$logFile = Join-Path $logsDir 'llama-swap.log'

Write-Host "Logging to: $logFile"
Write-Host "Endpoint: http://localhost:$port/v1  (Ctrl+C to stop)"

# Launch and tee output to log file (append mode):
& $swap --config $config --listen "127.0.0.1:$port" 2>&1 |
    Tee-Object -FilePath $logFile -Append
```

### `scripts/llm.ps1` change

```powershell
'logs' {
    $n = if ($rest -and $rest[0] -match '^\d+$') { [int]$rest[0] } else { 50 }
    . "$PSScriptRoot\_models.ps1"
    $logFile = Join-Path (Split-Path $PSScriptRoot) 'logs\llama-swap.log'
    if (-not (Test-Path $logFile)) {
        Write-Host "No log file found at: $logFile"
        Write-Host "Start endpoint first: bob serve"
        return
    }
    Write-Host "Tailing $logFile (last $n lines, Ctrl+C to stop):`n"
    Get-Content $logFile -Wait -Tail $n
}
```

Add `logs/` to `.gitignore`.

---

## D4 — Streaming `bob chat`

### Current behavior
`Invoke-RestMethod` blocks until the full response arrives. For a 200-token response at 86 t/s,
the user waits ~2.3 seconds staring at a blank terminal.

### New implementation

Replace the `'chat'` case entirely:

```powershell
'chat' {
    if ($rest.Count -lt 2) {
        Write-Host "Usage: bob chat <model> <prompt...> [--sys <system-prompt>] [--max <N>]"
        Write-Host "       bob chat coder 'write fizzbuzz in python'"
        return
    }

    . "$PSScriptRoot\_models.ps1"
    $cfg      = Get-ModelsConfig
    $port     = $cfg.defaults.port ?? 8080
    $base     = "http://localhost:$port/v1"
    $maxTok   = $cfg.defaults.maxTokens ?? 512
    $model    = $rest[0]
    $argList  = $rest[1..$rest.Count]

    # Parse optional flags
    $sysPrompt = $null
    $i = 0
    $promptParts = @()
    while ($i -lt $argList.Count) {
        if ($argList[$i] -eq '--sys' -and $i+1 -lt $argList.Count) {
            $sysPrompt = $argList[$i+1]; $i += 2
        } elseif ($argList[$i] -eq '--max' -and $i+1 -lt $argList.Count) {
            $maxTok = [int]$argList[$i+1]; $i += 2
        } else {
            $promptParts += $argList[$i]; $i++
        }
    }
    $prompt = $promptParts -join ' '

    $messages = @()
    if ($sysPrompt) { $messages += @{ role = 'system'; content = $sysPrompt } }
    $messages += @{ role = 'user'; content = $prompt }

    $body = @{
        model      = $model
        stream     = $true
        max_tokens = $maxTok
        messages   = $messages
    } | ConvertTo-Json -Depth 5 -Compress

    # Stream via curl (PowerShell's Invoke-RestMethod can't stream SSE)
    try {
        curl.exe --no-buffer --silent `
            -X POST "$base/chat/completions" `
            -H 'Content-Type: application/json' `
            -d $body |
        ForEach-Object {
            if ($_ -match '^data: (.+)$') {
                $data = $Matches[1]
                if ($data -ne '[DONE]') {
                    try {
                        $chunk = $data | ConvertFrom-Json
                        $text  = $chunk.choices[0].delta.content
                        if ($text) { Write-Host -NoNewline $text }
                    } catch {}   # malformed chunk — skip
                }
            }
        }
        Write-Host ""  # final newline
    } catch {
        Write-Host "Chat failed: $_" -ForegroundColor Red
        Write-Host "Is the endpoint running? bob serve"
    }
}
```

---

## D5 — Model Backing Names in `bob models`

Replace the current `'models'` case:

```powershell
'models' {
    . "$PSScriptRoot\_models.ps1"
    $cfg    = Get-ModelsConfig
    $port   = $cfg.defaults.port ?? 8080
    $base   = "http://localhost:$port/v1"
    $profile = Resolve-ProfileName -Config $cfg

    # Get loaded models from API
    $loadedIds = @{}
    try {
        $apiModels = (Invoke-RestMethod "$base/models" -TimeoutSec 3).data
        foreach ($m in $apiModels) { $loadedIds[$m.id] = $true }
        $endpointUp = $true
    } catch {
        $endpointUp = $false
    }

    $models = (Get-Models -Profile $profile).models

    Write-Host "`nProfile: $profile`n"
    $fmt = "{0,-10} {1,-42} {2,-9} {3}"
    Write-Host ($fmt -f 'Role', 'Model', 'VRAM', 'State')
    Write-Host ('-' * 70)

    foreach ($m in $models) {
        # Build friendly model label from gguf filename
        $label = $m.gguf -replace '\.gguf$','' -replace '-',' ' -replace '_',' '
        $label = "$label ($($m.sizeGB) GB)"
        $state = if (-not $endpointUp)    { '(endpoint down)' }
                 elseif ($loadedIds[$m.role]) {
                     if ($m.pinned) { 'loaded, pinned' } else { 'loaded' }
                 } else { 'unloaded' }
        $color = if ($endpointUp -and $loadedIds[$m.role]) { 'Green' } else { 'DarkGray' }
        Write-Host ($fmt -f $m.role, $label, "$($m.sizeGB) GB", '') -NoNewline
        Write-Host $state -ForegroundColor $color
    }
    Write-Host ""
    if (-not $endpointUp) {
        Write-Host "Endpoint not running. State unknown. Start with: bob serve" -ForegroundColor Yellow
    }
}
```

---

## D6 — Browser Auto-Open on `bob up`

### `scripts/up.ps1` change

After launching Open WebUI (and after a brief pause to let it start):

```powershell
param([switch]$NoOpen)

# ... existing launch code ...

if (-not $NoOpen) {
    # Give WebUI a moment to bind, then open browser
    Start-Sleep -Seconds 2
    Start-Process "http://localhost:$webuiPort"
    Write-Host "Browser opened to http://localhost:$webuiPort"
}
```

---

## D7 — `bob profile auto`

```powershell
'profile' {
    $name = $rest[0]
    . "$PSScriptRoot\_models.ps1"
    $cfg = Get-ModelsConfig

    if ($name -eq 'auto' -or -not $name) {
        $vramGB = Get-GpuVramGB
        if (-not $vramGB) {
            Write-Host "Could not detect GPU VRAM. Specify profile manually: bob profile <name>"
            return
        }
        $suggested = Get-SuggestedProfile -VramGB $vramGB
        if (-not $suggested) {
            Write-Host "No profile fits detected VRAM ($vramGB GB). Profiles available:"
            $cfg.profiles.Keys | Sort-Object | ForEach-Object { Write-Host "  $_" }
            return
        }
        Write-Host "Detected $vramGB GB VRAM — switching to profile: $suggested"
        $name = $suggested
    }

    Set-ActiveProfile -Name $name
    & "$PSScriptRoot\gen-llama-swap.ps1"
    Write-Host "Active profile: $name"
    Write-Host "Models for this profile:"
    & "$PSScriptRoot\fetch-models.ps1" -ListOnly -Profile $name
}
```

---

## D8 — Model Manifest (SHA256 Integrity)

### Problem
Current integrity check uses file size ±10% — too loose to detect a partially corrupt file
or a silently replaced model on HuggingFace. SHA256 hardcoded in PSD1 is impractical
(HF doesn't expose checksums in the direct-download URL format used). Better: compute
SHA256 on first successful download and store in a local manifest.

### `models/manifest.json` schema

```json
{
  "qwen3-30b-a3b-q4.gguf": {
    "sha256": "abc123...",
    "sizeGB": 17.3,
    "url": "https://huggingface.co/Qwen/Qwen3-30B-A3B-GGUF/resolve/main/Qwen3-30B-A3B-Q4_K_M.gguf",
    "verifiedAt": "2026-06-26T12:00:00Z"
  }
}
```

Add to `.gitignore`: `models/manifest.json`

### `scripts/fetch-models.ps1` changes

After a successful download, add:

```powershell
function Update-Manifest {
    param([string]$ModelsDir, [string]$Gguf, [string]$Url, [double]$SizeGB)
    $manifestPath = Join-Path $ModelsDir 'manifest.json'
    $manifest = if (Test-Path $manifestPath) {
        Get-Content $manifestPath -Raw | ConvertFrom-Json -AsHashtable
    } else { @{} }

    Write-Host "  Computing SHA256 for $Gguf..."
    $hash = (Get-FileHash (Join-Path $ModelsDir $Gguf) -Algorithm SHA256).Hash.ToLower()

    $manifest[$Gguf] = @{
        sha256     = $hash
        sizeGB     = $SizeGB
        url        = $Url
        verifiedAt = (Get-Date -Format 'o')
    }
    $manifest | ConvertTo-Json -Depth 3 | Set-Content $manifestPath
    Write-Host "  SHA256: $($hash.Substring(0,16))... recorded in models/manifest.json"
}
```

For models already on disk (skipping download), add a verify step:

```powershell
function Test-ManifestIntegrity {
    param([string]$ModelsDir, [string]$Gguf)
    $manifestPath = Join-Path $ModelsDir 'manifest.json'
    if (-not (Test-Path $manifestPath)) { return $true }   # no manifest, skip
    $manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json -AsHashtable
    if (-not $manifest[$Gguf]) { return $true }            # not in manifest, skip

    $expected = $manifest[$Gguf].sha256
    $actual   = (Get-FileHash (Join-Path $ModelsDir $Gguf) -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $expected) {
        Write-Warning "$Gguf failed SHA256 check. Expected: $($expected.Substring(0,16))... Got: $($actual.Substring(0,16))..."
        Write-Warning "File may be corrupt. Delete it and re-run: bob fetch"
        return $false
    }
    $true
}
```

SHA256 computation on a 17 GB file takes ~15 seconds on NVMe. Only run it:
- After a fresh download (always)
- On explicit `bob diagnose` (user-initiated)
- NOT on every `bob serve` start (too slow)

### `scripts/diagnose.ps1` changes

Add manifest coverage check:

```powershell
$manifestPath = Join-Path $modelsDir 'manifest.json'
$manifest = if (Test-Path $manifestPath) {
    Get-Content $manifestPath -Raw | ConvertFrom-Json -AsHashtable
} else { @{} }

foreach ($m in $models) {
    if ($manifest[$m.gguf]) {
        Write-Host "  $($m.role): SHA256 recorded ($($manifest[$m.gguf].verifiedAt))" -ForegroundColor Green
    } else {
        Write-Host "  $($m.role): no SHA256 (run bob fetch to compute)" -ForegroundColor DarkGray
    }
}
```

---

## D9 — `bob update`

```powershell
'update' {
    . "$PSScriptRoot\_models.ps1"
    $repo = Split-Path $PSScriptRoot

    # Show current submodule commit
    $before = git -C "$repo\external\llama.cpp" rev-parse --short HEAD 2>$null
    Write-Host "llama.cpp current commit: $before"

    # Pull latest
    Write-Host "Pulling latest llama.cpp from origin/master..."
    git -C "$repo\external\llama.cpp" pull origin master
    if ($LASTEXITCODE -ne 0) {
        Write-Host "git pull failed. Check network and try again." -ForegroundColor Red
        return
    }

    $after = git -C "$repo\external\llama.cpp" rev-parse --short HEAD 2>$null
    if ($before -eq $after) {
        Write-Host "Already up to date ($after). No rebuild needed."
        return
    }

    Write-Host "Updated: $before → $after"
    Write-Host "Rebuilding llama.cpp..."
    & "$PSScriptRoot\build-llama.ps1" -Force
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Build failed. Rollback with: Copy-Item bin/llama-server.exe.bak bin/llama-server.exe" -ForegroundColor Red
        return
    }

    Write-Host "Running benchmark to verify fast path..."
    & "$PSScriptRoot\llm.ps1" bench
}
```

---

## Help Text Update

Replace the help/default case in `llm.ps1` with the full updated command list:

```
Usage: bob <command> [args]

Inference:
  bob serve                            Start API endpoint (:8080)
  bob up [-NoOpen]                     Start endpoint + Open WebUI + browser
  bob stop                             Stop endpoint (free VRAM)
  bob restart                          Stop then start endpoint
  bob status                           Show loaded models and VRAM usage
  bob logs [-n N]                      Tail server log (default: last 50 lines)

Models:
  bob models                           List models with backing names and state
  bob chat <model> <prompt>            Chat with a model (streaming)
    [--sys <system>] [--max <tokens>]
  bob describe <image>                 Describe an image (requires vision model)
  bob transcribe <file>                Transcribe audio to text (requires whisper)

Management:
  bob profiles                         List VRAM profiles with sizes
  bob profile <name|auto>              Switch profile (auto = detect from VRAM)
  bob fetch [--list] [profile]         Download models for active/specified profile
  bob verify-urls [-Profile <name>]    Check all HuggingFace download URLs
  bob update                           Pull latest llama.cpp and rebuild
  bob gen                              Regenerate config/llama-swap.yaml

Tools:
  bob aider [args]                     Start aider in current folder
  bob webui                            Launch Open WebUI only
  bob qdrant                           Launch Qdrant vector DB
  bob whisper                          Launch whisper audio transcription server
  bob bench [gguf]                     Benchmark inference speed
  bob diagnose                         System and model health check
```

---

## Verification

```powershell
# D1: status
bob serve
bob status          # should show loaded/unloaded state per model
bob stop
bob status          # should say endpoint not running

# D2: restart
bob serve
bob restart         # should stop and restart cleanly

# D3: logs
bob serve
bob logs 20         # should tail last 20 lines, follow new output
# Ctrl+C to stop

# D4: streaming chat
bob chat coder "write a python function that reverses a string"
# tokens should appear one by one, not all at once

# D4: system prompt
bob chat coder "what time is it?" --sys "You are a pirate. Respond in character."

# D5: models with backing names
bob models
# should show: coder → qwen-coder-14b-q4_k_m (8.4 GB) [loaded]

# D6: browser auto-open
bob up              # should open browser to localhost:3000
bob up -NoOpen      # should not open browser

# D7: profile auto
bob profile auto    # should detect VRAM and switch/confirm profile

# D8: manifest
bob fetch           # after download, models/manifest.json should exist
# Corrupt a file and run bob diagnose — should flag SHA256 mismatch

# D9: update
bob update          # should show commit before/after; rebuild if changed
```

## Files Modified

| File | Change |
|------|--------|
| `scripts/llm.ps1` | Add D1–D9 commands, update help |
| `scripts/start.ps1` | Tee output to logs/llama-swap.log |
| `scripts/up.ps1` | Add -NoOpen switch, browser launch |
| `scripts/fetch-models.ps1` | SHA256 manifest write on download |
| `scripts/diagnose.ps1` | SHA256 manifest coverage report |
| `.gitignore` | Add `logs/`, `models/manifest.json` |

## Estimated Effort

- D1 (status): 1 hour
- D2 (restart): 15 min
- D3 (logs): 45 min (start.ps1 tee + bob logs command)
- D4 (streaming): 1.5 hours (SSE parsing + flag handling)
- D5 (model names): 45 min
- D6 (browser open): 15 min
- D7 (profile auto): 30 min
- D8 (manifest): 2 hours (write + read + verify + diagnose integration)
- D9 (update): 30 min

Total: ~7.5 hours
