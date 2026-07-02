# Module I — CLI Feedback and UX

**Scope:** Progress indicators, readiness signals, and output quality across all long-running commands. No new features — purely making existing behaviour visible to the user.

**Files affected:** `scripts/up.ps1`, `scripts/llm.ps1`, `scripts/fetch-models.ps1`, `scripts/setup.ps1`, `scripts/install-prereqs.ps1`, `scripts/setup-docker.ps1`, `scripts/build-llama.ps1`

---

## Overview

| ID | Command | Problem | Fix |
|----|---------|---------|-----|
| I1 | `bob up` | Endpoint starts silently; user has no idea when it's ready | Spinner that resolves to "ready (Ns)" |
| I2 | `bob up` | WebUI browser-open is now async but gives no completion signal | Background poller prints "WebUI ready" to a status line |
| I3 | `bob fetch` | 38 GB downloads with no per-file progress, speed, or ETA | Progress bar: `filename [####......] 42% 12.3 MB/s ETA 4m` |
| I4 | `setup.ps1` | 10 steps, some take 20+ minutes, no elapsed time shown | Step counter with elapsed: `=== Step 3/10: Build llama.cpp (usually 5-15 min) ===` |
| I5 | `install-prereqs.ps1` | Silent winget installs give no feedback during long downloads | Step counter + per-tool status line |
| I6 | `setup-docker.ps1` | Docker pull is verbose but health check wait is a silent loop | Dot-progress during health check wait |
| I7 | `bob chat` | First token can take 3-10s (model loading); terminal appears frozen | Spinner from send → first token |
| I8 | `bob services start` | No confirmation that all containers are actually healthy | Print each container's state after `up -d` |
| I9 | `build-llama.ps1` | cmake outputs raw lines; no summary of elapsed time | Wrap with elapsed timer; print "Build succeeded in 8m32s" |

---

## Implementation notes

### PowerShell progress primitives

**`Write-Progress`** (native):
- Renders a proper progress bar in Windows Terminal and VS Code terminal
- Invisible when output is piped or redirected — safe to use anywhere
- Use for bounded operations where percent is knowable (file download, step N/total)

**Overwrite-line spinner** (for unbounded waits):
```powershell
$spin = @('|','/','-','\')
$i = 0
while (-not $ready) {
    Write-Host "`r  $($spin[$i % 4]) Waiting for endpoint..." -NoNewline
    $i++; Start-Sleep -Milliseconds 120
}
Write-Host "`r  Endpoint ready ($elapsed s)        "
```
Works in any terminal. `\r` returns cursor to start of line; trailing spaces erase the spinner.

**Avoid:** `Start-Sleep` loops without any output. Users assume the process hung.

---

## I1 — `bob up`: endpoint readiness spinner

### Problem
`up.ps1` starts llama-swap in a hidden window and immediately prints the URL with no confirmation it's actually accepting connections.

### Implementation
After starting `$swapProc`, poll `http://localhost:$port/v1/models` in a spinner loop. Resolve to a single "ready" line. Timeout after 60s with a clear error.

```powershell
$spin = [char[]]@('|','/','-','\')
$sw   = [Diagnostics.Stopwatch]::StartNew()
$i    = 0
$up   = $false
while ($sw.Elapsed.TotalSeconds -lt 60) {
    try {
        Invoke-RestMethod "http://localhost:$port/v1/models" -ErrorAction Stop | Out-Null
        $up = $true; break
    } catch { }
    Write-Host "`r  $($spin[$i++ % 4]) Starting endpoint..." -NoNewline -ForegroundColor DarkGray
    Start-Sleep -Milliseconds 200
}
if ($up) {
    Write-Host "`r  Endpoint ready ($([int]$sw.Elapsed.TotalSeconds)s)              " -ForegroundColor Green
} else {
    Write-Warning "Endpoint did not respond in 60s. Check: bob logs"
}
```

---

## I2 — `bob up`: WebUI ready signal

### Problem
The background poller (added in the `bob stop` fix) opens the browser silently. The user sees no confirmation in the terminal.

### Current state
```powershell
$bgScript = "for(`$i=0;`$i-lt90;`$i++){try{...Start-Process '$url';break}catch{Start-Sleep 2}}"
Start-Process pwsh -WindowStyle Hidden ...
Write-Host "Browser will open when Open WebUI is ready." -ForegroundColor DarkGray
```

### Fix options
**Option A (simple):** Write a temp file when ready; have up.ps1 tail it after the endpoint spinner resolves. Adds complexity, polling from two directions.

**Option B (recommended):** Instead of a hidden background process, do the WebUI wait *inline* after the endpoint spinner, since the user is already waiting there:

```powershell
# after endpoint is confirmed ready
if (-not $NoOpen) {
    $sw2 = [Diagnostics.Stopwatch]::StartNew()
    $j = 0; $uiUp = $false
    while ($sw2.Elapsed.TotalSeconds -lt 120) {
        try {
            Invoke-WebRequest "http://localhost:$webuiPort" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop | Out-Null
            $uiUp = $true; break
        } catch { }
        Write-Host "`r  $($spin[$j++ % 4]) Starting Open WebUI..." -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Milliseconds 500
    }
    if ($uiUp) {
        Write-Host "`r  Open WebUI ready ($([int]$sw2.Elapsed.TotalSeconds)s)           " -ForegroundColor Green
        Start-Process "http://localhost:$webuiPort"
    } else {
        Write-Warning "Open WebUI didn't respond. Open manually: http://localhost:$webuiPort"
    }
}
```

Trade-off: `bob up` now blocks for ~20s on first run. But it already effectively did via the "you have to wait" mental model — this just makes it explicit and accurate. Add `-NoOpen` note to skip waiting.

---

## I3 — `bob fetch`: per-file progress bar

### Problem
`fetch-models.ps1` downloads up to 38 GB with no per-file progress, no speed, no ETA.

### Implementation
Replace the raw `Invoke-WebRequest` or `curl` call with a progress-aware wrapper using `Write-Progress`:

```powershell
function Download-WithProgress {
    param([string]$Url, [string]$Dest)
    $response = Invoke-WebRequest $Url -Method Head -ErrorAction Stop
    $total    = [long]$response.Headers['Content-Length']
    $client   = [Net.WebClient]::new()
    $client.add_DownloadProgressChanged({
        param($s,$e)
        $speed = if ($e.BytesReceived -gt 0) {
            "$([int]($e.BytesReceived / 1MB / $sw.Elapsed.TotalSeconds)) MB/s"
        } else { '...' }
        Write-Progress -Activity (Split-Path $Dest -Leaf) `
            -Status "$([int]($e.BytesReceived/1MB))/$([int]($total/1MB)) MB  $speed" `
            -PercentComplete $e.ProgressPercentage
    })
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $task = $client.DownloadFileTaskAsync($Url, $Dest)
    while (-not $task.IsCompleted) { Start-Sleep -Milliseconds 200 }
    Write-Progress -Activity (Split-Path $Dest -Leaf) -Completed
}
```

Shows the native progress bar for each file. On completion, print a summary line: `Downloaded Qwen2.5-Coder-14B-Q4_K_M.gguf (8.4 GB) in 4m12s`.

---

## I4 — `setup.ps1`: step counter with elapsed time

### Problem
The 10-step setup prints `=== Step name ===` with no indication of which step number, how many remain, or how long each step takes.

### Implementation
Wrap the `Step` helper function:

```powershell
$stepTotal  = 10
$stepCurrent = 0
$setupStart  = [Diagnostics.Stopwatch]::StartNew()

function Step {
    param([string]$Name)
    if ($script:stepCurrent -gt 0) {
        $elapsed = [int]$script:stepSw.Elapsed.TotalSeconds
        Write-Host "    done in ${elapsed}s" -ForegroundColor DarkGray
    }
    $script:stepCurrent++
    $script:stepSw = [Diagnostics.Stopwatch]::StartNew()
    Write-Host "`n=== Step $script:stepCurrent/$script:stepTotal: $Name ===" -ForegroundColor Cyan
}
```

For slow steps, add a hint line:
```powershell
Step "Build llama.cpp"
Write-Host "  (first build takes 5-15 min — subsequent builds skip automatically)" -ForegroundColor DarkGray
```

---

## I5 — `install-prereqs.ps1`: step counter

Same pattern as I4 but with a total step count. Add a "already installed — skipping" line for each tool that passes the `Have` check, so the user can see what's being skipped vs what's being installed.

```
=== Step 1/8: Node.js ===
  Already installed (v22.1.0) — skipping.

=== Step 2/8: uv ===
  Installing via winget...
  done in 14s

=== Step 3/8: Go ===
  Already installed (go1.23.0) — skipping.
```

---

## I6 — `setup-docker.ps1`: health check progress

### Problem
The health check wait at the end of `docker compose up -d` is a silent loop (the containers may take 15-30s to become healthy).

### Implementation
After `docker compose up -d`, poll container states with dots:

```powershell
Write-Host "Waiting for containers to be healthy..." -NoNewline -ForegroundColor DarkGray
$timeout = 60; $elapsed = 0
while ($elapsed -lt $timeout) {
    $states = docker compose -f $compose ps --format json 2>$null |
              ConvertFrom-Json | Select-Object -ExpandProperty Health
    if ($states -notcontains 'starting') { break }
    Write-Host '.' -NoNewline -ForegroundColor DarkGray
    Start-Sleep -Seconds 3; $elapsed += 3
}
Write-Host " done" -ForegroundColor DarkGray
```

---

## I7 — `bob chat`: first-token spinner

### Problem
`bob chat coder "..."` sends the request and waits silently for the stream to start. If the model isn't loaded, this can be 3-10 seconds of frozen terminal.

### Implementation
Start a spinner thread before opening the SSE connection. Stop and erase it on first token received. Since the stream is consumed line-by-line via `Invoke-WebRequest -Method POST` + reading the response stream, inject the spinner between the request send and first `data:` line read.

---

## I8 — `bob services start`: container state summary

After `docker compose up -d`, print each container's state:

```
Services started:
  compose-langfuse-postgres-1   healthy    (3s)
  compose-langfuse-1            healthy    (12s)
  compose-searxng-1             running    (2s)
  compose-n8n-1                 running    (4s)
```

`running` = started but no healthcheck defined. `healthy` = healthcheck passed.

---

## I9 — `build-llama.ps1`: elapsed time summary

Wrap the cmake build with a stopwatch and print on completion:

```powershell
$bsw = [Diagnostics.Stopwatch]::StartNew()
& $cmake --build build --config Release -j
if ($LASTEXITCODE -ne 0) { throw "Build failed." }
Write-Host "Build succeeded in $([int]$bsw.Elapsed.TotalMinutes)m$([int]($bsw.Elapsed.Seconds))s." -ForegroundColor Green
```

---

## Implementation order

These are independent. Suggested order by user-visible impact:

1. **I1 + I2** — `bob up` spinner + readiness (every session start, highest frequency)
2. **I3** — `bob fetch` progress (single long operation, high frustration without it)
3. **I4 + I5** — setup step counters (one-time but longest operations)
4. **I6 + I8** — Docker feedback (used occasionally)
5. **I7** — chat spinner (nice-to-have; SSE stream adds complexity)
6. **I9** — build elapsed time (trivial, low priority)

---

## Non-goals

- Fancy TUI / ncurses-style layouts — PowerShell `Write-Progress` is sufficient and degrades gracefully
- Persisting progress state across sessions
- Progress for `bob stop` (fast, no progress needed)
- Progress for `bob gen`, `bob status`, `bob ps` (instantaneous)
