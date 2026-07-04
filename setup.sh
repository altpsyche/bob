#!/usr/bin/env bash
# NC2 — Linux setup bootstrap for Bob. Thin bootstrapper (mirrors setup.bat): ensure pwsh is present,
# then hand off to the OS-aware scripts/setup.ps1 (build llama.cpp -> venvs + tools -> fetch models ->
# wire clients). Run after ./install_prereqs.sh. Idempotent — safe to re-run.
#
#   ./setup.sh                    # full (GPU build if CUDA present, else CPU tier)
#   ./setup.sh -SkipModels        # skip the model downloads
#   ./setup.sh -Profile cpu       # force the tiny CPU profile
#   ./setup.sh -Launch            # start the stack when finished
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ND4 — version-stamp: state which Bob release this blessed entry belongs to.
echo "[setup] Bob $(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo '?') — setup"

if ! command -v pwsh >/dev/null 2>&1; then
  echo "[setup] pwsh not found. Run ./install_prereqs.sh first (it installs PowerShell 7)."
  exit 1
fi

exec pwsh -NoProfile -File "$SCRIPT_DIR/scripts/setup.ps1" "$@"
