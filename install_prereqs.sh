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

PWSH_VERSION="7.4.6"
# Distro-agnostic pwsh install from the official linux tarball — the universal LAST resort when the
# package-manager path can't deliver pwsh: Arch has no AUR helper (CI images), and openSUSE's rpm from
# Microsoft's RHEL repo wants a RHEL-named 'openssl-libs' that doesn't exist there. Best-effort ICU
# (pwsh's .NET globalization dep) per manager first. Never fatal on its own.
pwsh_tarball() {
  if command -v pwsh >/dev/null 2>&1; then return 0; fi
  log "Installing pwsh $PWSH_VERSION from the official tarball (distro-agnostic)..."
  if   command -v pacman  >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm --needed icu tar || true
  elif command -v zypper  >/dev/null 2>&1; then $SUDO zypper --non-interactive install libicu tar gzip || true
  elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf install -y libicu tar || true
  elif command -v apt-get >/dev/null 2>&1; then $SUDO apt-get install -y libicu-dev tar || true
  fi
  local m; m="$(uname -m)"
  case "$m" in x86_64|amd64) m=x64 ;; aarch64|arm64) m=arm64 ;; esac
  local url="https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-${m}.tar.gz"
  local tgz="/tmp/powershell-${PWSH_VERSION}.tar.gz"
  if   command -v curl >/dev/null 2>&1; then curl -fsSL "$url" -o "$tgz" || return 1
  elif command -v wget >/dev/null 2>&1; then wget -q "$url" -O "$tgz"    || return 1
  else return 1; fi
  $SUDO mkdir -p /opt/microsoft/powershell/7
  $SUDO tar zxf "$tgz" -C /opt/microsoft/powershell/7
  $SUDO chmod +x /opt/microsoft/powershell/7/pwsh
  $SUDO ln -sf /opt/microsoft/powershell/7/pwsh /usr/bin/pwsh
  rm -f "$tgz"
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
      # Nested if (not `A && B && C || D`): shellcheck SC2015 flags that as not-if-then-else, and the
      # post-case tarball fallback is the real safety net if any step here fails.
      if [ -n "$ver" ] && wget -q "https://packages.microsoft.com/config/${id}/${ver}/packages-microsoft-prod.deb" -O "$deb"; then
        if $SUDO dpkg -i "$deb" && $SUDO apt-get update -y; then
          $SUDO apt-get install -y powershell || true
        fi
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
      # openSUSE: Microsoft's RHEL rpm depends on a RHEL-named 'openssl-libs' that openSUSE doesn't
      # provide, so that repo can't install pwsh here — go straight to snap, then the tarball fallback
      # below (the tarball is the method that actually works on openSUSE). curl for the tarball fetch.
      $SUDO zypper --non-interactive install curl || true
      snap_fallback
      ;;
    *)
      snap_fallback
      ;;
  esac
  # Universal last resort: if the manager-specific path + snap didn't produce pwsh, use the official
  # tarball (covers Arch-without-AUR and openSUSE). Only the error below is fatal.
  if ! command -v pwsh >/dev/null 2>&1; then pwsh_tarball || true; fi
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
