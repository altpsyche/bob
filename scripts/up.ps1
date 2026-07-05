#requires -Version 7
# Start endpoint + Open WebUI silently in the background (no terminal popups).
# Use 'bob serve' for interactive/foreground mode with live log output.
param([switch]$NoOpen, [switch]$WithServices)
$ErrorActionPreference = "Stop"
$repo  = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"
$webui = Get-VenvExe -Venv 'venv-webui' -Exe 'open-webui'   # NC4: OS-aware venv path
$cfg        = Get-ModelsConfig
$port        = $cfg.defaults.port ?? (Get-BobPortDefault 'port')
$litellmPort = $cfg.defaults.litellmPort ?? (Get-BobPortDefault 'litellmPort')
$webuiPort   = $cfg.defaults.webuiPort ?? (Get-BobPortDefault 'webuiPort')
$secret     = $cfg.defaults.webuiSecret ?? 'bob-dev'
$logsDir    = Join-Path $repo 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory $logsDir | Out-Null }

if (Test-PortInUse -Port $port) {
  Write-Warning "Port $port already in use — endpoint may already be running ('bob stop' to free it)."; return
}

# 1) Endpoint — detached background process (NC4 seam), logs to logs/llama-swap.log via start.ps1's Tee-Object
$swapId = Start-BobBackgroundProcess `
    -ArgList @("-NonInteractive", "-File", "`"$repo\scripts\start.ps1`"") `
    -PidFile (Join-Path $logsDir 'llama-swap.pid')
Write-Host "Endpoint:   http://localhost:$port/v1   (PID $swapId)" -ForegroundColor Green
Write-Host "            logs: bob logs" -ForegroundColor DarkGray

$spin = [char[]]@('|','/','-','\')
$sw   = [Diagnostics.Stopwatch]::StartNew()
$i    = 0; $up = $false
while ($sw.Elapsed.TotalSeconds -lt 60) {
    try { Invoke-RestMethod "http://localhost:$port/v1/models" -ErrorAction Stop | Out-Null; $up = $true; break }
    catch {}
    if (-not (Get-Process -Id $swapId -ErrorAction SilentlyContinue)) { Write-Warning "Endpoint process exited. Check: bob logs"; break }
    Write-Host "`r  $($spin[$i++ % 4]) Starting endpoint..." -NoNewline -ForegroundColor DarkGray
    Start-Sleep -Milliseconds 200
}
if ($up) { Write-Host "`r  Endpoint ready ($([int]$sw.Elapsed.TotalSeconds)s)              " -ForegroundColor Green }
else      { Write-Warning "Endpoint did not respond in 60s. Check: bob logs" }

# 2) LiteLLM proxy — routes all clients through :8081 (local + pro models)
$litellmExe = Get-VenvExe -Venv 'venv-litellm' -Exe 'litellm'
if (Test-Path $litellmExe) { & "$PSScriptRoot\start-litellm.ps1" -NoWindow }
else { Write-Host "LiteLLM venv not found — skipping proxy. Run scripts\bootstrap-litellm.ps1" -ForegroundColor DarkGray }

# 2b) Whisper STT server — auto-start when voice.enabled = $true and binary is present
$bobVoice = try { (Get-BobConfig).voice } catch { $null }
$whisperBin = Get-BinExe -Base 'whisper-server'
if ($bobVoice.enabled -and (Test-Path $whisperBin)) {
    & "$PSScriptRoot\start-whisper.ps1" -NoWindow
    Write-Host "Whisper STT: http://localhost:$($bobVoice.sttPort ?? (Get-BobPortDefault 'sttPort'))   (voice enabled)" -ForegroundColor Green
}

# 3) Open WebUI — hidden window, log to logs/open-webui.log
if (Test-Path $webui) {
  $owEnv = @(
    "`$env:OPENAI_API_BASE_URL='http://localhost:$litellmPort/v1';",
    "`$env:OPENAI_API_KEY='sk-local';",
    "`$env:RAG_EMBEDDING_ENGINE='openai';",
    "`$env:RAG_OPENAI_API_BASE_URL='http://localhost:$litellmPort/v1';",
    "`$env:RAG_OPENAI_API_KEY='sk-local';",
    "`$env:RAG_EMBEDDING_MODEL='embed';",
    # keep ALL Open WebUI state inside the (gitignored) repo data dir, not scattered in CWD.
    # Join-Path (not "$repo\..."): the value is consumed by open-webui (Python), which doesn't
    # normalize backslashes — a literal-backslash path scatters state into a bogus dir on Linux.
    "`$env:DATA_DIR='$(Join-Path $repo 'tools' 'webui-data')';",
    "`$env:WEBUI_SECRET_KEY='$secret';"
  ) -join ""
  $uiLog = Join-Path $logsDir 'open-webui.log'
  $uiCmd = "$owEnv & '$webui' serve --port $webuiPort 2>&1 | Tee-Object -FilePath '$uiLog'"
  $uiId = Start-BobBackgroundProcess `
      -ArgList @("-NonInteractive", "-Command", $uiCmd) `
      -PidFile (Join-Path $logsDir 'open-webui.pid')
  Write-Host "Open WebUI: http://localhost:$webuiPort   (PID $uiId)" -ForegroundColor Green
  if (-not $NoOpen) {
    $sw2 = [Diagnostics.Stopwatch]::StartNew(); $j = 0; $uiUp = $false
    while ($sw2.Elapsed.TotalSeconds -lt 120) {
        # TCP check: just verify the port is listening (avoids HTTP status-code false failures)
        try {
            $tcp = [System.Net.Sockets.TcpClient]::new()
            $tcp.Connect('127.0.0.1', $webuiPort)
            $tcp.Close()
            $uiUp = $true; break
        } catch {}
        # Bail early if the host process died
        if (-not (Get-Process -Id $uiId -ErrorAction SilentlyContinue)) {
            Write-Warning "Open WebUI process exited. Check: bob logs"; break
        }
        Write-Host "`r  $($spin[$j++ % 4]) Starting Open WebUI..." -NoNewline -ForegroundColor DarkGray
        Start-Sleep -Milliseconds 500
    }
    if ($uiUp) {
        Write-Host "`r  Open WebUI ready ($([int]$sw2.Elapsed.TotalSeconds)s)           " -ForegroundColor Green
        # NC4 — open the browser OS-appropriately (Start-Process URL is a Windows shell-verb thing).
        $uiUrl = "http://localhost:$webuiPort"
        if ((Get-BobOS) -eq 'windows') { Start-Process $uiUrl }
        elseif (Get-Command xdg-open -ErrorAction SilentlyContinue) { & xdg-open $uiUrl 2>$null }
    } else { Write-Warning "Open WebUI didn't respond. Open manually: http://localhost:$webuiPort" }
  }
} else {
  Write-Warning "open-webui not found — run scripts\bootstrap.ps1 first. Skipping Open WebUI."
}
if ($WithServices) {
  if (Get-Command docker -ErrorAction SilentlyContinue) {
    Write-Host "Starting Docker services..." -ForegroundColor Cyan
    $compose  = Join-Path $repo 'tools' 'compose' 'docker-compose.yml'   # Join-Path: path goes to native docker
    $envFile  = Join-Path $repo 'tools' 'compose' '.env'
    @"
REPO_PATH=$repo
LANGFUSE_PORT=$($cfg.defaults.langfusePort ?? (Get-BobPortDefault 'langfusePort'))
SEARXNG_PORT=$($cfg.defaults.searxngPort ?? (Get-BobPortDefault 'searxngPort'))
N8N_PORT=$($cfg.defaults.n8nPort ?? (Get-BobPortDefault 'n8nPort'))
"@ | Set-Content $envFile -Encoding utf8
    docker compose -f $compose up -d 2>$null
    Write-Host "Services started:" -ForegroundColor Green
    docker compose -f $compose ps --format json 2>$null | ConvertFrom-Json | ForEach-Object {
        $state = if ($_.Health) { $_.Health } else { $_.State }
        $color = if ($state -eq 'healthy') { 'Green' } else { 'DarkGray' }
        Write-Host ("  {0,-40} {1}" -f $_.Name, $state) -ForegroundColor $color
    }
  } else {
    Write-Warning "-WithServices: Docker not found. Run .\scripts\setup-docker.ps1 first."
  }
}
Write-Host "clients: http://localhost:$litellmPort/v1   aider: bob aider   stop: bob stop   logs: bob logs" -ForegroundColor DarkGray
