#requires -Version 7
# 'bob' — personal AI assistant CLI. Put on PATH by scripts/install-cli.ps1.
# Usage:  bob <command> [args]   (run `bob help` for the list)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"
$d        = (Get-ModelsConfig).defaults
# Port literals live only in $script:BobPortDefaults (M6) — read via Get-BobPortDefault, never re-inline.
$port        = $d.port        ?? (Get-BobPortDefault 'port')
$litellmPort = $d.litellmPort ?? (Get-BobPortDefault 'litellmPort')
$base        = "http://localhost:$port/v1"
$litellmBase = "http://localhost:$litellmPort/v1"
# Copy out of the automatic $args (whose slicing unwraps oddly) into plain arrays.
$argv = @($args)
if ($argv.Count -eq 0) {
  # NE2 Decision C — no-arg on an interactive terminal launches the REPL/TUI (`bob shell`, a python
  # verb routed below); a piped/redirected/CI invocation keeps today's help so scripts are unaffected.
  if (-not [Console]::IsInputRedirected -and -not [Console]::IsOutputRedirected) { $argv = @('shell') }
  else { $argv = @('help') }
}
$cmd  = $argv[0]
$rest = @($argv | Select-Object -Skip 1)   # always an array, even for a single arg

# NB4 (contract C1) — front-door dispatch. config/verbs.json (generated from the C6 command
# registry) declares each command's runtime; runtime commands are handled by `python -m bob`,
# everything else falls through to the orchestration switch below. Resolution is per
# fully-qualified command ("agent serve" vs "agent schedule") so subcommands split correctly.
# This is the "shim reads verbs.json without Python" step (bootstrap `setup` stays pwsh, no venv).
$verbsFile = Join-Path $repo 'config\verbs.json'
if (Test-Path $verbsFile) {
  try {
    $verbs = Get-Content -Raw -LiteralPath $verbsFile | ConvertFrom-Json -AsHashtable
    $route = $null
    $two   = if ($rest.Count) { "$cmd $($rest[0])" } else { $null }
    if     ($two -and $verbs.commands.Contains($two)) { $route = $verbs.commands[$two] }
    elseif ($verbs.commands.Contains($cmd))           { $route = $verbs.commands[$cmd] }
    if ($route -eq 'python') {
      $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
      if (-not (Test-Path $venvPy)) {
        Write-Host "Error: venv-litellm not found. Run: scripts\bootstrap-litellm.ps1" -ForegroundColor Red
        exit 1
      }
      # Regenerate data/config.json from the single source so the runtime sees fresh config
      # (M17 timestamp check makes this near-free), matching the old per-verb Get-BobConfig call.
      try { Get-BobConfig | Out-Null } catch {}
      $env:PYTHONPATH = Join-Path $repo 'scripts'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy -m bob @argv
      exit $LASTEXITCODE
    }
  } catch {
    Write-Warning "verbs.json dispatch failed ($_); falling back to the built-in switch."
  }
}

function Invoke-BobStream {
  # Stream a chat completion to stdout. Returns the full assistant text.
  # -Raw: suppress spinner + ANSI output, return clean text only (used by bob voice).
  param(
    [string]$Model,
    [object[]]$Messages,
    [int]$MaxTokens = 512,
    [string]$ApiBase,
    [switch]$Raw
  )
  $curl = Get-CurlExe   # NC5: curl.exe on Windows, curl elsewhere
  if (-not (Get-Command $curl -ErrorAction SilentlyContinue)) {
    throw "$curl not found (install curl, or on Windows requires Win10 1803+)"
  }
  $body = @{ model=$Model; stream=$true; max_tokens=$MaxTokens; messages=$Messages } |
          ConvertTo-Json -Depth 8 -Compress
  # Write body to a temp file — avoids Windows 32 KB command-line limit for large payloads (e.g. base64 images).
  # Use UTF-8 without BOM — BOM breaks JSON parsers (litellm, llama-server).
  $bodyTmp = [IO.Path]::GetTempFileName()
  [IO.File]::WriteAllText($bodyTmp, $body, [System.Text.UTF8Encoding]::new($false))
  $full = [System.Text.StringBuilder]::new()
  $spinRs = $null; $spinPs = $null; $spinDone = $false
  $prevEnc = [Console]::OutputEncoding
  [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
  try {
    if (-not $Raw) {
      $spinRs = [runspacefactory]::CreateRunspace(); $spinRs.Open()
      $spinPs = [powershell]::Create(); $spinPs.Runspace = $spinRs
      $spinPs.AddScript({
        $spin = [char[]]@('|','/','-','\'); $i = 0
        while ($true) { [Console]::Write("`r  $($spin[$i++ % 4]) ..."); [System.Threading.Thread]::Sleep(120) }
      }) | Out-Null
      $spinPs.BeginInvoke() | Out-Null
    }

    & $curl --no-buffer --silent -X POST "$ApiBase/chat/completions" `
        -H 'Content-Type: application/json' `
        -H 'Authorization: Bearer sk-local' `
        -d "@$bodyTmp" |
    ForEach-Object {
      if ($_ -match '^data: (.+)$') {
        $chunk = $Matches[1]
        if ($chunk -ne '[DONE]') {
          try {
            $t = ($chunk | ConvertFrom-Json).choices[0].delta.content
            if ($t) {
              if (-not $Raw -and -not $spinDone) {
                $spinPs.Stop(); $spinRs.Close()
                [Console]::Write("`r           `r")
                $spinDone = $true
              }
              if (-not $Raw) { Write-Host -NoNewline $t }
              [void]$full.Append($t)
            }
          } catch {}
        }
      }
    }
  } catch {
    Write-Host "Chat failed: $_" -ForegroundColor Red
    Write-Host "Is endpoint up? bob serve"
  } finally {
    if (-not $Raw -and -not $spinDone -and $spinPs) {
      $spinPs.Stop()
      if ($spinRs) { $spinRs.Close() }
      [Console]::Write("`r           `r")
    }
    Remove-Item $bodyTmp -ErrorAction SilentlyContinue
    [Console]::OutputEncoding = $prevEnc
  }
  return $full.ToString()
}

function Format-ForSpeech {
  # Strip markdown formatting before passing text to a TTS engine.
  # System prompts are advisory — this is the reliable safety net.
  param([string]$Text)
  $t = $Text
  # Normalize typographic Unicode characters to spoken equivalents.
  $t = $t.Replace([string][char]0x2014, ', ')   # em dash —
  $t = $t.Replace([string][char]0x2013, ' to ') # en dash –
  $t = $t.Replace([string][char]0x2018, "'")    # left single quote
  $t = $t.Replace([string][char]0x2019, "'")    # right single quote
  $t = $t.Replace([string][char]0x201C, '"')    # left double quote
  $t = $t.Replace([string][char]0x201D, '"')    # right double quote
  $t = $t.Replace([string][char]0x2026, '...')  # ellipsis
  $t = $t.Replace([string][char]0x00A0, ' ')    # non-breaking space
  $t = [regex]::Replace($t, '```[a-zA-Z]*\r?\n?', '')   # fenced code blocks — strip fence
  $t = $t.Replace('```', '')
  $t = [regex]::Replace($t, '`([^`]+)`', '$1')           # inline code
  $t = [regex]::Replace($t, '\*\*([^*]+)\*\*', '$1')     # **bold**
  $t = [regex]::Replace($t, '\*([^*\n]+)\*', '$1')       # *italic*
  $t = [regex]::Replace($t, '__([^_]+)__', '$1')         # __bold__
  $t = [regex]::Replace($t, '_([^_\n]+)_', '$1')         # _italic_
  $t = [regex]::Replace($t, '(?m)^#{1,6}\s+', '')        # headings
  $t = [regex]::Replace($t, '(?m)^[ \t]*[-*+]\s+', '')   # unordered bullets
  $t = [regex]::Replace($t, '(?m)^[ \t]*\d+\.\s+', '')   # numbered lists (keep the text)
  $t = [regex]::Replace($t, '(?m)^[-*_]{3,}\s*$', '')    # horizontal rules
  $t = [regex]::Replace($t, '\[([^\]]+)\]\([^\)]+\)', '$1')  # [link text](url)
  $t = [regex]::Replace($t, '(?m)^>\s?', '')             # blockquotes
  $t = $t.Replace('|', ' ')                              # table pipes → space
  $t = [regex]::Replace($t, '[ \t]{2,}', ' ')            # collapse multiple spaces
  $t = [regex]::Replace($t, '(\r?\n){3,}', "`n`n")       # collapse excess blank lines
  return $t.Trim()
}

function Show-Check([string]$label, [bool]$ok, [string]$fix = '') {
  $sym   = if ($ok) { [char]0x2713 } else { [char]0x2717 }
  $color = if ($ok) { 'Green' } else { 'Red' }
  $line  = "  $sym  $label"
  if (-not $ok -and $fix) { $line += "  →  $fix" }
  Write-Host $line -ForegroundColor $color
}

function Invoke-BobHealthCheck {
  # M11 — shared pre-flight for `bob setup check` and `bob doctor`. -Doctor adds the runtime
  # checks (endpoint reachable, GPU/VRAM, writable dirs, config.json parses) on top of the
  # dependency/registration checks. Delegates tool discovery to the Python loader (M1).
  param([switch]$Doctor)

  $bobCfg = Get-BobConfig
  $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
  $lPort  = $bobCfg.litellmPort ?? (Get-BobPortDefault 'litellmPort')

  $title = if ($Doctor) { 'Bob doctor — full pre-flight' } else { 'Bob agent setup check' }
  Write-Host "`n$title" -ForegroundColor Cyan
  Write-Host "─────────────────────────────────────────" -ForegroundColor DarkGray

  # 1. venv-litellm
  Show-Check 'venv-litellm exists' (Test-Path $venvPy) 'scripts\bootstrap-litellm.ps1'

  # 2. Python packages
  if (Test-Path $venvPy) {
    $hasOpenai   = (& $venvPy -c 'import openai; print("ok")' 2>$null) -eq 'ok'
    $hasRequests = (& $venvPy -c 'import requests; print("ok")' 2>$null) -eq 'ok'
    $pkgsOk = $hasOpenai -and $hasRequests
    $pkgFix = if (-not $hasOpenai) { 'pip install openai' } `
              elseif (-not $hasRequests) { 'pip install requests' } else { '' }
    Show-Check 'Python packages (openai, requests)' $pkgsOk $pkgFix
  } else {
    Show-Check 'Python packages (openai, requests)' $false 'run bootstrap-litellm.ps1 first'
  }

  # 3. data/config.json (auto-generated by Get-BobConfig)
  $cfgJson = Join-Path $repo 'data\config.json'
  Show-Check 'data/config.json exists' (Test-Path $cfgJson) 'run any bob command to generate'

  # 4. scripts/tools/ directory
  $toolsDir = Join-Path $repo 'scripts\tools'
  Show-Check 'scripts/tools/ exists' (Test-Path $toolsDir) ''

  # 5. data/schedules.json
  $schedFile = Join-Path $repo 'data\schedules.json'
  if (-not (Test-Path $schedFile)) {
    $sDir = Split-Path $schedFile
    if (-not (Test-Path $sDir)) { New-Item $sDir -ItemType Directory -Force | Out-Null }
    '[]' | Set-Content $schedFile -Encoding UTF8
    Write-Host "  →  data/schedules.json created (empty)" -ForegroundColor DarkGray
  }
  Show-Check 'data/schedules.json exists' (Test-Path $schedFile) ''

  # 6. fabric
  $hasFabric = [bool](Get-Command fabric -ErrorAction SilentlyContinue)
  Show-Check 'fabric on PATH' $hasFabric 'bob fabric-setup'

  # 7. SearXNG — check root page (fast); search page is slow on cold start
  $searxPort = $bobCfg.searxngPort ?? (Get-BobPortDefault 'searxngPort')
  # NC7 — Test-NetConnection is a Windows-only cmdlet; Test-PortInUse (TcpClient) is cross-platform.
  $searxOk = Test-PortInUse -Port $searxPort
  Show-Check "SearXNG reachable (:$searxPort)" $searxOk 'bob services start'

  # 8. n8n
  $n8nPort = $bobCfg.n8nPort ?? (Get-BobPortDefault 'n8nPort')
  $n8nOk = try { (Invoke-WebRequest "http://localhost:$n8nPort" -TimeoutSec 8 -UseBasicParsing -ErrorAction Stop).StatusCode -lt 500 } catch { $false }
  Show-Check "n8n reachable (:$n8nPort)" $n8nOk 'bob services start'

  # 9. LiteLLM proxy — TCP connect is reliable (health endpoint does backend checks = slow)
  $litellmOk = Test-PortInUse -Port $lPort
  Show-Check "LiteLLM proxy (:$lPort)" $litellmOk 'bob litellm'

  # 10. BobAgent recurring task (NC4 seam: scheduled task on Windows, cron entry on Linux)
  $taskOk = (Get-AgentTaskStatus).Registered
  Show-Check 'BobAgent task registered' $taskOk 'bob agent install'

  # 11. Agent model downloaded
  try {
    $agentModel = (Get-Models).models | Where-Object { $_.role -eq 'agent' } | Select-Object -First 1
    if ($agentModel) {
      $modelFile = Join-Path $repo "models\$($agentModel.gguf)"
      Show-Check "Agent model ($($agentModel.gguf))" (Test-Path $modelFile) 'bob fetch'
    } else {
      Show-Check 'Agent model in active profile' $false 'add agent role to config\models.psd1'
    }
  } catch { Show-Check 'Agent model (check failed)' $false '' }

  # 12. Tools load cleanly — delegate to the Python loader (single source of discovery).
  #     Honors agent.disabledTools; surfaces import/contract/configure errors.
  if (Test-Path $venvPy) {
    $disabledList = ($bobCfg.agent.disabledTools ?? @()) -join ','
    $loaderPy = Join-Path $repo 'scripts\tools\tool_loader.py'
    $listOut  = & $venvPy $loaderPy --list --disabled $disabledList 2>&1
    $loadErrs = @($listOut | Select-String -Pattern 'load error')
    Show-Check 'Agent tools load without error' ($loadErrs.Count -eq 0) 'run: bob agent tools'
    foreach ($le in $loadErrs) { Write-Host "     $le" -ForegroundColor DarkYellow }
  } else {
    Show-Check 'Agent tools load without error' $false 'venv-litellm missing'
  }

  # 13. litellm.yaml exists
  $litellmYaml = Join-Path $repo 'config\litellm.yaml'
  Show-Check 'config/litellm.yaml exists' (Test-Path $litellmYaml) 'scripts\gen-litellm.ps1'

  if ($Doctor) {
    Write-Host "  ── runtime ──" -ForegroundColor DarkGray

    # Inference endpoint reachable (/models)
    $apiOk = try { [bool]((Invoke-RestMethod "$base/models" -TimeoutSec 3).data) } catch { $false }
    Show-Check "Inference endpoint reachable ($base)" $apiOk 'bob serve'

    # GPU / VRAM — NC6: no GPU is NOT a failure. The NC8 CPU tier still serves; report the backend.
    $vram = Get-GpuVramGB
    if ($vram) { Show-Check "GPU VRAM detected (~$vram GB)" $true }
    else       { Show-Check "No GPU -> CPU backend (NC8 tier)" $true 'nvidia-smi absent or no NVIDIA GPU' }

    # data/ + logs/ writable — resolved through the C4 seam so BOB_DATA_DIR is honored (NC7).
    foreach ($entry in @(@{ name = 'data'; path = (Get-DataDir) }, @{ name = 'logs'; path = (Get-CacheDir) })) {
      $p = $entry.path
      $writable = try {
        if (-not (Test-Path $p)) { New-Item $p -ItemType Directory -Force | Out-Null }
        $probe = Join-Path $p ".write-test.$PID"
        Set-Content -LiteralPath $probe -Value 'x' -ErrorAction Stop
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
        $true
      } catch { $false }
      Show-Check "$($entry.name)/ writable" $writable "check permissions on $p"
    }

    # config.json parses
    $parseOk = try { $null -ne (Get-Content $cfgJson -Raw -ErrorAction Stop | ConvertFrom-Json) } catch { $false }
    Show-Check 'data/config.json parses' $parseOk 'run any bob command to regenerate'

    # ── reproducibility (ND1) ── installed state vs versions.lock: submodules at their locked commit,
    # present+pinned models' checksums match. Unpinned / not-downloaded items are skipped (not drift).
    Write-Host "  ── reproducibility ──" -ForegroundColor DarkGray
    try {
      $repro = Test-BobReproducibility
      foreach ($r in $repro) { Show-Check $r.label $r.ok $r.fix }
      if (-not $repro) { Show-Check 'versions.lock: nothing installed to verify yet' $true }
    } catch {
      Show-Check 'versions.lock reproducibility' $false 'bob lock'
    }
  }

  Write-Host ""
}


switch ($cmd) {
  'status' {
    try { $apiModels = (Invoke-RestMethod "$base/models" -TimeoutSec 3).data }
    catch {
      Write-Host "Endpoint not running. Start with: bob serve" -ForegroundColor Yellow; break
    }
    $loadedIds = @{}; foreach ($m in $apiModels) { $loadedIds[$m.id] = $true }
    $cfg     = Get-ModelsConfig
    $profile = Resolve-ProfileName -Config $cfg
    $models  = (Get-Models -Profile $profile).models
    Write-Host "`nEndpoint: $base  " -NoNewline; Write-Host "[running]" -ForegroundColor Green
    Write-Host "Profile:  $profile`n"
    Write-Host ("{0,-10} {1,-36} {2,-9} {3}" -f 'Role','Model','VRAM','State')
    Write-Host ('-' * 70)
    foreach ($m in $models) {
      $label  = "$($m.gguf -replace '\.gguf$') ($($m.sizeGB) GB)"
      $loaded = $loadedIds.ContainsKey($m.role)
      $state  = if ($m.pinned -and $loaded) { 'loaded (pinned)' }
                elseif ($m.pinned)           { 'loading...' }
                elseif ($loaded)             { 'loaded' }
                else                         { 'unloaded' }
      $color  = if ($loaded) { 'Green' } else { 'DarkGray' }
      $vram   = if ($loaded) { "$($m.sizeGB) GB" } else { '--' }
      Write-Host ("{0,-10} {1,-36} {2,-9} " -f $m.role, $label, $vram) -NoNewline
      Write-Host $state -ForegroundColor $color
    }
    $bobVoice = try { (Get-BobConfig).voice } catch { $null }
    $sttPort  = $bobVoice.sttPort ?? (Get-BobPortDefault 'sttPort')
    if (Test-PortInUse -Port $sttPort) {
      Write-Host ("  {0,-10} {1,-36} {2}" -f 'whisper','(stt server)',"UP (port $sttPort)") -ForegroundColor Green
    } else {
      Write-Host ("  {0,-10} {1,-36} {2}" -f 'whisper','(stt server)',"down (port $sttPort)") -ForegroundColor DarkGray
    }
    $ttsPort = $bobVoice.ttsPort ?? (Get-BobPortDefault 'ttsPort')
    if (Test-PortInUse -Port $ttsPort) {
      Write-Host ("  {0,-10} {1,-36} {2}" -f 'piper','(tts server)',"UP (port $ttsPort)") -ForegroundColor Green
    } else {
      Write-Host ("  {0,-10} {1,-36} {2}" -f 'piper','(tts server)',"down (port $ttsPort)") -ForegroundColor DarkGray
    }
    Write-Host ""
  }
  'ps' {
    Write-Host "`nBob Processes`n"
    Write-Host ("{0,-15} {1,-8} {2,-10} {3,-10} {4}" -f 'Service','PID','RAM','Uptime','Status')
    Write-Host ('-' * 60)
    foreach ($svc in @('llama-swap','litellm','open-webui','whisper','piper')) {
      $pf = Join-Path $repo "logs\$svc.pid"
      if (Test-Path $pf) {
        $wPid = [int](Get-Content $pf -Raw)
        $proc = Get-Process -Id $wPid -ErrorAction SilentlyContinue
        if ($proc) {
          $ram    = "$([math]::Round($proc.WorkingSet64/1MB)) MB"
          $uptime = ([DateTime]::Now - $proc.StartTime).ToString('hh\:mm\:ss')
          Write-Host ("{0,-15} {1,-8} {2,-10} {3,-10} " -f $svc,$wPid,$ram,$uptime) -NoNewline
          Write-Host "running" -ForegroundColor Green
        } else {
          Write-Host ("{0,-15} {1,-8} {2,-10} {3,-10} " -f $svc,$wPid,'--','--') -NoNewline
          Write-Host "dead (stale PID file)" -ForegroundColor Red
        }
      } else {
        Write-Host ("{0,-15} {1,-8} {2,-10} {3,-10} " -f $svc,'--','--','--') -NoNewline
        Write-Host "not running" -ForegroundColor DarkGray
      }
    }
    Write-Host ""
  }
  'diagnose' { & "$repo\scripts\diagnose.ps1" }
  'up'     { & "$repo\scripts\up.ps1" -NoOpen:($rest -contains '-NoOpen') -WithServices:($rest -contains '-WithServices') }
  'serve'  { & "$repo\scripts\start.ps1" }                    # llama-swap (:8080) + LiteLLM (:8081), interactive
  'restart' {
    Write-Host "Stopping endpoint..."
    Get-Process -Name 'llama-swap','llama-server','open-webui' -ErrorAction SilentlyContinue | Stop-Process -Force
    foreach ($svc in 'llama-swap','open-webui') {
      $pf = Join-Path $repo "logs\$svc.pid"
      if (Test-Path $pf) {
        $wPid = [int](Get-Content $pf -Raw)
        Get-Process -Id $wPid -ErrorAction SilentlyContinue | Stop-Process -Force
        Remove-Item $pf
      }
    }
    $litellmPid = Join-Path $repo 'logs\litellm.pid'
    if (Test-Path $litellmPid) {
      $wPid = [int](Get-Content $litellmPid -Raw -ErrorAction SilentlyContinue)
      if ($wPid) { Get-Process -Id $wPid -ErrorAction SilentlyContinue | Stop-Process -Force }
      Remove-Item $litellmPid -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    Write-Host "Starting endpoint (interactive — Ctrl+C to stop)..."
    & "$repo\scripts\start.ps1"
  }
  'logs' {
    $n = if ($rest.Count -and $rest[0] -match '^\d+$') { [int]$rest[0] } else { 50 }
    $logFile = Join-Path $repo 'logs\llama-swap.log'
    if (-not (Test-Path $logFile)) {
      Write-Host "No log file yet. Start endpoint first: bob serve"; break
    }
    Write-Host "Tailing $logFile (last $n lines, Ctrl+C to stop):`n"
    Get-Content $logFile -Wait -Tail $n
  }
  'webui'  { & "$repo\tools\venv-webui\Scripts\open-webui.exe" serve --port ($d.webuiPort ?? (Get-BobPortDefault 'webuiPort')) }
  'aider'  { & "$repo\tools\venv-aider\Scripts\aider.exe" @rest }   # runs in your current folder
  'models' {
    $loadedIds = @{}; $endpointUp = $false
    try {
      $apiModels = (Invoke-RestMethod "$base/models" -TimeoutSec 3).data
      foreach ($m in $apiModels) { $loadedIds[$m.id] = $true }
      $endpointUp = $true
    } catch {}
    $cfg     = Get-ModelsConfig
    $profile = Resolve-ProfileName -Config $cfg
    $models  = (Get-Models -Profile $profile).models
    Write-Host "`nProfile: $profile`n"
    $fmt = "{0,-10} {1,-42} {2,-9} {3}"
    Write-Host ($fmt -f 'Role','Model','VRAM','State'); Write-Host ('-' * 70)
    foreach ($m in $models) {
      $label = "$($m.gguf -replace '\.gguf$','' -replace '[-_]',' ') ($($m.sizeGB) GB)"
      $state = if (-not $endpointUp)        { '(endpoint down)' }
               elseif ($loadedIds[$m.role]) { if ($m.pinned) { 'loaded, pinned' } else { 'loaded' } }
               else                         { 'unloaded' }
      $color = if ($endpointUp -and $loadedIds[$m.role]) { 'Green' } else { 'DarkGray' }
      Write-Host ($fmt -f $m.role, $label, "$($m.sizeGB) GB", '') -NoNewline
      Write-Host $state -ForegroundColor $color
    }
    Write-Host ""
    if (-not $endpointUp) { Write-Host "Endpoint not running — state unknown. bob serve" -ForegroundColor Yellow }
  }
  'show' {
    if (-not $rest.Count) { Write-Host "usage: bob show <role>   (roles: coder chat planner fim embed)"; break }
    $info = Get-Models
    $m = $info.models | Where-Object role -eq $rest[0]
    if (-not $m) { Write-Host "Unknown role '$($rest[0])'. Valid: $($info.models.role -join ', ')"; break }
    $dest = Join-Path $repo "models\$($m.gguf)"
    Write-Host "`nRole:     $($m.role)"
    Write-Host "File:     $($m.gguf)"
    Write-Host "VRAM:     $($m.sizeGB) GB"
    Write-Host "Repo:     $($m.repo)"
    Write-Host "Path:     $($m.path)"
    if (Test-Path $dest) {
      $actGB = [math]::Round((Get-Item $dest).Length/1GB, 2)
      Write-Host "On disk:  $actGB GB" -ForegroundColor Green
    } else {
      Write-Host "On disk:  MISSING" -ForegroundColor Yellow
    }
    $mf = Join-Path $repo 'models\manifest.json'
    if (Test-Path $mf) {
      $manifest = Get-Content $mf -Raw | ConvertFrom-Json -AsHashtable
      if ($manifest[$m.gguf]) {
        $entry = $manifest[$m.gguf]
        Write-Host "SHA256:   $($entry.sha256.Substring(0,24))..."
        Write-Host "Verified: $($entry.verifiedAt)"
      }
    }
    Write-Host ""
  }
  'gen'    {                                                   # regenerate config from models.psd1
    & "$repo\scripts\gen-llama-swap.ps1" @rest
    & "$repo\scripts\gen-litellm.ps1"
    & "$repo\scripts\gen-webui.ps1"
    & "$repo\scripts\gen-continue.ps1"
    Assert-BobPortKeys   # M6 — fail loudly if the merged config is missing a service port
  }
  'fetch'  {                                                   # download models for a profile
    $fa = @{}
    if ($rest -contains '--list') { $fa['ListOnly'] = $true }
    $prof = @($rest | Where-Object { $_ -ne '--list' })
    if ($prof.Count) { $fa['Profile'] = $prof[0] }
    & "$repo\scripts\fetch-models.ps1" @fa
  }
  'profiles' {
    $cfg = Get-ModelsConfig
    foreach ($p in $cfg.profiles.Keys | Sort-Object) {
      $roles = @($cfg.profiles[$p].Keys | Where-Object { -not $_.StartsWith('_') })
      $gb    = ($roles | ForEach-Object { $cfg.profiles[$p][$_].sizeGB } | Measure-Object -Sum).Sum
      $have  = @($roles | Where-Object { Test-Path (Join-Path $repo "models\$($cfg.profiles[$p][$_].gguf)") }).Count
      $mark  = if ($p -eq $cfg.activeProfile) { '* ' } else { '  ' }
      Write-Host ("{0}{1,-6} ~{2,5:N1} GB  {3}/{4} on disk   {5}" -f $mark, $p, $gb, $have, $roles.Count, $cfg.profiles[$p]._targetVRAM)
    }
    $vram = Get-GpuVramGB; $sug = Get-SuggestedProfile -VramGB $vram
    if ($sug) { Write-Host "`nDetected ~$vram GB VRAM -> suggested '$sug'." -ForegroundColor DarkGray }
    Write-Host "(* = active)  switch: bob profile <name>   peek without switching: bob fetch --list <name>" -ForegroundColor DarkGray
  }
  'profile' {
    if ($rest[0] -eq 'auto' -or -not $rest.Count) {
      $vramGB = Get-GpuVramGB
      if (-not $vramGB) {
        # NC6/NC8 — degrade cleanly on a GPU-less box: select the CPU tier instead of erroring.
        Write-Host "No GPU detected -> switching to the 'cpu' profile (tiny model, correctness/wiring only)." -ForegroundColor Yellow
        Set-ActiveProfile 'cpu'
        & "$repo\scripts\gen-llama-swap.ps1"
        & "$repo\scripts\fetch-models.ps1" -ListOnly
        Write-Host "If the model is MISSING above, download it:  bob fetch" -ForegroundColor Yellow
        break
      }
      $sug = Get-SuggestedProfile -VramGB $vramGB
      if (-not $sug) {
        Write-Host "No profile fits $vramGB GB VRAM. Available:"
        (Get-ModelsConfig).profiles.Keys | Sort-Object | ForEach-Object { Write-Host "  $_" }
        break
      }
      Write-Host "Detected $vramGB GB VRAM -> switching to profile: $sug"
      Set-ActiveProfile $sug
      & "$repo\scripts\gen-llama-swap.ps1"
      & "$repo\scripts\fetch-models.ps1" -ListOnly
      Write-Host "If any model is MISSING above, download it:  bob fetch" -ForegroundColor Yellow
    } else {
      Set-ActiveProfile $rest[0]
      & "$repo\scripts\gen-llama-swap.ps1"
      & "$repo\scripts\fetch-models.ps1" -ListOnly
      Write-Host "If any model is MISSING above, download it:  bob fetch" -ForegroundColor Yellow
    }
  }
  'bench'  {
    $allModels = Get-Models
    $resolveModel = {
      param($name)
      $byRole = $allModels.models | Where-Object role -eq $name
      if ($byRole) { Join-Path $repo "models\$($byRole.gguf)" } else { $name }
    }
    $default = & $resolveModel 'coder'
    $m = if ($rest.Count) { & $resolveModel $rest[0] } else { $default }
    & "$repo\bin\llama-bench.exe" -m $m -ngl 99 -fa 1 -p 512 -n 128
  }
  'chat' {
    # --- Flag extraction ---
    $isPro    = $rest -contains '--pro'
    $isThink  = $rest -contains '--think'
    $isCode   = $rest -contains '--code'
    $isRaw    = $rest -contains '--raw'
    $maxTok   = $d.maxTokens ?? 512
    # Strip known flags; --max N is handled below
    $restWork = [System.Collections.Generic.List[string]]($rest | Where-Object { $_ -notin '--pro','--think','--code','--raw' })
    for ($fi = 0; $fi -lt $restWork.Count; $fi++) {
      if ($restWork[$fi] -eq '--max' -and $fi+1 -lt $restWork.Count) {
        $maxTok = [int]$restWork[$fi+1]; $restWork.RemoveAt($fi+1); $restWork.RemoveAt($fi); break
      }
    }
    $restClean = $restWork.ToArray()

    # --- Legacy syntax: bob chat <knownRole> <prompt...> [--sys <text>] ---
    $knownRoles = @('chat','coder','planner','fim','embed','chat-pro','coder-pro','planner-pro')
    if ($restClean.Count -ge 2 -and $knownRoles -contains $restClean[0]) {
      $model = $restClean[0]
      $argList = @($restClean | Select-Object -Skip 1)
      $sysPrompt = $null; $promptParts = @(); $i = 0
      while ($i -lt $argList.Count) {
        if ($argList[$i] -eq '--sys' -and $i+1 -lt $argList.Count) {
          $sysPrompt = $argList[$i+1]; $i += 2
        } else { $promptParts += $argList[$i]; $i++ }
      }
      $messages = @()
      if ($sysPrompt) { $messages += @{ role='system'; content=$sysPrompt } }
      $messages += @{ role='user'; content=($promptParts -join ' ') }
      Invoke-BobStream -Model $model -Messages $messages -MaxTokens $maxTok -ApiBase $litellmBase | Out-Null
      Write-Host ""; break
    }

    # --- Smart routing (M8: single routing table in Get-RoleForTask) ---
    $bobCfg = Get-BobConfig
    $chatTask   = if ($isThink) { 'think' } elseif ($isCode) { 'code' } else { 'chat' }
    $targetRole = Get-RoleForTask -Config $bobCfg -Task $chatTask -Pro:$isPro

    $modelInfo   = (Get-Models).models | Where-Object role -eq $targetRole
    $displayName = if ($modelInfo) { ($modelInfo.gguf -replace '\.gguf$','') } else { $targetRole }

    # --- One-shot: prompt provided as arg ---
    if ($restClean.Count -gt 0) {
      $prompt  = $restClean -join ' '
      $messages = @(@{ role='system'; content=$bobCfg.persona.systemPrompt }, @{ role='user'; content=$prompt })
      $reply = Invoke-BobStream -Model $targetRole -Messages $messages -MaxTokens $maxTok -ApiBase $litellmBase -Raw:$isRaw
      if ($isRaw) { Write-Output $reply } else { Write-Host "" }
      break
    }

    # --- REPL mode ---
    Write-Host "Bob [$targetRole | $displayName]  (empty line to exit, !recall <query> to inject memory)" -ForegroundColor Cyan
    Write-Host ""
    $history    = [System.Collections.Generic.List[hashtable]]::new()
    $history.Add(@{ role='system'; content=$bobCfg.persona.systemPrompt })
    $memPs      = Join-Path $repo 'scripts\bob-memory.ps1'
    $memEnabled = $bobCfg.memory.enabled -and (Test-Path $memPs)
    $memSlotIdx = -1   # index of the single replaceable memory slot in $history

    try {
      while ($true) {
        $userInput = Read-Host '>'
        if ([string]::IsNullOrWhiteSpace($userInput)) { break }

        # !recall <query> — explicit memory injection, replaces previous slot
        if ($userInput -match '^!recall\s+(.+)$') {
          if (-not $memEnabled) { Write-Host "  [memory not enabled — set memory.enabled = `$true in config/bob.psd1]" -ForegroundColor DarkGray; continue }
          $q = $Matches[1].Trim()
          try {
            $memJson  = & $memPs recall $q 2>$null
            $memItems = $memJson | ConvertFrom-Json -ErrorAction SilentlyContinue
            if ($memItems -and $memItems.Count -gt 0) {
              $memText = '[Memory: ' + ($memItems | ForEach-Object { $_.content } | Join-String -Separator ' | ') + ']'
              if ($memSlotIdx -ge 0) { $history[$memSlotIdx] = @{ role='system'; content=$memText } }
              else { $history.Add(@{ role='system'; content=$memText }); $memSlotIdx = $history.Count - 1 }
              Write-Host "  [injected $($memItems.Count) memor$(if($memItems.Count -eq 1){'y'}else{'ies'}) into context]" -ForegroundColor DarkGray
            } else { Write-Host "  [no memories matched '$q']" -ForegroundColor DarkGray }
          } catch { Write-Host "  [memory recall failed: $_]" -ForegroundColor DarkGray }
          continue
        }

        # !memory — show DB status
        if ($userInput -eq '!memory') {
          if ($memEnabled) { try { & $memPs status 2>$null } catch {} } else { Write-Host "  [memory not enabled]" -ForegroundColor DarkGray }
          continue
        }

        $history.Add(@{ role='user'; content=$userInput })
        $reply = Invoke-BobStream -Model $targetRole -Messages $history.ToArray() -MaxTokens $maxTok -ApiBase $litellmBase
        Write-Host ""
        if ($reply) { $history.Add(@{ role='assistant'; content=$reply }) }
      }
    } finally {
      if ($memEnabled -and ($bobCfg.memory.autoSummarize -eq $true) -and ($history.Count -gt 2)) {
        Write-Host "  [summarizing session...]" -ForegroundColor DarkGray
        try {
          $tmpFile = [System.IO.Path]::GetTempFileName() + ".json"
          $history.ToArray() | ConvertTo-Json -Depth 4 | Set-Content $tmpFile -Encoding utf8
          & $memPs summarize-session --messages-file $tmpFile 2>$null
          Remove-Item $tmpFile -ErrorAction SilentlyContinue
        } catch {}
      }
      Write-Host "" -ForegroundColor DarkGray
    }
  }
  'code' {
    # bob code [--pro] [prompt] — alias for bob chat --code [--pro] [prompt]
    $fwd = @('--code') + $rest
    & "$PSScriptRoot\bob.ps1" chat @fwd
  }
  'think' {
    # bob think [--pro] [prompt] — alias for bob chat --think [--pro] [prompt]
    $fwd = @('--think') + $rest
    & "$PSScriptRoot\bob.ps1" chat @fwd
  }
  'stop' {
    $killed = [System.Collections.Generic.List[string]]::new()

    # 1) Kill C++ binaries by process name (survive stale PID files)
    foreach ($bin in @('llama-swap','llama-server','whisper-server')) {
      $procs = Get-Process -Name $bin -ErrorAction SilentlyContinue
      if ($procs) { $procs | Stop-Process -Force; $killed.Add($bin) }
    }

    # 2) Kill Python-hosted services via PID files (litellm, piper, open-webui)
    foreach ($svc in @('litellm','piper','open-webui')) {
      $pf = Join-Path $repo "logs\$svc.pid"
      if (Test-Path $pf) {
        $wPid = [int](Get-Content $pf -Raw -ErrorAction SilentlyContinue)
        if ($wPid) {
          # Also kill child processes (uvicorn workers etc.) — NC1 seam, OS-aware child reap.
          try {
            Get-Process -Id $wPid -ErrorAction Stop | Out-Null   # verify alive before claiming a kill
            Stop-ProcessTree -ProcessId $wPid
            $killed.Add($svc)
          } catch {}
        }
        Remove-Item $pf -ErrorAction SilentlyContinue
      }
    }

    # 3) Clean remaining PID files
    foreach ($svc in @('llama-swap','whisper')) {
      $pf = Join-Path $repo "logs\$svc.pid"
      Remove-Item $pf -ErrorAction SilentlyContinue
    }

    # 4) Docker services (only if docker is available and compose file exists)
    $compose = "$repo\tools\compose\docker-compose.yml"
    if ((Get-Command docker -ErrorAction SilentlyContinue) -and (Test-Path $compose)) {
      $running = docker compose -f $compose ps -q 2>$null
      if ($running) {
        docker compose -f $compose down 2>$null | Out-Null
        $killed.Add('docker-services')
      }
    }

    if ($killed.Count) {
      Write-Host "Stopped: $($killed -join ', ')" -ForegroundColor Green
    } else {
      Write-Host "Nothing was running." -ForegroundColor DarkGray
    }
  }
  'build' {
    # NC3/NC8 — (re)build llama.cpp. Auto-selects the CPU tier when no GPU is present (or with --cpu);
    # otherwise a CUDA build for the detected arch. Cross-platform via build-llama.ps1's seam.
    $cpu   = ($rest -contains '--cpu') -or (-not (Get-GpuInfo))
    $force = $rest -contains '--force'
    $bArgs = @{}
    if ($cpu)   { $bArgs['Cpu'] = $true }
    if ($force) { $bArgs['Force'] = $true }
    if ($cpu -and -not ($rest -contains '--cpu')) {
      Write-Host "No GPU detected — building the CPU-only tier. Use 'bob build --cpu' to force, or install CUDA for a GPU build." -ForegroundColor Yellow
    }
    & "$repo\scripts\build-llama.ps1" @bArgs
  }
  'lock' {
    # ND1 — (re)generate versions.lock from the single sources (git gitlinks + models.psd1 +
    # manifest.json + pip freeze). `bob lock --check` is the staleness gate wired into check.ps1.
    if ($rest -contains '--check') {
      $rc = Test-VersionsLockSync
      if ($rc -eq 0) { Write-Host "versions.lock in sync" -ForegroundColor Green }
      exit $rc
    }
    $p = Write-VersionsLock
    Write-Host "wrote $p" -ForegroundColor Green
  }
  'update' {
    # ND3 — release-aware, cross-platform update with rollback. Moves the working tree to a target
    # release (default: fast-forward the current branch; `--tag <ref>` for a specific release), syncs
    # submodules to the NEW lock's pinned commits, rebuilds ONLY what changed with a bin/ snapshot,
    # verifies the rebuilt binary, and rolls the build output back on failure. Regenerates versions.lock
    # on success. Cross-platform via the NC1 seam (Get-BinExe / Backup-/Restore-BuildOutput).
    $targetRef = $null
    for ($i = 0; $i -lt $rest.Count; $i++) { if ($rest[$i] -eq '--tag' -and ($i + 1) -lt $rest.Count) { $targetRef = $rest[$i + 1] } }

    $binDir = Join-Path $repo 'bin'
    $llmCpp = Join-Path $repo 'external\llama.cpp'
    $before = & git -C $llmCpp rev-parse HEAD 2>$null

    Write-Host "Fetching updates..."
    & git -C $repo fetch --tags --quiet
    if ($targetRef) {
      Write-Host "Checking out release '$targetRef'..."
      & git -C $repo checkout $targetRef
    } else {
      Write-Host "Fast-forwarding the current branch..."
      & git -C $repo pull --ff-only
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "Fetch/checkout failed — nothing changed." -ForegroundColor Red; break }

    Write-Host "Syncing submodules to the pinned commits..."
    & git -C $repo submodule update --init --recursive
    if ($LASTEXITCODE -ne 0) { Write-Host "Submodule sync failed." -ForegroundColor Red; break }
    $after = & git -C $llmCpp rev-parse HEAD 2>$null

    # Reinstall the venv from the (possibly updated) requirements lock. Idempotent — a no-op if unchanged.
    if (Test-Path (Join-Path $repo 'scripts\bootstrap-litellm.ps1')) {
      Write-Host "Ensuring the Python runtime venv matches the lock..."
      & "$repo\scripts\bootstrap-litellm.ps1"
    }

    # Rebuild ONLY changed components. llama.cpp is the heavy one; rebuild only if its commit moved.
    $short = { param($s) if ($s) { "$s".Substring(0, [Math]::Min(8, "$s".Length)) } else { '(none)' } }
    if ($before -eq $after) {
      Write-Host "llama.cpp unchanged ($(& $short $after)) — no rebuild needed." -ForegroundColor DarkGray
    } else {
      Write-Host "llama.cpp $(& $short $before) -> $(& $short $after); rebuilding (bin/ snapshotted for rollback)..."
      $bak = Backup-BuildOutput -Path $binDir
      & "$repo\scripts\build-llama.ps1" -Force
      $buildOk = ($LASTEXITCODE -eq 0)

      # Verify the rebuild produced a working server binary (the concrete post-build gate). `bob doctor`
      # is run afterward for a full readout, but this is what decides rollback.
      $srv = Get-BinExe 'llama-server'
      $verifyOk = $false
      if ($buildOk -and (Test-Path $srv)) {
        try { & $srv --version 2>&1 | Out-Null; $verifyOk = ($LASTEXITCODE -eq 0) } catch { $verifyOk = $false }
      }

      if (-not $verifyOk) {
        Write-Host "Update verification failed (build ok=$buildOk, binary ok=$verifyOk) — rolling back the build output." -ForegroundColor Red
        if (Restore-BuildOutput -Path $binDir -BakPath $bak) {
          Write-Host "Rolled bin/ back to the previous build. Your install is unchanged." -ForegroundColor Yellow
        }
        break
      }
      Remove-BuildOutputBackup -Path $binDir -BakPath $bak
      Write-Host "Rebuild verified." -ForegroundColor Green
    }

    # Regenerate versions.lock so it reflects the new installed set, and give a full doctor readout.
    Write-VersionsLock | Out-Null
    Write-Host "Running bob doctor..." -ForegroundColor DarkGray
    & "$repo\scripts\bob.ps1" doctor
    Write-Host "Update complete (release $(Get-BobVersion))." -ForegroundColor Green
  }
  'version' {
    # ND3 — report the Bob release (VERSION + versions.lock) plus component versions + submodule pins.
    # Cross-platform: binary paths via the NC1 seam (Get-BinExe adds .exe only on Windows).
    $lockRelease = try { (Get-VersionsLock).release } catch { $null }
    Write-Host "Bob $(Get-BobVersion)" -ForegroundColor Cyan
    if ($lockRelease) { Write-Host "  versions.lock release: $lockRelease" -ForegroundColor DarkGray }
    $swapBin = Get-BinExe 'llama-swap'
    $srvBin  = Get-BinExe 'llama-server'
    $swapVer = if (Test-Path $swapBin) { & $swapBin --version 2>&1 | Select-Object -First 1 } else { '(not built)' }
    $srvVer  = if (Test-Path $srvBin)  { & $srvBin  --version 2>&1 | Select-Object -First 1 } else { '(not built)' }
    $swapCommit = & git -C "$repo\external\llama-swap" rev-parse --short HEAD 2>$null
    $llmCommit  = & git -C "$repo\external\llama.cpp"  rev-parse --short HEAD 2>$null
    Write-Host "llama-swap:   $swapVer  ($swapCommit)"
    Write-Host "llama-server: $srvVer  ($llmCommit)"
  }
  'verify-urls' {
    $vArgs = @{}
    if ($rest.Count) { $vArgs['Profile'] = $rest[0] }
    & "$repo\scripts\verify-urls.ps1" @vArgs
  }
  'mlock' {
    # Check current status first (no admin needed)
    $mlockStatus = & "$repo\scripts\grant-mlock.ps1" -Check 2>&1
    Write-Host $mlockStatus
    if ($LASTEXITCODE -ne 0) {
      Write-Host ""
      Write-Host "This grants the Windows SeLockMemoryPrivilege to your user account." -ForegroundColor DarkGray
      Write-Host "Required for --mlock to actually pin model weights in RAM." -ForegroundColor DarkGray
      Write-Host "A UAC prompt will appear. After granting, restart this terminal." -ForegroundColor DarkGray
      Write-Host ""
      $ans = Read-Host "Grant now? [y/N]"
      if ($ans -match '^[Yy]') {
        & "$repo\scripts\grant-mlock.ps1"
      }
    }
  }
  'fabric-setup' { & "$repo\scripts\setup-fabric.ps1" }
  'fabric'       { & "$repo\bin\fabric.exe" @rest }
  'litellm' {
    $subCmd  = if ($rest.Count -and $rest[0] -in 'stop','status','start') { $rest[0] } else { '' }
    $fwdArgs = if ($subCmd) { @($rest | Select-Object -Skip 1) } else { @($rest) }
    $pidFile = Join-Path $repo 'logs\litellm.pid'
    $lPort   = $d.litellmPort ?? (Get-BobPortDefault 'litellmPort')
    switch ($subCmd) {
      'stop' {
        if (Stop-ServiceByPid -Name 'LiteLLM' -PidFile $pidFile) {
          Write-Host "LiteLLM stopped." -ForegroundColor Green
        } else { Write-Host "LiteLLM not running." -ForegroundColor DarkGray }
      }
      'status' {
        if (Test-Path $pidFile) {
          $wPid = [int](Get-Content $pidFile -Raw)
          $proc = Get-Process -Id $wPid -ErrorAction SilentlyContinue
          if ($proc) {
            $uptime = ([DateTime]::Now - $proc.StartTime).ToString('hh\:mm\:ss')
            Write-Host "LiteLLM running  PID=$wPid  uptime=$uptime  http://localhost:$lPort/v1" -ForegroundColor Green
          } else { Write-Host "LiteLLM dead (stale PID $wPid)." -ForegroundColor Red }
        } else { Write-Host "LiteLLM not running." -ForegroundColor DarkGray }
      }
      default { & "$repo\scripts\start-litellm.ps1" @fwdArgs }
    }
  }
  'services' {
    $action  = if ($rest.Count) { $rest[0] } else { '' }
    $compose = "$repo\tools\compose\docker-compose.yml"
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
      Write-Host "Docker not found. Run: .\scripts\setup-docker.ps1" -ForegroundColor Yellow; break
    }
    $envFile = "$repo\tools\compose\.env"
    switch ($action) {
      'start'  {
        # Regenerate .env from current models.psd1 values before starting
        $dp = (Get-ModelsConfig).defaults
        @"
REPO_PATH=$repo
LANGFUSE_PORT=$($dp.langfusePort ?? (Get-BobPortDefault 'langfusePort'))
SEARXNG_PORT=$($dp.searxngPort ?? (Get-BobPortDefault 'searxngPort'))
N8N_PORT=$($dp.n8nPort ?? (Get-BobPortDefault 'n8nPort'))
"@ | Set-Content $envFile -Encoding utf8
        docker compose -f $compose up -d
        Write-Host "Services started:" -ForegroundColor Green
        docker compose -f $compose ps --format json 2>$null | ConvertFrom-Json | ForEach-Object {
            $state = if ($_.Health) { $_.Health } else { $_.State }
            $color = if ($state -eq 'healthy') { 'Green' } else { 'DarkGray' }
            Write-Host ("  {0,-40} {1}" -f $_.Name, $state) -ForegroundColor $color
        }
      }
      'stop'   { docker compose -f $compose down }
      'status' { docker compose -f $compose ps }
      'logs'   { docker compose -f $compose logs --tail=50 -f }
      default  { Write-Host "Usage: bob services start|stop|status|logs" }
    }
  }
  'eval' {
    $eArgs = @{}
    $pos = @()
    for ($i = 0; $i -lt $rest.Count; $i++) {
      if ($rest[$i] -eq '--shots' -and $i+1 -lt $rest.Count) { $eArgs['Shots'] = [int]$rest[++$i] }
      elseif ($rest[$i] -eq '--limit' -and $i+1 -lt $rest.Count) { $eArgs['Limit'] = [int]$rest[++$i] }
      else { $pos += $rest[$i] }
    }
    if ($pos.Count -ge 1) { $eArgs['Role'] = $pos[0] }
    if ($pos.Count -ge 2) { $eArgs['Task'] = $pos[1] }
    & "$repo\scripts\eval.ps1" @eArgs
  }
  'remember' {
    if (-not $rest.Count) { Write-Host "usage: bob remember <text>"; break }
    & "$repo\scripts\bob-memory.ps1" store ($rest -join ' ')
  }
  'recall' {
    if (-not $rest.Count) { Write-Host "usage: bob recall <query>"; break }
    & "$repo\scripts\bob-memory.ps1" recall ($rest -join ' ')
  }
  'memory' {
    if (-not $rest.Count) { & "$repo\scripts\bob-memory.ps1" status; break }
    $sub = $rest[0]
    if ($sub -in 'status','clear','list','show','forget','edit','pin','unpin','export','migrate','init-profile') {
      # bob-memory.ps1 prepends --db and forwards the rest to bob_memory.py's subparsers (MEM-5).
      & "$repo\scripts\bob-memory.ps1" @rest
    } else {
      Write-Host "Usage: bob memory <status|clear|list|show|forget|edit|pin|unpin|export|migrate> [args]"
    }
  }
  'budget' { & "$repo\scripts\bob-budget.ps1" }

  # ── Phase 3: Agent setup check ──────────────────────────────────────────────
  'setup' {
    $setupSub = if ($rest.Count) { $rest[0] } else { 'check' }
    if ($setupSub -ne 'check') { Write-Host "Usage: bob setup check  (or: bob doctor for the full pre-flight)"; break }
    Invoke-BobHealthCheck
  }

  'doctor' { Invoke-BobHealthCheck -Doctor }

  # ── Phase 2: Voice + Vision ─────────────────────────────────────────────────
  'setup-voice' { & "$repo\scripts\setup-voice.ps1" $(if ($rest -contains '-Force') { '-Force' }) }

  'listen' {
    $bobCfg  = Get-BobConfig
    $venvPy  = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $capture = Join-Path $repo 'scripts\bob-voice-capture.py'
    $env:PYTHONIOENCODING = 'utf-8'
    & $venvPy $capture --port ($bobCfg.voice.sttPort ?? (Get-BobPortDefault 'sttPort')) --silence-sec ($bobCfg.voice.silenceSec ?? 1.5)
    $env:PYTHONIOENCODING = $null
  }

  'transcribe' {
    if (-not $rest.Count) { Write-Host "usage: bob transcribe <audio-file>"; break }
    $bobCfg  = Get-BobConfig
    $venvPy  = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $capture = Join-Path $repo 'scripts\bob-voice-capture.py'
    $env:PYTHONIOENCODING = 'utf-8'
    & $venvPy $capture --file $rest[0] --port ($bobCfg.voice.sttPort ?? (Get-BobPortDefault 'sttPort'))
    $env:PYTHONIOENCODING = $null
  }

  'speak' {
    $bobCfg = Get-BobConfig
    $voice  = Join-Path $repo "bin\voices\$($bobCfg.voice.ttsVoice ?? 'en_GB-alan-medium').onnx"
    $piperExe = Join-Path $repo 'bin\piper.exe'
    if (-not (Test-Path $piperExe)) { Write-Host "piper.exe not found — run: bob setup-voice" -ForegroundColor Yellow; break }
    if (-not (Test-Path $voice))    { Write-Host "Voice model not found at $voice — run: bob setup-voice" -ForegroundColor Yellow; break }
    $text = if ($rest.Count) { $rest -join ' ' } else { $input | Out-String }
    if (-not $text -or -not $text.Trim()) { Write-Host "Nothing to speak." -ForegroundColor DarkGray; break }
    $tmpTxt = [IO.Path]::GetTempFileName()
    $tmpWav = $tmpTxt + '.wav'
    try {
      Set-Content $tmpTxt -Value $text -Encoding utf8 -NoNewline
      Get-Content $tmpTxt | & $piperExe --model $voice --output_file $tmpWav --quiet 2>&1 | Out-Null
      if ($LASTEXITCODE -ne 0) { throw "piper exited with code $LASTEXITCODE" }
      (New-Object System.Media.SoundPlayer $tmpWav).PlaySync()
    } finally {
      Remove-Item $tmpTxt, $tmpWav -ErrorAction SilentlyContinue
    }
  }

  'describe' {
    $pro  = $rest -contains '--pro'
    $rest = @($rest | Where-Object { $_ -ne '--pro' })
    if (-not $rest.Count) { Write-Host "usage: bob describe <image> [--pro] [prompt]"; break }
    $imagePath = $rest[0]
    if (-not (Test-Path $imagePath)) { Write-Host "File not found: $imagePath" -ForegroundColor Red; break }
    $bobCfg   = Get-BobConfig
    $prompt   = if ($rest.Count -gt 1) { $rest[1..($rest.Count-1)] -join ' ' } else { 'Describe this image.' }
    # Resize image to max 1024px on longest edge — large screenshots exceed context limits.
    Add-Type -AssemblyName System.Drawing
    $srcBmp  = [Drawing.Bitmap]::new($imagePath)
    $maxDim  = 1024
    $scale   = [Math]::Min($maxDim / $srcBmp.Width, $maxDim / $srcBmp.Height)
    $scale   = [Math]::Min($scale, 1.0)   # never upscale
    $w = [int]($srcBmp.Width  * $scale)
    $h = [int]($srcBmp.Height * $scale)
    $resizedTmp = $null
    if ($scale -lt 1.0) {
      $dstBmp = [Drawing.Bitmap]::new($w, $h)
      $g = [Drawing.Graphics]::FromImage($dstBmp)
      $g.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
      $g.DrawImage($srcBmp, 0, 0, $w, $h)
      $g.Dispose(); $srcBmp.Dispose()
      $resizedTmp = [IO.Path]::GetTempFileName() + '.png'
      $dstBmp.Save($resizedTmp, [Drawing.Imaging.ImageFormat]::Png)
      $dstBmp.Dispose()
      $imagePath = $resizedTmp
    } else {
      $srcBmp.Dispose()
    }
    try {
      $b64  = [Convert]::ToBase64String([IO.File]::ReadAllBytes($imagePath))
      $ext  = [IO.Path]::GetExtension($imagePath).TrimStart('.').ToLower()
      $mime = if ($ext -eq 'jpg') { 'jpeg' } else { $ext }
      $messages = @(@{
        role    = 'user'
        content = @(
          @{ type = 'image_url'; image_url = @{ url = "data:image/$mime;base64,$b64" } },
          @{ type = 'text';      text      = $prompt }
        )
      })
      $vRole = Get-RoleForTask -Config $bobCfg -Task vision -Pro:$pro
      Invoke-BobStream -Model $vRole -Messages $messages -MaxTokens ($d.maxTokens ?? 512) -ApiBase $litellmBase | Out-Null
      Write-Host ""
    } catch {
      # M9 — don't let an LLM/stream failure abort with a raw error; print and return.
      Write-Host "describe failed: $_" -ForegroundColor Red
    } finally {
      if ($resizedTmp) { Remove-Item $resizedTmp -ErrorAction SilentlyContinue }
    }
  }

  'screenshot' {
    Add-Type -AssemblyName System.Windows.Forms, System.Drawing
    $screen = [Windows.Forms.Screen]::PrimaryScreen.Bounds
    $bmp    = [Drawing.Bitmap]::new($screen.Width, $screen.Height)
    $g      = [Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($screen.Location, [Drawing.Point]::Empty, $screen.Size)
    $tmp = [IO.Path]::GetTempFileName() + '.png'
    $bmp.Save($tmp, [Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()
    try {
      $descArgs = @('describe', $tmp) + $rest
      & "$PSScriptRoot\bob.ps1" @descArgs
    } finally {
      Remove-Item $tmp -ErrorAction SilentlyContinue
    }
  }

  'voice' {
    $bobCfg    = Get-BobConfig
    $pro       = $rest -contains '--pro'
    $useAgent  = $rest -contains '--agent'
    $rest      = @($rest | Where-Object { $_ -notin @('--pro', '--agent') })
    $voiceSys  = $bobCfg.voice.systemPrompt ?? $bobCfg.persona.systemPrompt
    $voiceRole = Get-RoleForTask -Config $bobCfg -Task voice -Pro:$pro
    $voiceTok  = $bobCfg.voice.maxTokens ?? 256
    $venvPy     = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $loopScript = Join-Path $repo 'scripts\bob_loop.py'
    # Auto-start whisper STT if not reachable
    $sttPort = $bobCfg.voice.sttPort ?? (Get-BobPortDefault 'sttPort')
    if (-not (Test-PortInUse -Port $sttPort)) {
      Write-Host "Starting whisper STT..." -ForegroundColor DarkGray
      & "$PSScriptRoot\start-whisper.ps1" -NoWindow
    }
    $modeLabel = if ($useAgent) { 'agent' } elseif ($pro) { 'pro' } else { 'chat' }
    Write-Host "Bob voice loop ($modeLabel) — Ctrl+C to exit. Use headphones to avoid echo." -ForegroundColor Cyan
    if (-not $useAgent) { Write-Host "Model: $voiceRole" -ForegroundColor DarkGray }
    # Conversation history persists for the duration of the voice session (chat mode only).
    $messages = @(@{ role = 'system'; content = $voiceSys })
    try {
      while ($true) {
        # M9 — one failed turn (LLM down, STT/TTS error) must not abort the whole session.
        try {
        Write-Host "Listening..." -ForegroundColor DarkGray
        $transcript = & "$PSScriptRoot\bob.ps1" listen
        if (-not $transcript -or -not $transcript.Trim()) { continue }
        Write-Host "> $transcript" -ForegroundColor Yellow
        $agentExitCode = 0
        if ($useAgent) {
          $env:PYTHONIOENCODING = 'utf-8'
          $response = & $venvPy $loopScript $transcript --agency silent 2>$null | Out-String
          $agentExitCode = $LASTEXITCODE
          $env:PYTHONIOENCODING = $null
          $response = $response.Trim()
        } else {
          # /no_think: Qwen3 skips reasoning scratchpad — voice needs fast replies.
          $messages += @{ role = 'user'; content = "$transcript /no_think" }
          $response = Invoke-BobStream -Model $voiceRole -Messages $messages -MaxTokens $voiceTok -ApiBase $litellmBase -Raw
          # Strip trailing non-ASCII residue (Qwen3 leaks special-token bytes at end of raw stream).
          $response = [regex]::Replace($response.Trim(), '[-￿]+$', '')
        }
        $response = Format-ForSpeech $response
        if ($response) {
          Write-Host "Bob: $response" -ForegroundColor Cyan
          if (-not $useAgent) { $messages += @{ role = 'assistant'; content = $response } }
          & "$PSScriptRoot\bob.ps1" speak $response
        }
        if ($agentExitCode -eq 42) {
          Write-Host "Music started. Stopping voice loop." -ForegroundColor DarkGray
          break
        }
        } catch {
          Write-Host "Voice turn failed: $_  (continuing)" -ForegroundColor Red
          continue
        }
      }
    } finally {
      Write-Host "`nVoice loop ended." -ForegroundColor DarkGray
    }
  }
  'whisper' {
    $subCmd  = if ($rest.Count -and $rest[0] -in 'stop','start','status') { $rest[0] } else { '' }
    $whisperPidFile = Join-Path $repo 'logs\whisper.pid'
    $sttPort = try { (Get-BobConfig).voice.sttPort ?? (Get-BobPortDefault 'sttPort') } catch { Get-BobPortDefault 'sttPort' }
    switch ($subCmd) {
      'stop' {
        if (Stop-ServiceByPid -Name 'whisper-server' -PidFile $whisperPidFile) {
          Write-Host "whisper-server stopped." -ForegroundColor Green
        } else { Write-Host "whisper-server not running." -ForegroundColor DarkGray }
      }
      'status' {
        if (Test-Path $whisperPidFile) {
          $wPid = [int](Get-Content $whisperPidFile -Raw)
          $proc = Get-Process -Id $wPid -ErrorAction SilentlyContinue
          if ($proc) {
            $uptime = ([DateTime]::Now - $proc.StartTime).ToString('hh\:mm\:ss')
            Write-Host "whisper-server running  PID=$wPid  uptime=$uptime  http://localhost:$sttPort" -ForegroundColor Green
          } else { Write-Host "whisper-server dead (stale PID $wPid)." -ForegroundColor Red }
        } else { Write-Host "whisper-server not running." -ForegroundColor DarkGray }
      }
      default { & "$repo\scripts\start-whisper.ps1" $(if ($rest -contains '-NoWindow') { '-NoWindow' }) }
    }
  }

  'piper' {
    $subCmd = if ($rest.Count -and $rest[0] -in 'stop','start','status') { $rest[0] } else { '' }
    $piperPidFile = Join-Path $repo 'logs\piper.pid'
    $ttsPort = try { (Get-BobConfig).voice.ttsPort ?? (Get-BobPortDefault 'ttsPort') } catch { Get-BobPortDefault 'ttsPort' }
    switch ($subCmd) {
      'stop' {
        if (Stop-ServiceByPid -Name 'piper-server' -PidFile $piperPidFile) {
          Write-Host "piper-server stopped." -ForegroundColor Green
        } else { Write-Host "piper-server not running." -ForegroundColor DarkGray }
      }
      'status' {
        if (Test-Path $piperPidFile) {
          $wPid = [int](Get-Content $piperPidFile -Raw)
          $proc = Get-Process -Id $wPid -ErrorAction SilentlyContinue
          if ($proc) {
            $uptime = ([DateTime]::Now - $proc.StartTime).ToString('hh\:mm\:ss')
            Write-Host "piper-server running  PID=$wPid  uptime=$uptime  http://localhost:$ttsPort" -ForegroundColor Green
          } else { Write-Host "piper-server dead (stale PID $wPid)." -ForegroundColor Red }
        } else { Write-Host "piper-server not running." -ForegroundColor DarkGray }
      }
      default { & "$repo\scripts\start-piper-server.ps1" $(if ($rest -contains '-NoWindow') { '-NoWindow' }) }
    }
  }

  'agent' {
    $bobCfg     = Get-BobConfig
    $venvPy     = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $loopScript = Join-Path $repo 'scripts\bob_loop.py'

    if (-not (Test-Path $venvPy)) {
      Write-Host "Error: venv-litellm not found. Run: scripts\bootstrap-litellm.ps1" -ForegroundColor Red
      break
    }

    $knownSubs = @('schedule','log','tools','install','uninstall','status','enable','disable','serve','mcp')
    $sub       = if ($rest.Count -and $rest[0] -in $knownSubs) { $rest[0] } else { 'run' }
    $subRest   = if ($sub -ne 'run') { @($rest | Select-Object -Skip 1) } else { @($rest) }

    switch ($sub) {
      'run' {
        if (-not $subRest.Count) { Write-Host "Usage: bob agent <goal>  |  bob agent schedule|log|tools|install|status"; break }
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy $loopScript @subRest
        $env:PYTHONIOENCODING = $null
      }

      'schedule' {
        $schedCmd  = if ($subRest.Count) { $subRest[0] } else { 'list' }
        $schedArgs = @($subRest | Select-Object -Skip 1)
        $sFile     = Join-Path $repo ($bobCfg.agent.scheduleFile ?? 'data\schedules.json')

        function Read-BobSchedules { if (Test-Path $sFile) { @(Get-Content $sFile -Raw -Encoding UTF8 | ConvertFrom-Json) } else { @() } }
        function Write-BobSchedules($data) {
          $tmp = [System.IO.Path]::GetTempFileName()
          $data | ConvertTo-Json -Depth 10 | Set-Content $tmp -Encoding UTF8
          Move-Item $tmp $sFile -Force
        }
        function Register-BobAgentTask {
          # NC4 — OS-aware via the seam: Windows scheduled task, Linux crontab line. Both fire
          # bob-agent.ps1 every minute; the runner + Test-CronDue do the cron-expression evaluation.
          Register-AgentTask -ScriptPath (Join-Path $repo 'scripts\bob-agent.ps1')
        }

        switch ($schedCmd) {
          'list' {
            $s = Read-BobSchedules
            if (-not $s.Count) { Write-Host "No schedules. Add: bob agent schedule add <name> --cron <expr> --goal <text>"; break }
            $s | Format-Table `
              @{L='Name';E={$_.name}}, @{L='Cron';E={$_.cron}}, @{L='On';E={$_.enabled}},
              @{L='LastRun';E={if($_.lastRun){[DateTime]::Parse($_.lastRun).ToLocalTime().ToString('MM-dd HH:mm')}else{'-'}}},
              @{L='Result';E={if($_.lastRunResult){$_.lastRunResult.Substring(0,[Math]::Min(50,$_.lastRunResult.Length))}else{'-'}}} -AutoSize
          }
          'add' {
            $name = $schedArgs | Where-Object { -not $_.StartsWith('--') } | Select-Object -First 1
            if (-not $name) { Write-Host "Usage: bob agent schedule add <name> --cron <expr> --goal <text>"; break }
            $cronIdx  = [Array]::IndexOf($schedArgs,'--cron');  $cron  = if ($cronIdx -ge 0) { $schedArgs[$cronIdx+1] } else { '0 9 * * 1-5' }
            $goalIdx  = [Array]::IndexOf($schedArgs,'--goal');  $goal  = if ($goalIdx -ge 0) { $schedArgs[$goalIdx+1] } else { $name }
            $roleIdx  = [Array]::IndexOf($schedArgs,'--role');  $role  = if ($roleIdx -ge 0) { $schedArgs[$roleIdx+1] } else { 'agent' }
            $titleIdx = [Array]::IndexOf($schedArgs,'--title'); $title = if ($titleIdx -ge 0) { $schedArgs[$titleIdx+1] } else { $name }
            $notify   = $schedArgs -contains '--notify'
            $s = @(Read-BobSchedules)
            if ($s | Where-Object { $_.name -eq $name }) { Write-Host "Schedule '$name' already exists." -ForegroundColor Red; break }
            $sDir = Split-Path $sFile; if (-not (Test-Path $sDir)) { New-Item $sDir -ItemType Directory -Force | Out-Null }
            $s += [PSCustomObject]@{
              name=''; cron=''; action=@{}; notify=$false; notifyTitle=''; enabled=$true; lastRun=$null; lastRunResult=$null; createdAt=''
            }
            $s[-1].name = $name; $s[-1].cron = $cron; $s[-1].action = @{type='agent';goal=$goal;role=$role}
            $s[-1].notify = $notify; $s[-1].notifyTitle = $title; $s[-1].createdAt = [DateTime]::UtcNow.ToString('o')
            Write-BobSchedules $s
            Write-Host "Added '$name'  cron: $cron" -ForegroundColor Green
            if (-not (Get-AgentTaskStatus).Registered) {
              Register-BobAgentTask; Write-Host "BobAgent task auto-registered." -ForegroundColor DarkGray
            }
          }
          'remove' {
            $name = $schedArgs | Select-Object -First 1
            if (-not $name) { Write-Host "Usage: bob agent schedule remove <name>"; break }
            Write-BobSchedules (@(Read-BobSchedules) | Where-Object { $_.name -ne $name })
            Write-Host "Removed '$name'." -ForegroundColor Green
          }
          'run' {
            $name = $schedArgs | Select-Object -First 1
            if (-not $name) { Write-Host "Usage: bob agent schedule run <name>"; break }
            $entry = Read-BobSchedules | Where-Object { $_.name -eq $name } | Select-Object -First 1
            if (-not $entry) { Write-Host "Schedule not found: $name" -ForegroundColor Red; break }
            Write-Host "Running '$name' ..." -ForegroundColor Cyan
            $role = if ($entry.action.role) { $entry.action.role } else { $bobCfg.routing.agentRole ?? 'planner' }
            $env:PYTHONIOENCODING = 'utf-8'
            $result = & $venvPy $loopScript $entry.action.goal --role $role | Out-String
            $env:PYTHONIOENCODING = $null
            if ($entry.notify -and $result.Trim()) {
              . "$repo\scripts\bob-toast.ps1"
              Send-BobToast -Title ($entry.notifyTitle ?? $entry.name) -Body $result.Trim()
            }
            $s = @(Read-BobSchedules)
            foreach ($e in $s) {
              if ($e.name -eq $name) {
                $e.lastRun = [DateTime]::UtcNow.ToString('o')
                $trimmed = $result.Trim()
                $e.lastRunResult = $trimmed.Substring(0,[Math]::Min($trimmed.Length, ($bobCfg.agent.maxResultChars ?? 500)))
              }
            }
            Write-BobSchedules $s
          }
          'enable' {
            $name = $schedArgs | Select-Object -First 1; if (-not $name) { Write-Host "Usage: bob agent schedule enable <name>"; break }
            $s = @(Read-BobSchedules); $found = $false
            foreach ($e in $s) { if ($e.name -eq $name) { $e.enabled = $true; $found = $true } }
            if ($found) { Write-BobSchedules $s; Write-Host "Enabled: $name" -ForegroundColor Green } else { Write-Host "Not found: $name" -ForegroundColor Red }
          }
          'disable' {
            $name = $schedArgs | Select-Object -First 1; if (-not $name) { Write-Host "Usage: bob agent schedule disable <name>"; break }
            $s = @(Read-BobSchedules); $found = $false
            foreach ($e in $s) { if ($e.name -eq $name) { $e.enabled = $false; $found = $true } }
            if ($found) { Write-BobSchedules $s; Write-Host "Disabled: $name" -ForegroundColor DarkYellow } else { Write-Host "Not found: $name" -ForegroundColor Red }
          }
          'install' { Register-BobAgentTask; Write-Host "BobAgent task registered." -ForegroundColor Green }
          'status' {
            $st = Get-AgentTaskStatus
            if ($st.Registered) {
              Write-Host "BobAgent: $($st.State)" -ForegroundColor Green
              if ($st.NextRun) { Write-Host "Next run: $($st.NextRun.ToLocalTime())" }
            } else { Write-Host "BobAgent not registered. Run: bob agent schedule install" -ForegroundColor DarkGray }
          }
          default { Write-Host "bob agent schedule <add|list|run|remove|enable|disable|install|status>" }
        }
      }

      'log' {
        $logFile = Join-Path $repo ($bobCfg.agent.logFile ?? 'logs\bob-agent.log')
        if (Test-Path $logFile) { Get-Content $logFile -Tail 50 -Wait } else { Write-Host "No log yet: $logFile" -ForegroundColor DarkGray }
      }

      'tools' {
        $disabledTools = ($bobCfg.agent.disabledTools ?? @()) -join ','
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy "$repo\scripts\tools\tool_loader.py" --list --disabled $disabledTools
        $env:PYTHONIOENCODING = $null
      }

      'install' {
        # NC4 — OS-aware: Windows scheduled task, Linux crontab line (every minute).
        Register-AgentTask -ScriptPath (Join-Path $repo 'scripts\bob-agent.ps1')
        Write-Host "BobAgent task registered (runs every minute)." -ForegroundColor Green
        Write-Host "Enable proactive mode: set agent.enabled = `$true in config/bob.psd1" -ForegroundColor Cyan
      }

      'uninstall' {
        Unregister-AgentTask
        Write-Host "BobAgent task removed." -ForegroundColor Green
      }

      'status' {
        $st = Get-AgentTaskStatus
        if ($st.Registered) {
          Write-Host "BobAgent: $($st.State)" -ForegroundColor Green
          if ($st.NextRun) { Write-Host "Next run: $($st.NextRun.ToLocalTime())" }
          $logFile = Join-Path $repo ($bobCfg.agent.logFile ?? 'logs\bob-agent.log')
          if (Test-Path $logFile) { Write-Host "`nRecent log:" -ForegroundColor DarkGray; Get-Content $logFile -Tail 5 }
        } else { Write-Host "BobAgent: not registered. Run: bob agent install" -ForegroundColor DarkGray }
      }

      'serve' {
        # M5 — bind from config (loopback by default). The uvicorn CLI --host would otherwise
        # override the server's config-driven bind, re-exposing 0.0.0.0 regardless of bob.psd1.
        $aPort = $bobCfg.agent.agentPort ?? (Get-BobPortDefault 'agentPort')
        $aHost = $bobCfg.agent.serveHost ?? '127.0.0.1'
        Write-Host "Bob agent HTTP server on ${aHost}:$aPort  (POST /v1/agent/completions, Bearer auth)" -ForegroundColor Cyan
        if ($aHost -eq '0.0.0.0') {
          Write-Host "  WARNING: bound to 0.0.0.0 (LAN-exposed). Keep agent.allowPrivateFetch = `$false." -ForegroundColor Yellow
        }
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy -m uvicorn bob_agent_server:app --host $aHost --port $aPort --app-dir "$repo\scripts"
        $env:PYTHONIOENCODING = $null
      }

      'mcp' {
        # N10 — expose Bob's tools over the Model Context Protocol (stdio). Gated by agent.mcpEnabled.
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy "$repo\scripts\bob_mcp_server.py"
        $env:PYTHONIOENCODING = $null
      }

      default { Write-Host "Usage: bob agent <goal>  |  bob agent schedule|log|tools|install|uninstall|status|serve|mcp" }
    }
  }

  'clip' {
    if (-not $rest.Count) { Write-Host "Usage: bob clip <url> [--note <text>]"; break }
    $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $env:PYTHONIOENCODING = 'utf-8'
    & $venvPy "$repo\scripts\bob_clip.py" @rest
    $env:PYTHONIOENCODING = $null
  }

  'tools' {
    $bobCfg = Get-BobConfig
    $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    $toolSub  = if ($rest.Count) { $rest[0] } else { 'list' }
    $toolArgs = @($rest | Select-Object -Skip 1)
    $disabledTools = ($bobCfg.agent.disabledTools ?? @()) -join ','
    switch ($toolSub) {
      'list' {
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy "$repo\scripts\tools\tool_loader.py" --list --disabled $disabledTools
        $env:PYTHONIOENCODING = $null
      }
      'test' {
        if (-not $toolArgs.Count) { Write-Host "Usage: bob tools test <name>"; break }
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy "$repo\scripts\tools\tool_loader.py" --test $toolArgs[0]
        $env:PYTHONIOENCODING = $null
      }
      'info' {
        if (-not $toolArgs.Count) { Write-Host "Usage: bob tools info <name>"; break }
        $env:PYTHONIOENCODING = 'utf-8'
        & $venvPy "$repo\scripts\tools\tool_loader.py" --info $toolArgs[0]
        $env:PYTHONIOENCODING = $null
      }
      default { Write-Host "bob tools list|test <name>|info <name>" }
    }
  }

  'plugins' {
    $sub = if ($rest.Count) { $rest[0] } else { 'list' }
    switch ($sub) {
      'list' {
        $pluginsRoot = Join-Path $repo 'plugins'
        if (-not (Test-Path $pluginsRoot)) { Write-Host 'No plugins installed. Create plugins/<name>/invoke.ps1 or invoke.py'; break }
        $pluginDirs = Get-ChildItem $pluginsRoot -Directory -ErrorAction SilentlyContinue
        if (-not $pluginDirs) { Write-Host 'No plugins found in plugins/.'; break }
        Write-Host "`nInstalled plugins:" -ForegroundColor Cyan
        foreach ($p in $pluginDirs) {
          $hasPs  = Test-Path "$($p.FullName)\invoke.ps1"
          $hasPy  = Test-Path "$($p.FullName)\invoke.py"
          $type   = if ($hasPs) { 'ps1' } elseif ($hasPy) { 'py' } else { '?' }
          $descFile = Join-Path $p.FullName 'description.txt'
          $desc = if (Test-Path $descFile) { (Get-Content $descFile -Raw).Trim() } else { '' }
          Write-Host ("  bob {0,-15} [{1}]  {2}" -f $p.Name, $type, $desc)
        }
        Write-Host ''
      }
      default { Write-Host 'Usage: bob plugins list' }
    }
  }

  default {
    # Plugin fallback — run plugins/<cmd>/invoke.ps1 or invoke.py if it exists
    $pluginDir = Join-Path $repo "plugins\$cmd"
    if (Test-Path "$pluginDir\invoke.ps1") {
      & "$pluginDir\invoke.ps1" @rest
      break
    } elseif (Test-Path "$pluginDir\invoke.py") {
      $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy "$pluginDir\invoke.py" @rest
      $env:PYTHONIOENCODING = $null
      break
    }

    # WI-7 — one generated catalog: `help` and any unknown verb render from the command registry via
    # `python -m bob help` (the old hand-maintained here-string is retired). Falls back to a one-liner
    # only if the venv is missing (pre-bootstrap).
    $venvPy = Join-Path $repo 'tools\venv-litellm\Scripts\python.exe'
    if (Test-Path $venvPy) {
      if ($cmd -ne 'help') { Write-Host "Unknown command: $cmd`n" -ForegroundColor Yellow }
      try { Get-BobConfig | Out-Null } catch {}
      $env:PYTHONPATH = Join-Path $repo 'scripts'
      $env:PYTHONIOENCODING = 'utf-8'
      & $venvPy -m bob help
    } else {
      Write-Host "bob — run scripts\bootstrap-litellm.ps1 to set up, then 'bob help'."
    }
    break
  }
}
