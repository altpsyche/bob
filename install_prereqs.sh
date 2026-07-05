#!/usr/bin/env bash
# NC2 — Linux prereq bootstrap for Bob. Thin bootstrapper (mirrors install_prereqs.bat): install
# PowerShell 7 (pwsh) if absent, then hand off to the OS-aware scripts/install-prereqs.ps1, which
# installs the toolchain (compiler, cmake, ninja, go, node, python3) via apt/dnf/pacman/zypper.
# Idempotent — safe to re-run.
#
#   ./install_prereqs.sh          # GPU build (expects NVIDIA driver + CUDA toolkit)
#   ./install_prereqs.sh --cpu    # CPU-only tier (skips the CUDA toolkit)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[install_prereqs] %s\n' "$*"; }

# Root vs sudo: work on root containers/minimal installs (no sudo) as well as normal desktops.
# $SUDO is "" when already root, "sudo" otherwise; error loudly if we're non-root with no sudo.
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  log "ERROR: not running as root and 'sudo' is not installed. Re-run as root, or install sudo first."
  exit 1
fi

# ND4 — version-stamp: state which Bob release this blessed entry belongs to.
log "Bob $(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo '?') — prerequisite install"

detect_mgr() {
  for m in apt-get dnf pacman zypper; do
    if command -v "$m" >/dev/null 2>&1; then echo "$m"; return; fi
  done
  echo ""
}

# Best-effort pwsh via snap; NEVER fatal (the `|| true` keeps `set -e` from aborting the whole script
# when snap is absent or the install fails — the loud manual-guidance path at the end handles that).
snap_fallback() {
  if command -v snap >/dev/null 2>&1; then
    log "Falling back to snap for pwsh..."
    $SUDO snap install powershell --classic || true
  fi
  return 0
}

install_pwsh() {
  if command -v pwsh >/dev/null 2>&1; then log "pwsh ok"; return; fi
  local mgr="$1"
  log "Installing PowerShell 7 (pwsh) via $mgr ..."
  case "$mgr" in
    apt-get)
      $SUDO apt-get update -y
      $SUDO apt-get install -y wget apt-transport-https
      # shellcheck disable=SC1091
      . /etc/os-release
      # Rolling/testing apt distros can omit VERSION_ID; default it so `set -u` doesn't abort here.
      local id="${ID:-debian}" ver="${VERSION_ID:-}"
      local deb="/tmp/packages-microsoft-prod.deb"
      if [ -n "$ver" ] && wget -q "https://packages.microsoft.com/config/${id}/${ver}/packages-microsoft-prod.deb" -O "$deb"; then
        $SUDO dpkg -i "$deb" && $SUDO apt-get update -y && $SUDO apt-get install -y powershell || snap_fallback
      else
        snap_fallback
      fi
      ;;
    dnf)
      curl -fsSL https://packages.microsoft.com/config/rhel/9/prod.repo | $SUDO tee /etc/yum.repos.d/microsoft.repo >/dev/null || snap_fallback
      $SUDO dnf install -y powershell || snap_fallback
      ;;
    pacman)
      # pwsh is not in Arch's official repos — it's in the AUR (powershell-bin). Use an AUR helper if
      # one is present; otherwise fall back to snap, else print manual guidance. AUR helpers refuse root.
      if command -v paru >/dev/null 2>&1; then
        paru -S --noconfirm powershell-bin || snap_fallback
      elif command -v yay >/dev/null 2>&1; then
        yay -S --noconfirm powershell-bin || snap_fallback
      else
        log "pwsh is in the AUR (powershell-bin) — no AUR helper (paru/yay) found. Trying snap; otherwise install pwsh manually and re-run."
        snap_fallback
      fi
      ;;
    zypper)
      # openSUSE is rpm-based, so Microsoft's RHEL prod repo provides pwsh. Best-effort; the
      # opensuse/tumbleweed CI cell validates this path. Falls back to snap, then manual guidance.
      $SUDO zypper --non-interactive install curl || true
      $SUDO rpm --import https://packages.microsoft.com/keys/microsoft.asc 2>/dev/null || true
      if $SUDO zypper --non-interactive addrepo --refresh https://packages.microsoft.com/rhel/9.0/prod/ microsoft-prod 2>/dev/null &&
         $SUDO zypper --non-interactive --gpg-auto-import-keys refresh; then
        $SUDO zypper --non-interactive install powershell || snap_fallback
      else
        snap_fallback
      fi
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
  log "No supported package manager (apt/dnf/pacman/zypper) found. See docs/MANUAL-INSTALL.md."
  exit 1
fi
install_pwsh "$MGR"

log "Handing off to scripts/install-prereqs.ps1 ..."
exec pwsh -NoProfile -File "$SCRIPT_DIR/scripts/install-prereqs.ps1" "$@"
