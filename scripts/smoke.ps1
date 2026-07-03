#requires -Version 7
# NC7 + ND2 — the "reliable working Bob" end-to-end smoke, and the shared CROSS-OS gate the ND2 CI
# acceptance matrix runs on Windows AND Linux. Formerly scripts/smoke-linux.ps1 (a back-compat shim
# still forwards here); OS-agnostic in mechanism — it exercises the RUNNING stack, so it passes on
# either OS when the stack is up.
#
# Scope (per the NC8 decision): provision -> serve -> a COHERENT answer. It is model-agnostic and
# deliberately does NOT gate on a real tool round-trip (tool-protocol correctness lives in the N-era
# fake-client unit tests). Steps:
#   1. inference endpoint reachable (llama-swap /v1/models)
#   2. `bob agent "say hi"` returns a non-empty answer
#   3. `bob agent serve`: GET /health (no auth) + an owner-scoped session turn (N1) + an SSE stream (N3/N6)
#
#   ./scripts/smoke.ps1           # test whatever is already running; SKIP (exit 0) if nothing is up
#   ./scripts/smoke.ps1 -Up       # bring the stack + agent server up first, tear the server down after
param([switch]$Up, [int]$TimeoutSec = 120)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"

$pass = 0; $fail = 0
function Ok  ($m) { $script:pass++; Write-Host "  PASS  $m" -ForegroundColor Green }
function Bad ($m) { $script:fail++; Write-Host "  FAIL  $m" -ForegroundColor Red }
function Skip($m) { Write-Host "  SKIP  $m" -ForegroundColor DarkYellow }

$bobCfg     = Get-BobConfig
$port       = $bobCfg.port ?? (Get-BobPortDefault 'port')
$agentPort  = $bobCfg.agent.agentPort ?? (Get-BobPortDefault 'agentPort')
$agentHost  = $bobCfg.agent.serveHost ?? '127.0.0.1'
$litellmKey = Get-Secret -Name 'litellmKey' -Default ($bobCfg.litellmKey ?? 'sk-local')
$infBase    = "http://localhost:$port/v1"
$agentBase  = "http://${agentHost}:$agentPort"
$bob        = Join-Path $PSScriptRoot 'bob.ps1'

Write-Host "`nBob end-to-end smoke  (OS: $(Get-BobOS))" -ForegroundColor Cyan
Write-Host "─────────────────────────────────────────" -ForegroundColor DarkGray

function Wait-Url([string]$Url, [int]$Seconds, [hashtable]$Headers = @{}) {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  while ($sw.Elapsed.TotalSeconds -lt $Seconds) {
    try { Invoke-RestMethod $Url -Headers $Headers -TimeoutSec 5 -ErrorAction Stop | Out-Null; return $true } catch {}
    Start-Sleep -Milliseconds 500
  }
  return $false
}

# --- 1. inference endpoint --------------------------------------------------
if ($Up) {
  Write-Host "[up] starting the stack (bob up)..." -ForegroundColor DarkGray
  & $bob up -NoOpen | Out-Null
}
if (-not (Wait-Url "$infBase/models" ($Up ? $TimeoutSec : 5))) {
  if ($Up) { Bad "inference endpoint never came up at $infBase (check: bob logs)"; Write-Host "`n$pass passed, $fail failed" -ForegroundColor Red; exit 1 }
  Skip "inference endpoint not running at $infBase — start it (bob up) or pass -Up. Nothing to test."
  Write-Host "`n$pass passed, $fail failed (skipped)" -ForegroundColor DarkYellow
  exit 0
}
Ok "inference endpoint reachable ($infBase)"

# --- 2. bob agent "say hi" returns a coherent answer ------------------------
$env:PYTHONIOENCODING = 'utf-8'
$answer = try { (& $bob agent 'say hi' 2>&1 | Out-String).Trim() } catch { "ERROR: $_" }
$env:PYTHONIOENCODING = $null
if ($answer -and $answer.Length -ge 2 -and $answer -notmatch '^(ERROR|Traceback|Error:)') {
  Ok "bob agent 'say hi' answered ($($answer.Length) chars)"
} else {
  Bad "bob agent 'say hi' returned no coherent answer: $($answer.Substring(0, [Math]::Min(120, $answer.Length)))"
}

# --- 3. agent HTTP server: /health + session turn + SSE ---------------------
$serverPid = $null
$serverPidFile = Join-Path (Get-CacheDir) 'agent-serve.smoke.pid'
try {
  $serverUp = Wait-Url "$agentBase/health" 3
  if (-not $serverUp -and $Up) {
    Write-Host "[up] starting the agent server (bob agent serve)..." -ForegroundColor DarkGray
    $serverPid = Start-BobBackgroundProcess -ArgList @('-NonInteractive', '-File', "`"$bob`"", 'agent', 'serve') -PidFile $serverPidFile
    $serverUp = Wait-Url "$agentBase/health" 30
  }

  if (-not $serverUp) {
    Skip "agent server not running at $agentBase — start it (bob agent serve) or pass -Up."
  } else {
    # 3a. /health (no auth)
    try {
      $h = Invoke-RestMethod "$agentBase/health" -TimeoutSec 5 -ErrorAction Stop
      Ok "GET /health responded"
    } catch { Bad "GET /health failed: $_" }

    $hdr = @{ Authorization = "Bearer $litellmKey" }

    # 3b. create an owner-scoped session (N1), then run a turn on it. A session_id must be minted
    # via POST /v1/sessions — an unknown id is a deliberate 404 (no-existence-leak; AGENT-SERVER.md).
    $sid = $null
    try {
      $cr = Invoke-RestMethod "$agentBase/v1/sessions" -Method Post -Headers $hdr `
              -ContentType 'application/json' -Body '{}' -TimeoutSec 10 -ErrorAction Stop
      $sid = $cr.session_id
      if ($sid) { Ok "created session ($sid)" } else { Bad "POST /v1/sessions returned no session_id" }
    } catch { Bad "create session (POST /v1/sessions) failed: $_" }

    if (-not $sid) {
      Skip "session turn + SSE — no session to run them on"
    } else {
      # owner-scoped session turn (N1)
      try {
        $body = @{ goal = 'say hi'; session_id = $sid } | ConvertTo-Json -Compress
        $r = Invoke-RestMethod "$agentBase/v1/agent/completions" -Method Post -Headers $hdr `
               -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec -ErrorAction Stop
        if ($r.result -and -not $r.error) { Ok "session turn returned a result (session_id=$($r.session_id))" }
        else { Bad "session turn returned no result / an error: $($r.error)" }
      } catch { Bad "session turn (POST /v1/agent/completions) failed: $_" }

      # 3c. SSE stream (N3/N6) on the same session — assert we receive event data
      try {
        $body = @{ goal = 'say hi'; session_id = $sid } | ConvertTo-Json -Compress
        $resp = Invoke-WebRequest "$agentBase/v1/agent/completions/stream" -Method Post -Headers $hdr `
                  -ContentType 'application/json' -Body $body -TimeoutSec $TimeoutSec -ErrorAction Stop
        $text = "$($resp.Content)"
        if ($text -match 'data:' -and $text -match '"type"') { Ok "SSE stream produced events" }
        else { Bad "SSE stream produced no recognizable events" }
      } catch { Bad "SSE stream (POST /v1/agent/completions/stream) failed: $_" }
    }
  }
}
finally {
  if ($serverPid) {
    Write-Host "[up] stopping the smoke agent server (PID $serverPid)..." -ForegroundColor DarkGray
    Stop-ProcessTree -ProcessId $serverPid
    Remove-Item $serverPidFile -ErrorAction SilentlyContinue
  }
}

Write-Host "`n$pass passed, $fail failed" -ForegroundColor $(if ($fail) { 'Red' } else { 'Green' })
exit $(if ($fail) { 1 } else { 0 })
