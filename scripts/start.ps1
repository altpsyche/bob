#requires -Version 7
# Launch the llama-swap endpoint (OpenAI-compatible) on the configured port.
$ErrorActionPreference = "Stop"
$repo   = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"   # brings in the NC OS-aware seams (Get-BinExe / Get-VenvExe) via _platform.ps1
$swap   = Get-BinExe 'llama-swap'                     # bin/llama-swap(.exe) — OS-aware
$config = Join-Path $repo 'config' 'llama-swap.yaml'
$port = (Get-ModelsConfig).defaults.port ?? (Get-BobPortDefault 'port')

if (-not (Test-Path $swap))   { throw "$(Split-Path $swap -Leaf) missing. Run scripts/build-llama-swap.ps1 (or drop the release binary in bin/)." }
# Regenerate the runtime config from the single source (config/models.psd1) so edits /
# profile switches always take effect, and a fresh clone always has a config. Sub-second.
& "$PSScriptRoot\gen-llama-swap.ps1"
& "$PSScriptRoot\gen-litellm.ps1"

# Auto-start LiteLLM proxy in background so clients on :8081 work immediately.
$litellmExe = Get-VenvExe -Venv 'venv-litellm' -Exe 'litellm'   # NC4: Scripts\litellm.exe | bin/litellm
if (Test-Path $litellmExe) { & "$PSScriptRoot\start-litellm.ps1" -NoWindow }
else { Write-Host "LiteLLM venv not found — skipping proxy. Run scripts/bootstrap-litellm.ps1" -ForegroundColor DarkGray }

# Auto-start whisper STT server if voice is enabled in bob.psd1.
$whisperExe = Get-BinExe 'whisper-server'
$bobCfg     = Get-BobConfig
if ($bobCfg.voice.enabled -and (Test-Path $whisperExe)) {
    & "$PSScriptRoot\start-whisper.ps1" -NoWindow
} elseif ($bobCfg.voice.enabled) {
    Write-Host "voice.enabled = `$true but $(Split-Path $whisperExe -Leaf) missing — run: bob setup-voice" -ForegroundColor Yellow
}

if (Test-PortInUse -Port $port) {
  Write-Warning "Port $port already in use — the endpoint is probably already running ('bob stop' to free it)."; return
}

# Expose the repo root to the config so model paths relocate with the clone (see config\llama-swap.yaml).
$env:LLAMA_LOCAL_ROOT = ($repo -replace '\\','/')

$logsDir = Join-Path $repo 'logs'
if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory $logsDir | Out-Null }
$logFile = Join-Path $logsDir 'llama-swap.log'

Write-Host "Endpoint: http://localhost:$port/v1   (loopback only; Ctrl+C to stop)" -ForegroundColor Green
Write-Host "Log: $logFile"

# Tee-Object without -Append truncates on each start (one clean log per run)
& $swap --config $config --listen "127.0.0.1:$port" 2>&1 | Tee-Object -FilePath $logFile
