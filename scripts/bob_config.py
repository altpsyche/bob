"""NB2 (contract C2) — the Python runtime-config resolver: produce the runtime-subset of the
`config.json` shape from the neutral sources (config/defaults.json + an optional neutral user
override) WITHOUT PowerShell, so the agent runtime can boot on any OS.

It produces only the ~15 keys the Python core actually reads (C2): port, litellmPort, agentPort,
searxngPort, litellmKey, routing.*, persona.systemPrompt, agent.*, memory.*, vision.*, voice.*. It
never reproduces provisioner keys (profiles, peers, model file paths, build flags).

This is now the ONE config resolve path on every OS — bob_core.load_config calls it unconditionally
(the PowerShell Get-BobConfig/data/config.json path is retired, MODULE ONE).
"""
import copy
import json
from pathlib import Path
from typing import Optional

from bob_core import REPO, load_defaults

_USER_JSON = REPO / "config" / "user.json"
_USER_TOML = REPO / "config" / "user.toml"


def _deep_merge(base: dict, over: dict) -> dict:
    """Recursively merge `over` into a copy of `base` (dict-into-dict; scalars/lists replace)."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _routing_from_role_table(role_table: dict) -> dict:
    """Derive the default routing map (defaultRole -> chat, proRole -> chat-pro, ...) from the
    shared roleTable, so the routing default *values* aren't duplicated anywhere (NB1)."""
    routing: dict = {}
    for entry in role_table.values():
        if entry.get("section", "routing") != "routing":
            continue  # vision lives in its own section, not routing
        routing.setdefault(entry["base"], entry["fallback"])
        routing.setdefault(entry["pro"], entry["proFallback"])
    return routing


def load_user_overlay(user_path: Optional[Path] = None) -> dict:
    """The ONE loader for the neutral per-machine override (config/user.json, or user.toml if present).
    Both the runtime resolver (resolve_runtime_config) and the model-registry resolver
    (bob_models.load_models_config) merge THIS, so the override is parsed one way with one policy.
    Returns {} when absent OR unreadable — a malformed overlay must never break config resolution.
    The override is the runtime/registry-config shape (e.g. {"agent": {"maxSteps": 3}})."""
    try:
        if user_path is not None:
            if not user_path.exists():
                return {}
            if user_path.suffix == ".toml":
                return _load_toml(user_path)
            return json.loads(user_path.read_text(encoding="utf-8"))
        if _USER_JSON.exists():
            return json.loads(_USER_JSON.read_text(encoding="utf-8"))
        if _USER_TOML.exists():
            return _load_toml(_USER_TOML)
    except (json.JSONDecodeError, OSError, RuntimeError):
        return {}   # a bad overlay is ignored, not fatal (matches the model-registry resolver)
    return {}


def _load_toml(path: Path) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover — no TOML support on this interpreter
        raise RuntimeError(f"cannot read {path}: tomllib requires Python 3.11+; use user.json instead")
    with path.open("rb") as fh:
        return tomllib.load(fh)


def resolve_runtime_config(user_path: Optional[Path] = None) -> dict:
    """Build the runtime-subset config from config/defaults.json + an optional neutral user
    override (config/user.json). Returns the runtime config the core reads."""
    defaults = load_defaults()
    ports = defaults["ports"]
    runtime = defaults.get("runtime", {})

    cfg: dict = {
        "port": ports["port"],
        "litellmPort": ports["litellmPort"],
        "searxngPort": ports["searxngPort"],
        "litellmKey": runtime.get("litellmKey", "sk-local"),
        "routing": _routing_from_role_table(defaults["roleTable"]),
        "persona": copy.deepcopy(runtime.get("persona", {})),
        "memory": copy.deepcopy(runtime.get("memory", {})),
        "vision": copy.deepcopy(runtime.get("vision", {})),
        "voice": copy.deepcopy(runtime.get("voice", {})),
        "agent": copy.deepcopy(runtime.get("agent", {})),
    }
    # agentPort default lives under agent (that's where the server reads it, via _port).
    cfg["agent"].setdefault("agentPort", ports["agentPort"])

    cfg = _deep_merge(cfg, load_user_overlay(user_path))

    # Mirror Get-BobConfig: default allowedReadPaths to the repo root when empty, so file_read
    # works out of the box (the N9 denylist still refuses secrets inside it).
    if not cfg["agent"].get("allowedReadPaths"):
        cfg["agent"]["allowedReadPaths"] = [str(REPO)]
    return cfg
