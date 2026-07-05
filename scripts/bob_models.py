"""ONE-C C0c — the Python reader for the neutral model registry (config/models.json).

The registry (model selection: profiles, roles, repos/ggufs, defaults, peers) is now neutral JSON, read
identically here and by PowerShell (Get-ModelsConfig via ConvertFrom-Json -AsHashtable). This module is
the Python door onto it — the model/show/profiles/profile verbs (ONE-C Slice 4) build on these functions.

Resolution mirrors the pwsh exactly:
  registry (models.json)  <- read-only, version-controlled
  + user.json overlay     <- per-machine, deep-merged (config/user.json, gitignored)
  activeProfile: env BOB_PROFILE  >  data/active-profile.json (writable, D4)  >  models.json default
"""
import json
import os
from pathlib import Path
from typing import Optional

import osenv
from bob_config import _deep_merge

REPO = Path(__file__).resolve().parent.parent
MODELS_FILE = REPO / "config" / "models.json"
USER_FILE = REPO / "config" / "user.json"


def _active_profile_file() -> Path:
    """The writable activeProfile override (D4) — under the data dir, mirroring the pwsh
    $script:ActiveProfileFile. osenv.data_dir() honors BOB_DATA_DIR."""
    return osenv.data_dir() / "active-profile.json"


def load_models_config(models_file: Optional[Path] = None, user_file: Optional[Path] = None) -> dict:
    """The resolved registry: models.json deep-merged with config/user.json, with `activeProfile`
    overridden by data/active-profile.json when present (env BOB_PROFILE is applied at resolve time,
    not here — matching Get-ModelsConfig, whose result Resolve-ProfileName then reads)."""
    mf = models_file or MODELS_FILE
    if not mf.exists():
        raise RuntimeError(f"models config not found: {mf}")
    config = json.loads(mf.read_text(encoding="utf-8"))
    uf = user_file or USER_FILE
    if uf.exists():
        try:
            config = _deep_merge(config, json.loads(uf.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass  # a malformed user overlay must not break the registry read
    apf = _active_profile_file()
    if apf.exists():
        try:
            override = json.loads(apf.read_text(encoding="utf-8")).get("activeProfile")
            if override:
                config["activeProfile"] = override
        except (json.JSONDecodeError, OSError):
            pass
    return config


def resolve_profile_name(name: Optional[str] = None, config: Optional[dict] = None) -> str:
    """Profile precedence (mirrors Resolve-ProfileName): explicit arg > $BOB_PROFILE > the resolved
    activeProfile. Raises on an unknown profile."""
    config = config if config is not None else load_models_config()
    resolved = name or os.environ.get("BOB_PROFILE") or config.get("activeProfile")
    profiles = config.get("profiles", {})
    if resolved not in profiles:
        raise ValueError(f"unknown profile '{resolved}'. Valid: {', '.join(sorted(profiles))}")
    return resolved


def profile_roles(name: Optional[str] = None, config: Optional[dict] = None) -> dict:
    """The role→spec map for a profile, skipping '_'-prefixed metadata (_targetVRAM/_notes/_cpuTier)."""
    config = config if config is not None else load_models_config()
    profile = config["profiles"][resolve_profile_name(name, config)]
    return {role: spec for role, spec in profile.items() if not role.startswith("_")}


def set_active_profile(name: str, config: Optional[dict] = None) -> str:
    """Persist the writable activeProfile to data/active-profile.json (D4) — the same file pwsh
    Set-ActiveProfile writes. Validates against known profiles. Returns the resolved name."""
    config = config if config is not None else load_models_config()
    profiles = config.get("profiles", {})
    if name not in profiles:
        raise ValueError(f"unknown profile '{name}'. Valid: {', '.join(sorted(profiles))}")
    apf = _active_profile_file()
    apf.parent.mkdir(parents=True, exist_ok=True)
    apf.write_text(json.dumps({"activeProfile": name}, indent=2) + "\n", encoding="utf-8")
    return name


def list_profiles(config: Optional[dict] = None) -> dict:
    """{profile_name: _targetVRAM string} for every profile — for `bob profiles`."""
    config = config if config is not None else load_models_config()
    return {name: prof.get("_targetVRAM", "") for name, prof in config.get("profiles", {}).items()}


def regenerate_configs() -> bool:
    """Interim bridge (ONE-C): regenerate the runtime configs (llama-swap.yaml + litellm.yaml) from
    models.json via the still-PowerShell generators, best-effort. Returns True if pwsh ran them, False
    when pwsh is absent (leaving the existing configs in place). Single-sourced here so the stack
    bring-up (scripts/tools/stack.py) and profile switch (scripts/tools/models.py) share ONE bridge;
    when Slice 6 ports `gen` to Python this body swaps to the Python generator and callers are unchanged."""
    import shutil
    import subprocess

    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        return False
    for gen in ("gen-llama-swap.ps1", "gen-litellm.ps1"):
        script = REPO / "scripts" / gen
        if script.exists():
            subprocess.run([pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                           check=False, capture_output=True)
    return True
