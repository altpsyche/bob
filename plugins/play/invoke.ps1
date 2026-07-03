#requires -Version 7
# bob play — play music via Spotify or YouTube (direct video, not search page).
#
# Usage:
#   bob play lofi hip hop
#   bob play pink floyd dark side of the moon
#   bob play --youtube arctic monkeys    (force YouTube)
#   bob play --spotify jazz              (force Spotify)

$youtube  = $args -contains '--youtube'
$spotify  = $args -contains '--spotify'
$query    = ($args | Where-Object { $_ -notin @('--youtube', '--spotify') }) -join ' '

if (-not $query) {
    Write-Host "Usage: bob play <search query>"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  bob play lofi hip hop"
    Write-Host "  bob play pink floyd dark side of the moon"
    Write-Host "  bob play --youtube arctic monkeys"
    Write-Host "  bob play --spotify jazz for coding"
    exit 0
}

$repo   = Split-Path (Split-Path $PSScriptRoot)
# OS-aware venv layout (Windows Scripts\python.exe | Linux/macOS bin/python); plugins are standalone
# so resolve inline rather than dot-sourcing the platform seam.
$venvPy = if ($IsWindows) { Join-Path $repo 'tools\venv-litellm\Scripts\python.exe' }
          else            { Join-Path $repo 'tools/venv-litellm/bin/python' }

if (-not (Test-Path $venvPy)) {
    Write-Host "venv-litellm not found — run bootstrap-litellm.ps1 first" -ForegroundColor Red
    exit 1
}

$platform = if ($youtube) { 'youtube' } elseif ($spotify) { 'spotify' } else { 'auto' }

$env:PYTHONIOENCODING = 'utf-8'
$result = & $venvPy -c "
import sys
sys.path.insert(0, r'$repo\scripts\tools')
sys.path.insert(0, r'$repo\scripts')
import music
try:
    from bob_core import load_config
    music.configure(load_config())
except Exception:
    music.configure({})
print(music._music_play(r'$query', '$platform'))
" 2>$null
$env:PYTHONIOENCODING = $null

if ($result) {
    Write-Host $result -ForegroundColor Green
} else {
    Write-Host "music_play failed — check SearXNG and venv" -ForegroundColor Red
    exit 1
}
