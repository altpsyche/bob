#!/usr/bin/env sh
# Bob one-command installer (Linux). Fetched and piped to a shell:
#
#   curl -fsSL https://get.bob.sh | sh
#
# It clones Bob (with submodules) into $BOB_HOME (default ~/bob), runs the prereq + setup steps, then
# verifies the result against versions.lock. Idempotent: a re-run fast-forwards an existing clone instead
# of recloning. Linux here; Windows uses install/install.ps1. macOS is not supported yet (it needs a Metal
# backend).
#
# Overrides (env): BOB_HOME (install dir), BOB_REPO_URL (git source). Pass --cpu for the CPU-only tier,
# --from-source to compile the engine, and any other setup flag (--with-aider, --with-fabric, --with-webui,
# --skip-models, ...) to pass it through to setup; --with-node also reaches the prereq step.
# Channel: installs default to the 'stable' release tag (tested, carries the prebuilt driver-only engines);
# pass --dev (or --channel latest) to track the latest main instead. --dev and --channel are consumed here.
set -eu

# The git source. get.bob.sh is intended to front the GitHub raw path for this file (see README); until
# it does, fetch this script directly from raw.githubusercontent.com/altpsyche/bob/main/install/install.sh.
# BOB_REPO_URL can be overridden for forks/testing.
BOB_REPO_URL="${BOB_REPO_URL:-https://github.com/altpsyche/bob.git}"
BOB_HOME="${BOB_HOME:-$HOME/bob}"

log() { printf '[bob-install] %s\n' "$*"; }

# macOS is not supported yet (no Metal backend).
if [ "$(uname -s)" = "Darwin" ]; then
  log "macOS is not supported yet. Use Linux or Windows for now."
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

# Parse the release channel (consumed here, not passed on). Default 'stable'; --dev / '--channel latest'
# tracks main. Remaining args flow to setup; --cpu/--from-source also flow to the prereq step.
CHANNEL="stable"
SETUP_ARGS=""
skip_next=""
for a in "$@"; do
  if [ -n "$skip_next" ]; then CHANNEL="$a"; skip_next=""; continue; fi
  case "$a" in
    --dev) CHANNEL="latest" ;;
    --channel) skip_next=1 ;;
    --channel=*) CHANNEL="${a#--channel=}" ;;
    *) SETUP_ARGS="$SETUP_ARGS $a" ;;
  esac
done

# Stable channel: check out the latest release tag (which carries the prebuilt engines) so a fresh install is
# driver-only plug-and-play. `bob update` then infers the channel from this checkout (a tag -> stable).
if [ "$CHANNEL" = "stable" ]; then
  git -C "$BOB_HOME" fetch --tags --quiet 2>/dev/null || true
  # Pick the newest release whose engines.json is actually published, so an install DURING a release's publish
  # window (its tag exists but the assets are not uploaded yet) lands on the newest READY release rather than
  # 404 -> a slow source build. Same rule as lifecycle.latest_ready_release_tag (which cannot run before
  # Python exists): the newest 5 v* tags by version order (_MAX_TAG_WALK), each probed at
  # https://github.com/<slug>/releases/download/<tag>/engines.json, ready when that manifest carries at least
  # one real row (a top-level key not starting with '_'). Fall back to the newest tag if none look ready
  # (offline / no origin).
  # owner/repo from the origin URL. Done in three plain substitutions (sed -E has no lazy +? quantifier, so a
  # single pattern would not reliably strip a trailing .git): drop through github host, then .git, then slash.
  SLUG=$(git -C "$BOB_HOME" remote get-url origin 2>/dev/null | sed -E 's#.*github\.com[:/]##; s#\.git$##; s#/$##')
  # A manifest on stdin has a real row. python3 parses it exactly; without python3 (it may not be installed
  # yet) a row is recognised as a top-level non-underscore key whose value is an object.
  manifest_ready() {
    if command -v python3 >/dev/null 2>&1; then
      python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if isinstance(d, dict) and any(not k.startswith("_") for k in d) else 1)' 2>/dev/null
    else
      grep -Eq '"[^_"][^"]*"[[:space:]]*:[[:space:]]*\{'
    fi
  }
  TAG=""
  while IFS= read -r t; do
    [ -z "$t" ] && continue
    if [ -n "$SLUG" ] && curl -fsL --max-time 20 "https://github.com/$SLUG/releases/download/$t/engines.json" 2>/dev/null | manifest_ready; then
      TAG="$t"; break
    fi
  done <<EOF
$(git -C "$BOB_HOME" tag --list 'v*' --sort=-v:refname | head -n5)
EOF
  [ -z "$TAG" ] && TAG=$(git -C "$BOB_HOME" tag --list 'v*' --sort=-v:refname | head -n1)
  if [ -n "$TAG" ]; then
    log "Stable channel: checking out release $TAG  (use --dev to track the latest main)."
    git -C "$BOB_HOME" checkout --quiet "$TAG"
    git -C "$BOB_HOME" submodule update --init --recursive
  else
    log "Stable channel requested but no release tag exists yet; staying on the default branch."
  fi
else
  log "Dev channel: tracking the latest main (prebuilt engine when one matches this commit, else a source build)."
fi

# Forward the prereq-relevant flags (--cpu, --from-source, --with-node) to the prereq step. --with-node is
# prereq-only, so it is not passed on to setup.
PREREQ_FLAGS=""
SETUP_ONLY=""
# shellcheck disable=SC2086  # intentional word-split of the accumulated flag string (POSIX sh, no arrays)
for a in $SETUP_ARGS; do
  case "$a" in
    --cpu) PREREQ_FLAGS="$PREREQ_FLAGS --cpu"; SETUP_ONLY="$SETUP_ONLY $a" ;;
    --from-source) PREREQ_FLAGS="$PREREQ_FLAGS --from-source"; SETUP_ONLY="$SETUP_ONLY $a" ;;
    --with-node) PREREQ_FLAGS="$PREREQ_FLAGS --with-node" ;;
    *) SETUP_ONLY="$SETUP_ONLY $a" ;;
  esac
done
SETUP_ARGS="$SETUP_ONLY"

log "Installing prerequisites ..."
# shellcheck disable=SC2086
./install_prereqs.sh $PREREQ_FLAGS
log "Running setup ..."
# shellcheck disable=SC2086
./setup.sh $SETUP_ARGS
log "Verifying against versions.lock ..."
# scripts/ on PYTHONPATH so `python -m bob.kernel` resolves (the stubs set this internally; it does not
# persist back to this shell).
export PYTHONPATH="$BOB_HOME/scripts${PYTHONPATH:+:$PYTHONPATH}"
python3 -m bob.kernel verify-install || log "(verify reported drift - see the lines above)"

log "Done. Start Bob with:  bob"
