#requires -Version 7
# Install Docker Desktop (if needed) and start the services stack (Module H).
# Ports are read from config/models.psd1 defaults (override via config/user.psd1).
#
# Services started:
#   Langfuse  — bob observability       (default :3001)
#   SearXNG   — private web search      (default :8888)
#   n8n       — workflow automation     (default :5678)
#
# Run once to install. Afterwards: bob services start|stop|status|logs
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"   # NC1 seam: Get-BobOS, Install-Package, Get-BobPortDefault, Get-ModelsConfig
$os = Get-BobOS

function Test-DockerReady {
    # $true only when the daemon actually responds. `docker info` prints the CLIENT block to stdout even
    # when the daemon is down (exit 1), so gate on the exit code — never on stdout truthiness.
    & docker info *> $null
    return ($LASTEXITCODE -eq 0)
}

# 1. Install Docker if not present. Windows: Docker Desktop via winget. Linux: the distro docker package
#    via the Install-Package seam (docker.io/docker/docker per manager), then enable the daemon.
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    if ($os -eq 'windows') {
        $dockerBin = 'C:\Program Files\Docker\Docker\resources\bin'
        if (Test-Path "$dockerBin\docker.exe") {
            $env:PATH = "$dockerBin$([IO.Path]::PathSeparator)$env:PATH"
            Write-Host "  Added Docker to PATH for this session." -ForegroundColor DarkGray
        } else {
            Write-Host "Installing Docker Desktop via winget..." -ForegroundColor Cyan
            winget install Docker.DockerDesktop --accept-package-agreements --accept-source-agreements
            Write-Warning @"
Docker Desktop installed.
ACTION REQUIRED: Log out and back in (or restart), then re-run:
    .\scripts\setup-docker.ps1
"@
            return
        }
    } else {
        Write-Host "Installing Docker via the system package manager..." -ForegroundColor Cyan
        try { Install-Package -Package 'docker' }
        catch { throw "Docker install failed: $_`nInstall Docker manually (see docs/), then re-run: ./scripts/setup-docker.ps1" }
        if (Get-Command systemctl -ErrorAction SilentlyContinue) {
            & sudo systemctl enable --now docker 2>$null
        }
        Write-Warning "Docker installed. For non-root use: 'sudo usermod -aG docker `$USER' then re-login. Continuing (this run may need sudo/root for the daemon)."
    }
}

# 2. Wait for the Docker daemon. Windows: launch Docker Desktop. Linux: systemctl start docker.
Write-Host "Checking Docker daemon..." -ForegroundColor Cyan
if (-not (Test-DockerReady)) {
    if ($os -eq 'windows') {
        $ddExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
        if (Test-Path $ddExe) { Start-Process $ddExe -WindowStyle Minimized; Write-Host "  Starting Docker Desktop..." -ForegroundColor DarkGray }
    } elseif (Get-Command systemctl -ErrorAction SilentlyContinue) {
        Write-Host "  Starting docker daemon (systemctl)..." -ForegroundColor DarkGray
        & sudo systemctl start docker 2>$null
    }
    $timeout = 90; $elapsed = 0
    while ($elapsed -lt $timeout -and -not (Test-DockerReady)) {
        Start-Sleep -Seconds 5; $elapsed += 5
        Write-Host "  Waiting... ($elapsed/$timeout s)" -ForegroundColor DarkGray
    }
}
if (-not (Test-DockerReady)) {
    $hint = if ($os -eq 'windows') { 'Launch Docker Desktop manually and re-run.' }
            else { 'Start it: sudo systemctl start docker (check: systemctl status docker), then re-run.' }
    throw "Docker daemon did not respond. $hint"
}
Write-Host "  Docker ready." -ForegroundColor Green

# 3. Read ports from models.psd1 (respects user.psd1 overrides via Get-ModelsConfig)
$d = (Get-ModelsConfig).defaults
$langfusePort = $d.langfusePort ?? (Get-BobPortDefault 'langfusePort')
$searxngPort  = $d.searxngPort  ?? (Get-BobPortDefault 'searxngPort')
$n8nPort      = $d.n8nPort      ?? (Get-BobPortDefault 'n8nPort')
$n8nTimezone  = $d.n8nTimezone  ?? 'UTC'

# 4. Write .env for docker-compose (Join-Path, not a backslash literal — the path is handed to the
#    native `docker` binary, which doesn't normalize `\` on Linux).
$envFile = Join-Path $repo 'tools' 'compose' '.env'
@"
REPO_PATH=$repo
LANGFUSE_PORT=$langfusePort
SEARXNG_PORT=$searxngPort
N8N_PORT=$n8nPort
N8N_TIMEZONE=$n8nTimezone
"@ | Set-Content $envFile -Encoding utf8
Write-Host "  Ports: Langfuse=$langfusePort  SearXNG=$searxngPort  n8n=$n8nPort  Timezone=$n8nTimezone" -ForegroundColor DarkGray

# 5. Create data directories (gitignored)
@('langfuse-data', 'n8n-data') | ForEach-Object {
    $d2 = Join-Path $repo "tools\$_"
    if (-not (Test-Path $d2)) { New-Item -ItemType Directory -Force $d2 | Out-Null }
}

# 6. Write SearXNG config if absent
$sxDir = Join-Path $repo 'config\searxng'
$sxCfg = Join-Path $sxDir 'settings.yml'
if (-not (Test-Path $sxDir)) { New-Item -ItemType Directory -Force $sxDir | Out-Null }
if (-not (Test-Path $sxCfg)) {
    @'
use_default_settings: true
server:
  secret_key: "bob-searxng"
  bind_address: "0.0.0.0:8080"
search:
  safe_search: 0
  default_lang: "en"
  formats:
    - html
    - json
'@ | Set-Content $sxCfg -Encoding utf8
}

# 7. Pull images and start stack (Join-Path — path goes to native `docker compose -f`)
$compose = Join-Path $repo 'tools' 'compose' 'docker-compose.yml'
Write-Host "Pulling images (first run may take a few minutes)..." -ForegroundColor Cyan
docker compose -f $compose pull
Write-Host "Starting services..." -ForegroundColor Cyan
docker compose -f $compose up -d

Write-Host "Waiting for containers to start..." -NoNewline -ForegroundColor DarkGray
$hcTimeout = 60; $hcElapsed = 0
while ($hcElapsed -lt $hcTimeout) {
    # State is always populated; Health is only set when HEALTHCHECK is defined.
    # Modern `docker compose ps --format json` emits NDJSON (one object per line); parse per-line so it
    # works whether the output is NDJSON or a single JSON array.
    $containers = @(docker compose -f $compose ps --format json 2>$null | Where-Object { $_ } | ForEach-Object { $_ | ConvertFrom-Json })
    $notRunning  = @($containers | Where-Object { $_.State -notin 'running','exited' })
    $unhealthy   = @($containers | Where-Object { $_.Health -eq 'starting' })
    if ($notRunning.Count -eq 0 -and $unhealthy.Count -eq 0) { break }
    Write-Host '.' -NoNewline -ForegroundColor DarkGray
    Start-Sleep -Seconds 3; $hcElapsed += 3
}
Write-Host " done" -ForegroundColor DarkGray

Write-Host ""
Write-Host "Services running:" -ForegroundColor Green
Write-Host "  Langfuse:  http://localhost:$langfusePort  (login: admin@local.dev / admin123)" -ForegroundColor Green
Write-Host "  SearXNG:   http://localhost:$searxngPort" -ForegroundColor Green
Write-Host "  n8n:       http://localhost:$n8nPort" -ForegroundColor Green
Write-Host ""
Write-Host "Manage:          bob services start|stop|status|logs" -ForegroundColor DarkGray
Write-Host "Change ports:    edit config/user.json, re-run .\scripts\setup-docker.ps1" -ForegroundColor DarkGray
Write-Host "Enable tracing:  uncomment langfuse callbacks in config/litellm.yaml" -ForegroundColor DarkGray
