#requires -Version 7
# Build the llama-swap submodule (Go) into bin/ (cross-platform: llama-swap.exe on Windows, llama-swap
# on Linux/macOS). Fallback if you don't want Go: download the matching native binary from
#   https://github.com/mostlygeek/llama-swap/releases  into  bin/
param([switch]$Force)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot -Parent
. "$PSScriptRoot\_platform.ps1"          # NC seam: Get-BinExe / Get-BobExeName (OS-aware names)
$src = Join-Path $repo "external" "llama-swap"
$bin = Join-Path $repo "bin"
$out = Get-BinExe 'llama-swap'           # bin/llama-swap(.exe)

if (-not $Force -and (Test-Path $out)) {
  Write-Host "$(Split-Path $out -Leaf) already built — skipping (use -Force to rebuild)." -ForegroundColor DarkGray
  return
}

if (-not (Test-Path $src)) {
  throw "llama-swap submodule not found at $src. Run: git submodule update --init --recursive"
}
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
  throw "Go not found. Install Go (via your package manager — e.g. pacman -S go / apt install golang-go / dnf install golang; scoop install go on Windows), or download the llama-swap release binary into $bin"
}

New-Item -ItemType Directory -Force -Path $bin | Out-Null
Push-Location $src
try {
  go build -o $out .
  if ($LASTEXITCODE -ne 0) { throw "go build failed" }
} finally { Pop-Location }
Write-Host "Built: $out" -ForegroundColor Green
