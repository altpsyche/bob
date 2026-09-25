#!/usr/bin/env bash
# Tier-1 shell stub. Thin bootstrapper: ensure python3 is present (install_prereqs
# put it there), then hand off to the Python cold-start kernel (submodules -> engine -> venvs -> gen
# configs -> fetch models -> wire clients). Zero PowerShell. Run after ./install_prereqs.sh.
# Idempotent, safe to re-run.
#
#   ./setup.sh                    # full (prebuilt GPU engine on NVIDIA, else the CPU tier)
#   ./setup.sh --skip-models      # skip the model downloads
#   ./setup.sh --profile cpu      # force the tiny CPU profile
#   ./setup.sh --with-aider       # also install aider (opt-in; later: bob aider-setup)
#   ./setup.sh --with-fabric      # also build fabric (opt-in, needs Go; later: bob fabric-setup)
#   ./setup.sh --launch           # start the stack when finished
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$SCRIPT_DIR/scripts${PYTHONPATH:+:$PYTHONPATH}"

# version-stamp: state which Bob release this blessed entry belongs to.
echo "[setup] Bob $(cat "$SCRIPT_DIR/VERSION" 2>/dev/null || echo '?') — setup"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[setup] python3 not found. Run ./install_prereqs.sh first (it installs Python)."
  exit 1
fi

exec python3 -m bob.kernel setup "$@"
