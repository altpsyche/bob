#!/usr/bin/env sh
# Bob one-command installer (Linux). Fetched and piped to a shell:
#
#   curl -fsSL https://get.bob.sh | sh
#
# It clones Bob (with submodules) into $BOB_HOME (default ~/bob), runs the prereq + setup steps, then
# verifies the result against versions.lock. Idempotent: a re-run fast-forwards an existing clone instead
# of recloning. macOS support arrives in Bob 2.0; Linux and Windows ship in 1.1.
#
# Overrides (env): BOB_HOME (install dir), BOB_REPO_URL (git source). Pass --cpu for the CPU-only tier.
set -eu

# The git source. get.bob.sh is intended to front the GitHub raw path for this file (see README); until
# it does, fetch this script directly from raw.githubusercontent.com/altpsyche/bob/main/install/install.sh.
# BOB_REPO_URL can be overridden for forks/testing.
BOB_REPO_URL="${BOB_REPO_URL:-https://github.com/altpsyche/bob.git}"
BOB_HOME="${BOB_HOME:-$HOME/bob}"

log() { printf '[bob-install] %s\n' "$*"; }

# macOS is out of scope for 1.1 (Metal backend lands in 2.0).
if [ "$(uname -s)" = "Darwin" ]; then
  log "macOS support arrives in Bob 2.0. Use Linux or Windows for now."
  exit 1
fi

# Ensure git (one package-manager call), mirroring install_prereqs.sh's python3 bootstrap.
if ! command -v git >/dev/null 2>&1; then
  log "git not found - installing it via the system package manager..."
  if [ "$(id -u)" -eq 0 ]; then SUDO="";
  elif command -v sudo >/dev/null 2>&1; then SUDO="sudo";
  else log "ERROR: not root and 'sudo' is missing. Re-run as root or install sudo first."; exit 1; fi
  if   command -v apt-get    >/dev/null 2>&1; then $SUDO apt-get update -y && $SUDO apt-get install -y git
  elif command -v dnf        >/dev/null 2>&1; then $SUDO dnf install -y git
  elif command -v pacman     >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm --needed git
  elif command -v zypper     >/dev/null 2>&1; then $SUDO zypper --non-interactive install git
  elif command -v rpm-ostree >/dev/null 2>&1; then $SUDO rpm-ostree install --idempotent --allow-inactive git
  else log "No supported package manager (apt/dnf/pacman/zypper/rpm-ostree). Install git, then re-run."; exit 1; fi
fi

# Clone with submodules, or fast-forward an existing clone (idempotent re-run).
if [ -d "$BOB_HOME/.git" ]; then
  log "Updating existing Bob at $BOB_HOME ..."
  git -C "$BOB_HOME" pull --ff-only || log "(pull skipped: local changes - keeping the current tree)"
  git -C "$BOB_HOME" submodule update --init --recursive
else
  log "Cloning Bob into $BOB_HOME ..."
  git clone --recurse-submodules "$BOB_REPO_URL" "$BOB_HOME"
fi

cd "$BOB_HOME"

# Forward only --cpu to the prereq step (its argparse accepts nothing else); pass all args to setup.
CPU=""
for a in "$@"; do [ "$a" = "--cpu" ] && CPU="--cpu"; done

log "Installing prerequisites ..."
# shellcheck disable=SC2086
./install_prereqs.sh $CPU
log "Running setup ..."
./setup.sh "$@"
log "Verifying against versions.lock ..."
# scripts/ on PYTHONPATH so `python -m bob.kernel` resolves (the stubs set this internally; it does not
# persist back to this shell).
export PYTHONPATH="$BOB_HOME/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 -m bob.kernel verify-install || log "(verify reported drift - see the lines above)"

log "Done. Start Bob with:  bob"
