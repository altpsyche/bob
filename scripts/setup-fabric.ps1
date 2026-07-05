#requires -Version 7
# Build fabric from the external/fabric submodule and configure it for the local endpoint.
# fabric pipes text through 200+ named prompt patterns: git diff | fabric --pattern write_git_commit
#
# Run once after cloning:  bob fabric-setup
# Re-run to reconfigure or after a submodule update.
# Pass -Force to rebuild the fabric binary in bin/ even if it already exists.
param([switch]$Force)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_models.ps1"   # NC1 seam: Get-BobExeName, Get-HomeConfigDir, Get-ModelsConfig, Get-BobPortDefault
$src  = Join-Path $repo 'external\fabric'
$out  = Join-Path $repo (Join-Path 'bin' (Get-BobExeName 'fabric'))   # bin/fabric.exe (win) | bin/fabric (linux)

# 1. Ensure submodule is initialised
if (-not (Test-Path (Join-Path $src 'go.mod'))) {
    Write-Host "Initialising external/fabric submodule..." -ForegroundColor Cyan
    git -C $repo submodule update --init --depth=1 external/fabric
    if ($LASTEXITCODE -ne 0) { throw "git submodule update failed for external/fabric" }
}

# 2. Build fabric binary (Go is a bootstrap.ps1 prereq)
if ($Force -or -not (Test-Path $out)) {
    Write-Host "Building fabric..." -ForegroundColor Cyan
    Push-Location $src
    try {
        go build -o $out ./cmd/fabric/
        if ($LASTEXITCODE -ne 0) { throw "go build failed for fabric" }
    } finally { Pop-Location }
    Write-Host "  -> $out" -ForegroundColor Green
} else {
    Write-Host "$out already built — skipping (pass -Force to rebuild)." -ForegroundColor DarkGray
}

# 3. Write ~/.config/fabric/.env (endpoint config)
$cfg  = Get-ModelsConfig
$port = $cfg.defaults.litellmPort ?? (Get-BobPortDefault 'litellmPort')

$fabricDir = Get-HomeConfigDir -App 'fabric'   # NC5: %USERPROFILE%\.config\fabric | ~/.config/fabric
if (-not (Test-Path $fabricDir)) { New-Item -ItemType Directory -Force $fabricDir | Out-Null }

@"
OPENAI_API_KEY=sk-local
OPENAI_API_BASE_URL=http://localhost:$port/v1
DEFAULT_VENDOR=OpenAI
DEFAULT_MODEL=coder
"@ | Set-Content (Join-Path $fabricDir '.env') -Encoding utf8
Write-Host "Configured: coder @ http://localhost:$port/v1" -ForegroundColor Green

# 4. Symlink patterns from submodule (no download needed — always in sync with pinned commit)
$patternsLink = Join-Path $fabricDir 'patterns'
$patternsTarget = Join-Path $src 'data\patterns'

if (Test-Path $patternsLink) {
    $linkItem = Get-Item $patternsLink -ErrorAction SilentlyContinue
    if ($linkItem -and $linkItem.LinkType -ne 'SymbolicLink') {
        Write-Host "patterns already copied — re-run with -Force (after deleting $patternsLink) to refresh after submodule bumps." -ForegroundColor Yellow
    } else {
        Write-Host "patterns link already exists — skipping." -ForegroundColor DarkGray
    }
} else {
    try {
        New-Item -ItemType SymbolicLink -Path $patternsLink -Target $patternsTarget -ErrorAction Stop | Out-Null
        $count = (Get-ChildItem $patternsTarget -Directory).Count
        Write-Host "Linked patterns: $patternsLink -> $patternsTarget ($count patterns)" -ForegroundColor Green
    } catch {
        # No symlink privilege — copy instead
        Copy-Item $patternsTarget $patternsLink -Recurse -Force
        $count = (Get-ChildItem $patternsLink -Directory).Count
        Write-Host "Copied patterns to $patternsLink ($count patterns)" -ForegroundColor Yellow
        Write-Host "  (enable Windows Developer Mode for symlinks so git pull updates patterns automatically)" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "Usage examples:" -ForegroundColor Cyan
Write-Host "  git diff --staged | fabric --pattern write_git_commit"
Write-Host "  cat error.log     | fabric --pattern explain"
Write-Host "  cat notes.txt     | fabric --pattern summarize"
Write-Host "  fabric --listpatterns               # show all 200+ patterns"
Write-Host "  fabric --model planner --pattern X  # use planner for complex tasks"
