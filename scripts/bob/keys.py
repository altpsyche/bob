"""Provider API keys: list, set, and remove them through the secret seam, from `bob key` and the shell's
`/key`. Both call these functions; neither holds key logic of its own.

A key is stored in <data_dir>/secrets.json (osenv.store_secret, mode 0600, never a tracked file) under
the env name the consumer reads: a cloud peer's `apiKeyEnv` from config/models.json (DEEPSEEK_API_KEY,
ZHIPU_API_KEY, ...) or a web-search key. osenv.secret resolves env, then keychain, then secrets.json, so
an exported variable still wins over a stored key.

LiteLLM reads a peer's key from its own environment (`api_key: os.environ/<apiKeyEnv>` in litellm.yaml),
so peer_key_env() resolves every enabled peer's key for the proxy launch, and setting or removing a peer
key restarts a running proxy so the change takes effect.

  entries(mcfg)            -> one row per known key (name, env, kind, enabled, source)
  set_key(name, value)     -> store it; enable a disabled peer; restart a running LiteLLM
  remove_key(name)         -> drop it from secrets.json; restart a running LiteLLM
  peer_key_env(mcfg)       -> {apiKeyEnv: value} for every enabled peer with a resolvable key
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import osenv  # noqa: E402

# Keys that are not cloud peers: the web tool reads these through osenv.secret (scripts/tools/web.py).
SEARCH_KEYS = {
    "brave": {"env": "braveApiKey", "desc": "Brave Search (web tool)"},
    "tavily": {"env": "tavilyApiKey", "desc": "Tavily search (web tool)"},
}


def _models_config() -> dict:
    """The resolved registry (models.json deep-merged with config/user.json), or {} when unreadable."""
    try:
        import bob_models
        return bob_models.load_models_config()
    except Exception:  # noqa: BLE001: a broken registry lists no peers rather than crashing the command
        return {}


def _peers(mcfg: dict) -> dict:
    return {n: p for n, p in (mcfg.get("peers") or {}).items() if isinstance(p, dict) and p.get("apiKeyEnv")}


def entries(mcfg: dict = None) -> list:
    """Every key Bob knows about: the cloud peers (in models.json order), then the search keys. Each row is
    {name, env, kind: "peer"|"search", enabled, roles, source}, where `source` is where the key resolves
    from (osenv.secret_source) or None when it is missing. A peer's `apiKey` in config/user.json counts as
    "config" when no other source has the key."""
    mcfg = _models_config() if mcfg is None else mcfg
    rows = []
    for name, peer in _peers(mcfg).items():
        env = peer["apiKeyEnv"]
        source = osenv.secret_source(env) or ("config" if peer.get("apiKey") else None)
        rows.append({"name": name, "env": env, "kind": "peer", "enabled": peer.get("enabled") is not False,
                     "roles": sorted(f"{r}-pro" for r in (peer.get("pro") or {})), "source": source})
    for name, spec in SEARCH_KEYS.items():
        rows.append({"name": name, "env": spec["env"], "kind": "search", "enabled": True,
                     "roles": [], "source": osenv.secret_source(spec["env"]), "desc": spec["desc"]})
    return rows


def find(name: str, mcfg: dict = None):
    """The entry whose provider name or env name is `name` (case-insensitive), or None."""
    key = (name or "").strip().lower()
    for row in entries(mcfg):
        if key in (row["name"].lower(), row["env"].lower()):
            return row
    return None


def peer_key_env(mcfg: dict = None) -> dict:
    """{apiKeyEnv: key} for every enabled peer whose key resolves (env, keychain, secrets.json, else the
    peer's `apiKey` from config/user.json). Passed into the LiteLLM process environment at launch."""
    mcfg = _models_config() if mcfg is None else mcfg
    out = {}
    for peer in _peers(mcfg).values():
        if peer.get("enabled") is False:
            continue
        val = osenv.secret(peer["apiKeyEnv"], peer.get("apiKey") or None)
        if val:
            out[peer["apiKeyEnv"]] = val
    return out


def _enable_peer(name: str) -> None:
    """Set peers.<name>.enabled = true in config/user.json, the per-machine overlay models.json merges."""
    from bob import kernel
    cfg = kernel._read_user_config()
    cfg.setdefault("peers", {}).setdefault(name, {})["enabled"] = True
    kernel._write_user_config(cfg)


def _restart_litellm(config: dict) -> str:
    """Restart the LiteLLM proxy when it is running, so it starts with the current peer keys. '' when it
    is not running: the next start picks the keys up on its own."""
    tools = str(_SCRIPTS / "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import stack
    from bob_core import check_litellm
    if not check_litellm(config):
        return ""
    stack.service_control(config, "litellm", "stop")
    return stack.service_control(config, "litellm", "start")


def set_key(name: str, value: str, config: dict) -> list:
    """Store `value` as the key for provider `name` and return the lines to show. A disabled peer is
    enabled (and the runtime configs regenerated so its *-pro routes exist); a running LiteLLM is
    restarted. Raises ValueError for an unknown name or an empty value."""
    row = find(name)
    if row is None:
        raise ValueError(f"unknown provider '{name}'. Known: {', '.join(r['name'] for r in entries())}")
    value = (value or "").strip()
    if not value:
        raise ValueError("empty key, nothing stored")
    path = osenv.store_secret(row["env"], value)
    lines = [f"Stored {row['env']} in {path}."]
    if row["source"] == "env":
        lines.append(f"warning: {row['env']} is also set in your environment, and that value wins. "
                     "Unset it to use the stored key.")
    if row["kind"] != "peer":
        lines.append("Restart the shell or agent for the web tool to use it.")
        return lines
    if not row["enabled"]:
        _enable_peer(row["name"])
        import bob_models
        bob_models.regenerate_configs()
        lines.append(f"Enabled peer '{row['name']}' in config/user.json and regenerated the configs.")
        shared = sorted({o["name"] for o in entries() if o["kind"] == "peer" and o["enabled"]
                         and o["name"] != row["name"] and set(o["roles"]) & set(row["roles"])})
        if shared:
            lines.append(f"warning: {', '.join(shared)} also serves some of these models, so LiteLLM "
                         "splits requests between the peers. Disable one with "
                         f'"peers": {{"<name>": {{"enabled": false}}}} in config/user.json.')
    restarted = _restart_litellm(config)
    if restarted:
        lines.append(f"Restarted LiteLLM. {restarted}")
    if row["roles"]:
        lines.append(f"Models: {', '.join(row['roles'])}")
    return lines


def remove_key(name: str, config: dict) -> list:
    """Delete the stored key for provider `name` from secrets.json and return the lines to show. A running
    LiteLLM is restarted when a peer key was removed. Raises ValueError for an unknown name."""
    row = find(name)
    if row is None:
        raise ValueError(f"unknown provider '{name}'. Known: {', '.join(r['name'] for r in entries())}")
    if not osenv.delete_secret(row["env"]):
        lines = [f"No stored {row['env']} in {osenv.secrets_file()}."]
    else:
        lines = [f"Removed {row['env']} from {osenv.secrets_file()}."]
        if row["kind"] == "peer":
            restarted = _restart_litellm(config)
            if restarted:
                lines.append(f"Restarted LiteLLM. {restarted}")
    still = osenv.secret_source(row["env"])
    if still:
        lines.append(f"note: {row['env']} still resolves from {still}.")
    return lines


def status_lines(mcfg: dict = None) -> list:
    """One line per key: provider, env name, where it resolves from (or 'missing'), and what it serves."""
    lines = []
    for r in entries(mcfg):
        state = f"set ({r['source']})" if r["source"] else "missing"
        what = ", ".join(r["roles"]) if r["roles"] else r.get("desc", "")
        off = "" if r["enabled"] else "  [peer disabled; setting a key enables it]"
        lines.append(f"  {r['name']:<10} {r['env']:<18} {state:<22} {what}{off}")
    return lines
