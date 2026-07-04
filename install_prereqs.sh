#!/usr/bin/env bash
# NC2 — Linux prereq bootstrap for Bob. Thin bootstrapper (mirrors install_prereqs.bat): install
# PowerShell 7 (pwsh) if absent, then hand off to the OS-aware scripts/install-prereqs.ps1, which
# installs the toolchain (compiler, cmake, ninja, go, node, python3) via apt/dnf/pacman.
# Idempotent — safe to re-run.
#
#   ./install_prereqs.sh          # GPU build (expects NVIDIA driver + CUDA toolkit)
#   ./install_prereqs.sh --cpu    # CPU-only tier (skips the CUDA toolkit)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[install_prereqs] %s\n' "$*"; }

# ND4 — version-stamp: state which Bob release this blessed entry belongs to.
log "Bob $(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo '?') — prerequisite install"

detect_mgr() {
  for m in apt-get dnf pacman; do
    if command -v "$m" >/dev/null 2>&1; then echo "$m"; return; fi
  done
  echo ""
}

snap_fallback() {
  if command -v snap >/dev/null 2>&1; then
    log "Falling back to snap for pwsh..."
    sudo snap install powershell --classic
  fi
}

install_pwsh() {
  if command -v pwsh >/dev/null 2>&1; then log "pwsh ok"; return; fi
  local mgr="$1"
  log "Installing PowerShell 7 (pwsh) via $mgr ..."
  case "$mgr" in
    apt-get)
      sudo apt-get update -y
      sudo apt-get install -y wget apt-transport-https
      # shellcheck disable=SC1091
      . /etc/os-release
      local deb="/tmp/packages-microsoft-prod.deb"
      if wget -q "https://packages.microsoft.com/config/${ID}/${VERSION_ID}/packages-microsoft-prod.deb" -O "$deb"; then
        sudo dpkg -i "$deb" && sudo apt-get update -y && sudo apt-get install -y powershell || snap_fallback
      else
        snap_fallback
      fi
      ;;
    dnf)
      curl -fsSL https://packages.microsoft.com/config/rhel/9/prod.repo | sudo tee /etc/yum.repos.d/microsoft.repo >/dev/null || true
      sudo dnf install -y powershell || snap_fallback
      ;;
    *)
      snap_fallback
      ;;
  esac
  if ! command -v pwsh >/dev/null 2>&1; then
    log "ERROR: pwsh install failed. Install it manually, then re-run:"
    log "  https://learn.microsoft.com/powershell/scripting/install/installing-powershell-on-linux"
    exit 1
  fi
}

MGR="$(detect_mgr)"
if [ -z "$MGR" ]; then
  log "No supported package manager (apt/dnf/pacman) found. See docs/MANUAL-INSTALL.md."
  exit 1
fi
install_pwsh "$MGR"

log "Handing off to scripts/install-prereqs.ps1 ..."
exec pwsh -NoProfile -File "$SCRIPT_DIR/scripts/install-prereqs.ps1" "$@"
