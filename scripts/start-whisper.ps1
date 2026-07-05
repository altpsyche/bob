#requires -Version 7
# Start the whisper.cpp STT server.
# Foreground (default): shows logs in this terminal, Ctrl+C to stop.
# Background (-NoWindow): starts hidden, logs to logs/whisper.log, PID to logs/whisper.pid.
param([switch]$NoWindow)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"
$exe  = Get-BinExe -Base 'whisper-server'   # NC4: bin\whisper-server.exe | bin/whisper-server
$bobCfg  = Get-BobConfig
$sttPort = $bobCfg.voice.sttPort ?? (Get-BobPortDefault 'sttPort')
$mdl     = Join-Path $repo "models\whisper\ggml-$($bobCfg.voice.sttModel ?? 'small').bin"

if (-not (Test-Path $exe)) {
    throw "whisper-server binary not found ($exe) — run: bob setup-voice"
}
if (-not (Test-Path $mdl)) {
    throw "Whisper model not found at $mdl — run: bob setup-voice"
}

# Kill any stale whisper-server.exe processes before starting (prevents VRAM leak from orphaned procs)
$stale = Get-Process -Name 'whisper-server' -ErrorAction SilentlyContinue
if ($stale) {
    $stale | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
}
$pidFile = Join-Path $repo 'logs\whisper.pid'
Remove-Item $pidFile -ErrorAction SilentlyContinue

if ($NoWindow) {
    $logsDir = Join-Path $repo 'logs'
    if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory $logsDir | Out-Null }
    $logFile = Join-Path $logsDir 'whisper.log'
    $cmd = "& '$exe' --model '$mdl' --port $sttPort --host 127.0.0.1 2>&1 | Tee-Object -FilePath '$logFile'"
    $launchPid = Start-BobBackgroundProcess -ArgList @("-NonInteractive", "-Command", $cmd) -PidFile $pidFile
    Write-Host "whisper-server: http://localhost:$sttPort   (PID $launchPid)" -ForegroundColor Green
    Write-Host "Logs: logs/whisper.log   Stop: bob whisper stop" -ForegroundColor DarkGray

    # Poll for readiness — GPU load takes ~5s, allow up to 30s
    $ready = $false
    Write-Host -NoNewline "  Loading model..." -ForegroundColor DarkGray
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $tcp = [Net.Sockets.TcpClient]::new('127.0.0.1', $sttPort)
            $tcp.Close()
            $ready = $true
            break
        } catch {}
    }
    if ($ready) { Write-Host " ready." -ForegroundColor Green }
    else { Write-Host ""; Write-Warning "whisper-server may not be ready yet on port $sttPort — check logs/whisper.log" }
} else {
    Write-Host "whisper-server starting on http://localhost:$sttPort (Ctrl+C to stop)" -ForegroundColor Cyan
    & $exe --model $mdl --port $sttPort --host 127.0.0.1
}
