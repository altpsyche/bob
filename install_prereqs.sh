#!/usr/bin/env bash
# ONE-D Slice D8 (DD5) — Tier-0 shell stub. The ONE thin, unavoidable shell layer: ensure a system
# python3 (one package-manager call), then hand off to the Python cold-start kernel, which installs the
# toolchain (compiler, cmake, ninja, go, node, python3, CUDA) via apt/dnf/pacman/zypper. Zero PowerShell.
# Idempotent — safe to re-run.
#
#   ./install_prereqs.sh          # GPU build (expects an NVIDIA driver + CUDA toolkit)
#   ./install_prereqs.sh --cpu    # CPU-only tier (skips the CUDA toolkit)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}"

log() { printf '[install_prereqs] %s\n' "$*"; }

# ND4 — version-stamp: state which Bob release this blessed entry belongs to.
log "Bob $(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo '?') — prerequisite install"

# Root vs sudo: work on root containers/minimal installs (no sudo) as well as normal desktops.
if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
elif command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  log "ERROR: not running as root and 'sudo' is not installed. Re-run as root, or install sudo first."
  exit 1
fi

# Ensure a system python3 (one package-manager call). Everything else is the Python kernel's job.
if ! command -v python3 >/dev/null 2>&1; then
  log "python3 not found — installing it via the system package manager..."
  if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -y && $SUDO apt-get install -y python3
  elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf install -y python3
  elif command -v pacman  >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm --needed python
  elif command -v zypper  >/dev/null 2>&1; then $SUDO zypper --non-interactive install python3
  else
    log "No supported package manager (apt/dnf/pacman/zypper). Install python3 manually — see docs/MANUAL-INSTALL.md."
    exit 1
  fi
fi
if ! command -v python3 >/dev/null 2>&1; then
  log "ERROR: python3 still not found after install. Install it manually, then re-run."
  exit 1
fi

log "Handing off to python3 -m bob.kernel prereqs ..."
exec python3 -m bob.kernel prereqs "$@"
