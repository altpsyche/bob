"""DeepSeek Harness (dsh) link management for Bob — one writer for every Bob-owned dsh entry.

Ownership map (no key is written to two layers):
  $DSH_HOME/settings.yaml                    llm-pi-ai.providers.bob
  $DSH_HOME/.credentials.yaml                BOB_LITELLM_KEY
  $DSH_HOME/cordis.patch.yml                 Bob MCP plugin entry
  $DSH_HOME/profiles/<name>/cordis.patch.yml agent-default-model and bob-dsh-bridge
  config/user.json (the user overlay)        agent.mcpEnabled, agent.dshTools, agent.dshEnabled, agent.dshTrust

`bob setup` installs the link (install); `bob update` refreshes only what is present (refresh), and
agent.dshEnabled=false, set by `bob dsh uninstall`, keeps both away from dsh.

The module deliberately delegates model-route generation to scripts/tools/generate.py (the same
fragments `bob gen` writes) instead of owning a second model registry or budget implementation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONFIG_DSH = REPO / "config" / "dsh"

# The DSH CLI is often the first Bob surface a user touches (and `bob dsh trust` resolves tiers from
# the live tool registry), so make the sibling tool modules importable here instead of relying on the
# caller having already extended sys.path.
for _p in (REPO / "scripts", REPO / "scripts" / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
DEFAULT_MODEL = "coder-deep"
FALLBACK_MODEL = "chat-deep"
PROVIDER = "bob"
MCP_ID = "bob-tools"
HOOK_ID = "bob-hooks"   # legacy id removed by bridge_on
BRIDGE_ID = "bob-dsh-bridge"
BRIDGE_PACKAGE = REPO / "scripts" / "dsh_bridge"


def home() -> Path:
    """$DSH_HOME when set and non-blank, else ~/.dsh, matching dsh itself."""
    env = (os.environ.get("DSH_HOME") or "").strip()
    return Path(env).expanduser() if env else Path.home() / ".dsh"


def dsh_bin() -> str:
    return shutil.which("dsh") or ""


def dsh_version() -> str:
    exe = dsh_bin()
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
        return (out.stdout or out.stderr or "").strip().splitlines()[0]
    except Exception:
        return ""


def profiles(root: Path = None) -> list:
    base = (root or home()) / "profiles"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith("."))


def default_profile(root: Path = None) -> str:
    names = profiles(root)
    if "web" in names:
        return "web"
    return names[0] if names else "web"


def _profile_patch(profile: str, root: Path = None) -> Path:
    return (root or home()) / "profiles" / profile / "cordis.patch.yml"


def _load_yaml(path: Path):
    import yaml
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _plugin_entry_lines(plugin_id: str, name: str, config_lines: list) -> list:
    out = ["- insert:", f"    - id: {plugin_id}", f"      name: '{name}'", "      config:"]
    out += [f"        {ln}" for ln in config_lines]
    return out


def _upsert_plugin(path: Path, plugin_id: str, entry: list) -> str:
    """Insert or replace one top-level insert item by id, preserving every other byte."""
    from generate import _patch_lines, _top_level_items

    lines = _patch_lines(path)
    id_line = re.compile(rf"\s*-\s+id:\s*['\"]?{re.escape(plugin_id)}['\"]?\s*(#.*)?$")
    for start, end in _top_level_items(lines):
        item = lines[start:end]
        if any(id_line.match(ln) for ln in item):
            lines[start:end] = entry
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return f"replaced {plugin_id}"
    while lines and not lines[-1].strip():
        lines.pop()
    lines += ([""] if lines else []) + entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"installed {plugin_id}"


def _remove_plugin(path: Path, plugin_id: str) -> bool:
    from generate import _patch_lines, _top_level_items

    if not path.exists():
        return False
    lines = _patch_lines(path)
    id_line = re.compile(rf"\s*-\s+id:\s*['\"]?{re.escape(plugin_id)}['\"]?\s*(#.*)?$")
    for start, end in reversed(_top_level_items(lines)):
        if any(id_line.match(ln) for ln in lines[start:end]):
            del lines[start:end]
            while lines and not lines[-1].strip():
                lines.pop()
            if not _top_level_items(lines):
                lines.append("[]")  # dsh needs the patch layer to stay an array
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def _default_model_entry(model: str) -> list:
    """A top-level override of the `agent-default-model` row dsh-base already inserts: an `insert:`
    would add a second row with that id, which dsh refuses to load."""
    return ["- id: agent-default-model", "  name: '@deepseek-ai/dsh-agent-default-model'", "  config:",
            f"    provider: {PROVIDER}", f"    model: {model}"]


def _choose_model(mode: str, config: dict = None) -> str:
    from bob_core import load_config
    import bob_models

    cfg = config or load_config()
    try:
        name = bob_models.resolve_profile_name(config=bob_models.load_models_config())
        roles = bob_models.profile_roles(name, config=bob_models.load_models_config())
    except Exception:
        roles = {}
    if "coder" in roles:
        return f"coder-{mode}"
    return f"chat-{mode}"


def set_default_model(profile: str, model: str, root: Path = None) -> str:
    path = _profile_patch(profile, root)
    return _upsert_plugin(path, "agent-default-model", _default_model_entry(model))


def set_mode(profile: str, mode: str) -> str:
    return set_default_model(profile, _choose_model(mode))


def _install_mcp(root: Path) -> str:
    import generate
    from bob_core import load_config

    cfg = load_config()
    generate.configure(cfg)
    generate.gen_dsh()
    return generate._install_dsh_mcp(root)


class ConfigWriteRefused(RuntimeError):
    """A user-overlay write Bob will not make (a TOML overlay, or a file that does not parse)."""


def _user_config_path() -> Path:
    """The overlay Bob's DSH commands write: the one bob_config resolves (BOB_USER_CONFIG, else
    config/user.json, else config/user.toml), defaulting to config/user.json when none exists yet."""
    import bob_config
    path = bob_config.user_config_path()
    return Path(path) if path is not None else (REPO / "config" / "user.json")


def _update_user_config(mutate, keys: str) -> tuple:
    """Read-modify-write the user overlay through kernel.update_user_config, the one strict writer, and
    return (path, what `mutate` returned). Raises ConfigWriteRefused, leaving the file untouched, for a
    TOML overlay (edited by hand) or a JSON file that does not parse. `keys` names what the caller
    sets, for the refusal message."""
    from bob import kernel
    path = _user_config_path()
    if path.suffix == ".toml":
        raise ConfigWriteRefused(f"set {keys} in {path} by hand, then re-run")
    try:
        return path, kernel.update_user_config(mutate, path)
    except kernel.UserConfigError as e:
        raise ConfigWriteRefused(str(e)) from e


def _set_agent_flags(**flags) -> str:
    """Set boolean agent.<key> values in the user overlay. Raises ConfigWriteRefused."""
    def mutate(data):
        agent = data.get("agent")
        if not isinstance(agent, dict):
            agent = data["agent"] = {}
        agent.update({k: bool(v) for k, v in flags.items()})
    keys = ", ".join(f"agent.{k}={str(bool(v)).lower()}" for k, v in flags.items())
    path, _ = _update_user_config(mutate, keys)
    return f"{path}: {keys}"




def link_enabled(config: dict = None) -> bool:
    """Whether setup and update manage the DSH link: agent.dshEnabled, which `bob dsh uninstall` turns
    off and `bob dsh install` turns back on."""
    if config is None:
        from bob_core import load_config
        config = load_config()
    return (config.get("agent", {}) or {}).get("dshEnabled", True) is not False


def tools_on(profile: str = None) -> str:
    root = home()
    if not root.is_dir():
        return _missing_home()
    try:
        flag = _set_agent_flags(mcpEnabled=True, dshTools=True)
    except ConfigWriteRefused as e:
        return f"tools not enabled: {e}"
    return flag + "\n" + _install_mcp(root)


def _remove_mcp_entries(root: Path) -> list:
    removed = []
    if _remove_plugin(root / "cordis.patch.yml", MCP_ID):
        removed.append(str(root / "cordis.patch.yml"))
    for name in profiles(root):
        if _remove_plugin(_profile_patch(name, root), MCP_ID):
            removed.append(str(_profile_patch(name, root)))
    return removed


def tools_off(profile: str = None) -> str:
    """Remove Bob's MCP entry from dsh and persist agent.dshTools=false, so `bob gen`, setup and update
    do not put it back. Bob's MCP server stays on for other clients (agent.mcpEnabled is untouched)."""
    removed = _remove_mcp_entries(home())
    line = "removed " + ", ".join(removed) if removed else "bob MCP tools were not installed"
    try:
        return line + "\n" + _set_agent_flags(dshTools=False)
    except ConfigWriteRefused as e:
        return line + f"\nagent.dshTools not persisted: {e}"


def _profile_package(profile: str, root: Path = None) -> Path:
    return (root or home()) / "profiles" / profile / "package.json"


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def bridge_status(profile: str = None) -> str:
    root = home()
    target = profile or default_profile(root)
    return f"bob-dsh-bridge: {'on' if _bridge_installed(root, target) else 'off'} (profile {target})"


def _bridge_command() -> str:
    """The absolute command the bridge spawns to reach Bob: the same shim the MCP entry uses, resolved on
    PATH when it is a bare name (bob.cmd on Windows), since dsh does not run the bridge from a shell."""
    import generate
    shim = generate._bob_shim()
    return shutil.which(shim) or shim


def bridge_on(profile: str = None) -> str:
    """Install the native session bridge into a dsh profile with `dsh plugin add`, then load it with an
    insert in the profile's patch layer. The bridge is a plain plugin dependency, not a profile bundle
    (it declares no `dsh.bundle`, and dsh refuses to boot a profile that lists one), so a bundles entry
    left from an earlier install is removed. Nothing is recorded when the add fails or there is no dsh
    binary to run it, so the profile never references a plugin it does not have."""
    import generate
    root = home()
    if not root.is_dir():
        return _missing_home()
    if not (BRIDGE_PACKAGE / "package.json").exists():
        return f"bridge package missing at {BRIDGE_PACKAGE}"
    target = profile or default_profile(root)
    dsh = dsh_bin()
    if not dsh:
        return f"  profile {target}: bridge not installed (dsh binary not found on PATH)"
    lines = []
    try:
        r = subprocess.run([dsh, "plugin", "--profile", target, "add", f"file:{BRIDGE_PACKAGE}"],
                           capture_output=True, text=True, timeout=300)
    except Exception as e:
        return f"  profile {target}: bridge not installed (dsh plugin add: {e})"
    output = (r.stdout or r.stderr or "").strip().splitlines()
    if r.returncode != 0:
        detail = output[-1] if output else f"exit {r.returncode}"
        return f"  profile {target}: bridge not installed (dsh plugin add failed: {detail})"
    if output:
        lines.append("dsh plugin add: " + output[-1])
    try:
        pkg_path = _profile_package(target, root)
        pkg = _load_json(pkg_path)
        deps = pkg.setdefault("dependencies", {})
        deps[BRIDGE_ID] = f"file:{BRIDGE_PACKAGE}"
        _drop_bridge_bundle(pkg)
        _write_json(pkg_path, pkg)
        patch = _profile_patch(target, root)
        _remove_plugin(patch, HOOK_ID)
        _upsert_plugin(patch, BRIDGE_ID, _plugin_entry_lines(
            BRIDGE_ID, BRIDGE_ID, [f"bobCommand: {generate._yaml_str(_bridge_command())}"]))
        lines.append(f"profile {target}: installed {BRIDGE_ID}")
    except Exception as e:
        lines.append(f"profile {target}: could not write the bridge entry ({e})")
    return "\n".join(f"  {ln}" for ln in lines)


def _drop_bridge_bundle(pkg: dict) -> None:
    """Remove the bridge from the profile's `dsh.profile.bundles` (see bridge_on)."""
    bundles = ((pkg.get("dsh") or {}).get("profile") or {}).get("bundles")
    if isinstance(bundles, list) and BRIDGE_ID in bundles:
        bundles.remove(BRIDGE_ID)


def _bridge_installed(root: Path, profile: str) -> bool:
    deps = _load_json(_profile_package(profile, root)).get("dependencies") or {}
    return _plugin_present(_profile_patch(profile, root), BRIDGE_ID) and BRIDGE_ID in deps


def bridge_off(profile: str = None) -> str:
    root = home()
    target = profile or default_profile(root)
    try:
        patch = _profile_patch(target, root)
        removed = _remove_plugin(patch, BRIDGE_ID)
        pkg_path = _profile_package(target, root)
        pkg = _load_json(pkg_path)
        deps = pkg.get("dependencies") or {}
        deps.pop(BRIDGE_ID, None)
        _drop_bridge_bundle(pkg)
        _write_json(pkg_path, pkg)
        return f"profile {target}: removed {BRIDGE_ID}" if removed else f"profile {target}: bridge was not installed"
    except Exception as e:
        return f"profile {target}: could not update bridge state ({e})"


def _dsh_db(create: bool = False):
    """Open the Bob memory DB used by the DSH import tables. Reads do not create a DB."""
    import sqlite3
    from bob_core import _get_db_path, load_config
    cfg = load_config()
    path = Path(_get_db_path(cfg))
    if not create and not path.exists():
        return None, cfg, path
    return sqlite3.connect(str(path)), cfg, path


def _dsh_tables(db) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dsh_sessions'").fetchone() is not None
    except Exception:
        return False


def sessions_list() -> str:
    db, _cfg, _path = _dsh_db()
    if db is None:
        return "no imported DSH sessions"
    try:
        if not _dsh_tables(db):
            return "no imported DSH sessions"
        rows = db.execute(
            "SELECT s.session_id, s.parent_session_id, s.cwd, s.last_seq, s.state, s.updated_at,"
            " (SELECT COUNT(*) FROM transcript t WHERE t.run_id = 'dsh:' || s.session_id) AS turns"
            " FROM dsh_sessions s ORDER BY s.updated_at DESC, s.session_id").fetchall()
    finally:
        db.close()
    if not rows:
        return "no imported DSH sessions"
    lines = [f"{'session':<36} {'parent':<36} {'last':<6} {'turns':<6} state  cwd"]
    for sid, parent, cwd, last, state, _updated, turns in rows:
        lines.append(f"{sid:<36} {(parent or '-'):<36} {last:<6} {turns:<6} {state or '':<7} {cwd or '-'}")
    return "\n".join(lines)


def sessions_show(session_id: str) -> str:
    db, _cfg, _path = _dsh_db()
    if db is None:
        return f"unknown DSH session: {session_id}"
    try:
        if not _dsh_tables(db):
            return f"unknown DSH session: {session_id}"
        row = db.execute(
            "SELECT parent_session_id, cwd, origin, last_seq, state, updated_at"
            " FROM dsh_sessions WHERE session_id=?", [session_id]).fetchone()
        turns = db.execute(
            "SELECT seq, role, tool_name, content FROM transcript WHERE run_id=? ORDER BY seq",
            [f"dsh:{session_id}"]).fetchall()
        raw_events = db.execute(
            "SELECT COUNT(*) FROM dsh_events WHERE session_id=?", [session_id]).fetchone()[0]
    finally:
        db.close()
    if row is None:
        return f"unknown DSH session: {session_id}"
    lines = [f"session {session_id}", f"  parent: {row[0] or '-'}", f"  cwd: {row[1] or '-'}",
             f"  origin: {row[2] or '-'}", f"  last_seq: {row[3]}", f"  state: {row[4] or '-'}",
             f"  raw events: {raw_events}", f"  turns: {len(turns)}"]
    for seq, role, tool, content in turns:
        label = f"{role}:{tool}" if tool else role
        lines.append(f"  [{seq}] {label}: {str(content)[:160]}")
    return "\n".join(lines)


def sessions_consolidate(session_id: str) -> str:
    from bob_core import consolidate_session, load_config, project_key
    db, cfg, _path = _dsh_db()
    if db is None:
        return f"no imported turns for DSH session: {session_id}"
    try:
        if not _dsh_tables(db):
            return f"no imported turns for DSH session: {session_id}"
        turns = [{"role": role, "content": content, "tool_name": tool_name}
                 for _seq, role, tool_name, content in db.execute(
                     "SELECT seq, role, tool_name, content FROM transcript WHERE run_id=? ORDER BY seq",
                     [f"dsh:{session_id}"]).fetchall()]
        row = db.execute("SELECT cwd FROM dsh_sessions WHERE session_id=?", [session_id]).fetchone()
    finally:
        db.close()
    if not turns:
        return f"no imported turns for DSH session: {session_id}"
    # Match the shell/agent-API consolidation scope: the project key (git root/cwd) when project
    # scoping is on, not the raw DSH cwd string.
    scope = project_key(row[0], cfg) if row and row[0] else None
    result = consolidate_session(turns, config=cfg, owner=cfg.get("agent", {}).get("defaultOwner", "local"),
                                 scope=scope, session_id=session_id)
    return f"consolidated {session_id}: {result}"


def sessions_forget(session_id: str) -> str:
    """Forget one imported DSH session and its child sessions: raw events, derived transcript, and any
    memory they produced. The ids are remembered, so the next bridge snapshot does not import them again."""
    import bob_memory
    from bob_core import _get_db_path, load_config
    cfg = load_config()
    db_path = Path(_get_db_path(cfg))
    owner = cfg.get("agent", {}).get("defaultOwner", "local")
    facts = 0
    turns = 0
    ids = [session_id]
    if db_path.exists():
        ids = bob_memory.dsh_forget_session(session_id, db_path)
        for sid in ids:
            # Provenance-based memory forget, then the transcript (it is not audit-retained).
            try:
                facts += bob_memory.forget_by_session(sid, db_path, owner=owner)
            except Exception:
                pass
            try:
                turns += bob_memory.forget_transcript_session(sid, db_path, owner=owner)
            except Exception:
                pass
    children = f" and {len(ids) - 1} child session(s)" if len(ids) > 1 else ""
    return (f"forgot DSH session {session_id}{children}: {turns} transcript turn(s), "
            f"{facts} consolidated memory row(s)")


_TRUST_TIER_ALIASES = {
    "observe": "read", "read": "read", "read-only": "read", "readonly": "read",
    "write": "write", "edit": "write", "project": "write",
    "execute": "execute", "exec": "execute", "shell": "execute",
    "all": "all", "full": "all", "*": "all",
}
_TRUST_TIERS = ("read", "write", "execute", "all")


def _gated_tools(registry) -> set:
    """Every tool Bob's unattended/MCP gate refuses unless explicitly allowed."""
    gated = set(getattr(registry, "approval_required_tools", set()))
    gated |= set(getattr(registry, "mutating_tools", set()))
    gated |= set(getattr(registry, "remote_tools", set()))
    gated |= {"spawn_agent", "schedule_run"}
    return gated


# Tools kept out of the `write` tier even though they are plain mutations: anything that schedules
# work to run later (every schedule_* tool, present or future) and memory_block, which rewrites the
# always-injected core instructions. Both outlive the call, so they sit in `execute` and above.
_WRITE_DENY_PREFIXES = ("schedule_",)
_WRITE_DENY = frozenset({"memory_block"})


def _write_denied(name: str) -> bool:
    return name in _WRITE_DENY or name.startswith(_WRITE_DENY_PREFIXES)


def trust_tiers(registry) -> dict:
    """The DSH trust tiers as concrete allow-sets, derived from the live registry.

    read    -- no state-changing tools
    write   -- state-changing tools that are not command execution, delegation, scheduling, core
               memory blocks or remote MCP (profile_switch and the file/edit tools are in)
    execute -- write + command execution, scheduling and core memory blocks
    all     -- every gated tool, including delegation, schedule_run and remote MCP
    """
    gated = _gated_tools(registry)
    dangerous = {"spawn_agent", "schedule_run"} | set(getattr(registry, "remote_tools", set()))
    execute = {n for n in gated if n not in dangerous}
    # `write` is the coding-loop subset: mutations, but not tools whose whole point is running commands
    # (approval-required tools such as shell_run), deferring work, or rewriting persistent instructions.
    approval = set(getattr(registry, "approval_required_tools", set()))
    write = {n for n in execute if n not in approval and not _write_denied(n)}
    return {"read": set(), "write": write, "execute": execute, "all": gated}


def _trust_spec_tools(spec, tiers: dict) -> set:
    if isinstance(spec, str):
        return set(tiers.get(_TRUST_TIER_ALIASES.get(spec.strip().lower(), ""), set()))
    if isinstance(spec, (list, tuple, set)):
        return {str(n) for n in spec if str(n).strip()}
    return set()


def resolve_dsh_allow(config: dict = None, cwd: str = None, registry=None) -> set:
    """The effective unattended allow-set for a DSH/MCP call: the explicit global
    agent.mcpAllowTools list plus the tier selected for this project (falling back to global), or
    the explicit list agent.mcpAllowTools when no trust tier is configured."""
    from bob_core import load_config
    cfg = config or load_config()
    agent = cfg.get("agent", {}) or {}
    base = {str(n) for n in (agent.get("mcpAllowTools") or []) if str(n).strip()}
    trust = agent.get("dshTrust") or {}
    if not isinstance(trust, dict) or not trust:
        return base
    if registry is None:
        from tool_registry import ToolRegistry
        registry = ToolRegistry.from_config(cfg, quiet=True)
    tiers = trust_tiers(registry)
    projects = trust.get("projects") or {}
    project_spec = _project_spec(projects, cwd) if isinstance(projects, dict) else None
    active = project_spec if project_spec is not None else trust.get("global")
    return base | _trust_spec_tools(active, tiers)


def _norm_path(path: str) -> Path:
    """A path in comparable form: resolved, and case-folded where the filesystem is case-insensitive
    (os.path.normcase lower-cases on Windows and is the identity elsewhere)."""
    return Path(os.path.normcase(str(Path(path).expanduser().resolve())))


def _project_spec(projects: dict, cwd: str):
    """The trust spec of the most specific configured project that contains `cwd` (the project itself
    or any directory under it), or None when no project does."""
    if not cwd:
        return None
    here = _norm_path(cwd)
    best, best_depth = None, -1
    for path, spec in projects.items():
        try:
            root = _norm_path(path)
        except (OSError, ValueError, TypeError):
            continue
        if (here == root or here.is_relative_to(root)) and len(root.parts) > best_depth:
            best, best_depth = spec, len(root.parts)
    return best


def make_trust_hook(config: dict, registry):
    """A built-in PreToolUse hook: a configured DSH trust tier is a deny-by-default policy for the
    unattended MCP surface. It is inert for attended runs (where the operator can approve), and inert
    when no dshTrust tier is configured, so existing behavior is unchanged."""
    trust = (config or {}).get("agent", {}).get("dshTrust") or {}
    if not isinstance(trust, dict) or not trust:
        return None

    def hook(name, args, context):
        if getattr(context, "unattended_allow", None) is None:
            return None
        cwd = getattr(context, "cwd", None) or os.getcwd()
        try:
            effective = resolve_dsh_allow(config, cwd=cwd, registry=registry)
        except Exception:
            return None
        if name in _gated_tools(registry) and name not in effective:
            return {"decision": "deny"}
        return None

    return hook


def trust_tier_names() -> str:
    return ", ".join(_TRUST_TIERS)


def _project_key(project: str = None) -> str:
    return str(Path(project).expanduser().resolve() if project else Path.cwd().resolve())


def trust_status(config: dict = None, cwd: str = None) -> str:
    from bob_core import load_config
    cfg = config or load_config()
    agent = cfg.get("agent", {}) or {}
    trust = agent.get("dshTrust") or {}
    manual = list(agent.get("mcpAllowTools") or [])
    lines = ["bob dsh trust", f"  mcpAllowTools: {', '.join(manual) if manual else '(empty)'}"]
    if isinstance(trust, dict) and trust:
        lines.append(f"  global tier:  {trust.get('global') or '(unset)'}")
        projects = trust.get("projects") or {}
        if projects:
            for path, spec in sorted(projects.items()):
                lines.append(f"  project tier: {path} -> {spec}")
    else:
        lines.append("  trust tiers:  (unset; only agent.mcpAllowTools is consulted)")
    try:
        from tool_registry import ToolRegistry
        reg = ToolRegistry.from_config(cfg, quiet=True)
        effective = sorted(resolve_dsh_allow(cfg, cwd=cwd or os.getcwd(), registry=reg))
        tiers = trust_tiers(reg)
        lines.append(f"  effective for {_project_key(cwd)}: {', '.join(effective) if effective else '(read-only)'}")
        lines.append("  tiers: " + ", ".join(f"{name}({len(tools)})" for name, tools in tiers.items()))
    except Exception as e:
        lines.append(f"  (could not resolve tiers: {e})")
    return "\n".join(lines)


def trust_tools(tools: list = None, all_tools: bool = False, off: bool = False, tier: str = None,
                scope: str = "global", project: str = None) -> str:
    """Update DSH trust. Without arguments, show status.

    scope=global writes agent.dshTrust.global (and explicit tools to agent.mcpAllowTools);
    scope=project writes agent.dshTrust.projects[<resolved project path>]. A tier is a symbolic name
    (read|write|execute|all) resolved from the live tool registry; an explicit tool list is a custom
    per-scope allow-set. `off` clears the selected scope (and the global manual list, for global)."""
    if not (off or all_tools or tier or tools):
        return trust_status()
    if tier and not all_tools and not off:
        normalized = _TRUST_TIER_ALIASES.get(str(tier).strip().lower())
        if normalized not in _TRUST_TIERS:
            return f"unknown trust tier '{tier}' (known: {trust_tier_names()})"
    key = _project_key(project)
    where = f"project {key}" if scope == "project" else "all projects"

    def mutate(data):
        agent = data.get("agent")
        if not isinstance(agent, dict):
            agent = data["agent"] = {}
        trust = agent.setdefault("dshTrust", {})
        if not isinstance(trust, dict):
            trust = agent["dshTrust"] = {}
        projects = trust.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = trust["projects"] = {}
        if off:
            if scope == "project":
                projects.pop(key, None)
                result = f"cleared trust tier for project {key}"
            else:
                trust.pop("global", None)
                agent["mcpAllowTools"] = []
                result = "cleared global trust tier and mcpAllowTools"
        elif all_tools or tier:
            spec = "all" if all_tools else _TRUST_TIER_ALIASES[str(tier).strip().lower()]
            if scope == "project":
                projects[key] = spec
            else:
                trust["global"] = spec
            result = f"trust tier {spec} set for {where}"
        else:
            names = sorted({str(t) for t in tools if str(t).strip()})
            if scope == "project":
                current = projects.get(key)
                current_names = set(current) if isinstance(current, (list, tuple, set)) else set()
                projects[key] = sorted(current_names | set(names))
            else:
                current = {str(n) for n in (agent.get("mcpAllowTools") or []) if str(n).strip()}
                agent["mcpAllowTools"] = sorted(current | set(names))
            result = f"trusted {', '.join(names)} for {where}"
        # Drop an empty trust block so an unconfigured install keeps the mcpAllowTools-only behavior.
        if not trust.get("global") and not projects:
            agent.pop("dshTrust", None)
        return result

    try:
        path, result = _update_user_config(
            mutate, f"agent.dshTrust and agent.mcpAllowTools (tiers: {trust_tier_names()})")
    except ConfigWriteRefused as e:
        return f"trust not changed: {e}"
    return f"{path}: {result}"


def import_session(payload: dict, config: dict = None) -> dict:
    """Import one native-bridge payload through the one Bob transcript pipeline."""
    from bob_core import _get_db_path, load_config
    import bob_memory
    from bob_core import project_key
    cfg = config or load_config()
    owner = cfg.get("agent", {}).get("defaultOwner", "local")
    return bob_memory.dsh_import_sessions(
        payload.get("sessions") or [], _get_db_path(cfg), owner=owner,
        scope_for=lambda cwd: project_key(cwd, cfg) if cwd else None)


def _missing_home() -> str:
    return (f"no DeepSeek Harness home at {home()}. Install dsh first: "
            "npm i -g @deepseek-ai/dsh, then run `dsh web` once, then `bob dsh install`.")




def pinned_dsh_version() -> str:
    """The DeepSeek Harness version pinned in versions.lock, with a conservative fallback."""
    try:
        from bob.versions import pinned_package
        entry = pinned_package("dsh") or {}
        return str(entry.get("version") or "0.1.5-rc.3")
    except Exception:
        return "0.1.5-rc.3"


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?")


def _version_key(text: str):
    """A sortable key for the first semver in `text` (a pre-release sorts below its release), or None
    when there is no version in it."""
    m = _VERSION_RE.search(text or "")
    if not m:
        return None
    pre = m.group(4)
    pre_key = (1,) if pre is None else (0,) + tuple(
        (0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) + (pre_key,)


def _dsh_manager(exe: str) -> tuple:
    """(name, path) of the package manager to install or upgrade dsh with. An installed dsh is upgraded
    by the manager that owns it (a pnpm global lives under a `pnpm` directory), so an npm-installed
    dsh never gains a second pnpm copy that shadows it on PATH. A fresh install prefers pnpm."""
    pnpm, npm = shutil.which("pnpm"), shutil.which("npm")
    if exe:
        try:
            owned_by_pnpm = "pnpm" in str(Path(exe).resolve()).lower()
        except OSError:
            owned_by_pnpm = "pnpm" in exe.lower()
        if owned_by_pnpm:
            return ("pnpm", pnpm) if pnpm else ("", "")
        return ("npm", npm) if npm else ("", "")
    if pnpm:
        return "pnpm", pnpm
    return ("npm", npm) if npm else ("", "")


def ensure_dsh(install_missing: bool = True) -> str:
    """Install the pinned DeepSeek Harness when it is missing, or upgrade it when the installed one is
    older than the pin. A newer installed dsh is left alone (the pin is a floor, not a downgrade), and
    an installed dsh whose version cannot be read is left alone too. `install_missing=False` (the
    `bob update` refresh) only upgrades an existing install.

    Bob never guesses a floating version: the exact version comes from versions.lock.
    """
    want = pinned_dsh_version()
    exe = dsh_bin()
    have = dsh_version() if exe else ""
    if exe:
        have_key, want_key = _version_key(have), _version_key(want)
        if have_key is None or want_key is None:
            return f"dsh {have or exe} already installed (version not comparable to pin {want}; left as-is)"
        if have_key >= want_key:
            return f"dsh {have} already installed"
    elif not install_missing:
        return "dsh not installed; skipped (run `bob dsh install` to add it)"
    name, manager = _dsh_manager(exe)
    if not manager:
        if exe:
            return (f"dsh {have} is older than the pinned {want}, and the package manager that installed "
                    f"it was not found; upgrade it with that manager to @deepseek-ai/dsh@{want}")
        return ("dsh not installed and no pnpm/npm found. Install Node.js + pnpm, then run: "
                f"pnpm add -g @deepseek-ai/dsh@{want}")
    if name == "pnpm":
        argv = [manager, "add", "-g", f"@deepseek-ai/dsh@{want}"]
    else:
        argv = [manager, "install", "-g", f"@deepseek-ai/dsh@{want}"]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return f"dsh install failed: {e}"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        return "dsh install failed: " + (detail[0] if detail else f"exit {r.returncode}")
    return f"{'upgraded' if exe else 'installed'} @deepseek-ai/dsh@{want} with {name}"


def ensure_home(profile: str = None) -> str:
    """Create the local DSH profile home non-interactively when the package is installed but the profile
    has never been booted. Runs `dsh --profile <name> --dump-config`, which composes the shipped profile
    layers and exits before the app starts — no browser, no long-running process. This is what lets a
    fresh `bob setup` / `bob update` finish wiring DSH in one command instead of requiring a separate
    `dsh web` first."""
    root = home()
    target = profile or default_profile(root)
    if (root / "profiles" / target).is_dir():
        return f"dsh home already initialized at {root} (profile {target})"
    exe = dsh_bin()
    if not exe:
        return ("dsh home not initialized and dsh binary not found. Install dsh, then run: "
                "bob dsh install")
    try:
        r = subprocess.run([exe, "--profile", target, "--dump-config"],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        return f"dsh home init failed: {e}"
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip().splitlines()
        return "dsh home init failed: " + (detail[-1] if detail else f"exit {r.returncode}")
    if not (root / "profiles" / target).is_dir():
        return f"dsh home init did not create {root / 'profiles' / target}"
    return f"initialized dsh home at {root} (profile {target})"


def _guarded(label: str, fn) -> str:
    """Run one link step; a failure (an unreadable profile patch, a locked file) becomes a report line
    instead of aborting the rest of the link."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return f"{label}: failed ({e})"


def _route_models() -> set:
    """The model ids in the generated dsh provider route (config/dsh/settings.yaml)."""
    data = _load_yaml(CONFIG_DSH / "settings.yaml") or {}
    route = ((data.get("llm-pi-ai") or {}).get("providers") or {}).get(PROVIDER) or {} \
        if isinstance(data, dict) else {}
    return {str(m.get("id")) for m in (route.get("models") or []) if isinstance(m, dict)}


def _current_mode(root: Path, profile: str) -> str:
    """The quick/deep mode of the profile's current Bob default model, or "" when it has none."""
    model = _default_model(root, profile)
    mode = model.rsplit("-", 1)[-1] if "-" in model else ""
    return mode if mode in ("quick", "deep") else ""


def install(profile: str = None, tools: bool = False, bridge: bool = True,
            use_default: bool = False, mode: str = None, harness: bool = True) -> str:
    """Install the whole link: pinned package, profile home, provider route, credential, MCP tools
    when asked, bridge, and the default model. `use_default` keeps the mode of a Bob default already
    in place (quick stays quick); `mode` sets one explicitly."""
    import generate
    from bob_core import load_config

    harness_line = ensure_dsh() if harness else ""
    home_line = ensure_home(profile)
    root = home()
    if not root.is_dir():
        prefix = "\n".join(x for x in (harness_line, home_line) if x)
        return ((prefix + "\n") if prefix else "") + _missing_home()
    cfg = load_config()
    generate.configure(cfg)
    generate.gen_dsh()
    lines = []
    if harness_line:
        lines.append(harness_line)
    if home_line:
        lines.append(home_line)
    lines += [generate._install_dsh_settings(root), generate._install_dsh_credential(root)]
    target = profile or default_profile(root)
    if tools:
        lines.append(_install_mcp(root))
    if bridge:
        lines.append(bridge_on(target))
    if mode:
        lines.append(f"profile {target}: " + _guarded("default model", lambda: set_mode(target, mode)))
    elif use_default:
        kept = _current_mode(root, target)
        model = _choose_model(kept) if kept else DEFAULT_MODEL
        lines.append(f"profile {target}: "
                     + _guarded("default model", lambda: set_default_model(target, model, root)))
    return "Installed bob dsh link\n" + "\n".join(f"  {ln}" for ln in lines)


def refresh(profile: str = None, tools: bool = False) -> str:
    """The `bob update` pass: bring every part of the link that is already present up to date, and add
    nothing the user removed. Upgrades an installed dsh older than the pin (never installs one), and
    refreshes the provider route and credential when the route is there, the MCP entry when `tools`
    (generate.dsh_tools_enabled), the bridge code when the bridge is installed, and the default model only when it
    is a Bob model, keeping its quick/deep mode."""
    import generate
    from bob_core import load_config

    lines = [ensure_dsh(install_missing=False)]
    root = home()
    if not root.is_dir():
        lines.append(f"no dsh home at {root}; nothing to refresh (`bob dsh install` sets it up)")
        return "Refreshed bob dsh link\n" + "\n".join(f"  {ln}" for ln in lines)
    cfg = load_config()
    generate.configure(cfg)
    generate.gen_dsh()
    target = profile or default_profile(root)
    settings = _load_yaml(root / "settings.yaml") or {}
    providers = ((settings.get("llm-pi-ai") or {}).get("providers") or {}) if isinstance(settings, dict) else {}
    if PROVIDER in providers or _credential_ok(root):
        lines += [generate._install_dsh_settings(root), generate._install_dsh_credential(root)]
    else:
        lines.append("provider route not present; left off")
    if tools:
        lines.append(_install_mcp(root))
    if _bridge_installed(root, target):
        lines.append(bridge_on(target).strip())
    current = _default_model(root, target)
    kept = _current_mode(root, target)
    if kept and current not in _route_models():
        # The Bob model this profile defaults to left the route (the hardware profile changed): move it
        # to the same mode on what the route serves now.
        model = _choose_model(kept)
        lines.append(f"profile {target}: "
                     + _guarded("default model", lambda: set_default_model(target, model, root)))
    return "Refreshed bob dsh link\n" + "\n".join(f"  {ln}" for ln in lines)


def _provider_route_ok(root: Path) -> bool:
    data = _load_yaml(root / "settings.yaml") or {}
    route = (((data.get("llm-pi-ai") or {}).get("providers") or {}).get(PROVIDER) or {})
    return bool(route.get("models"))


def _credential_ok(root: Path) -> bool:
    path = root / ".credentials.yaml"
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return bool(re.search(rf"^\s+BOB_LITELLM_KEY\s*:\s*\S", text, re.M))


def _plugin_present(path: Path, plugin_id: str) -> bool:
    if not path.exists():
        return False
    id_line = re.compile(rf"\s*-\s+id:\s*['\"]?{re.escape(plugin_id)}['\"]?\s*(#.*)?$")
    return any(id_line.match(ln) for ln in path.read_text(encoding="utf-8", errors="replace").splitlines())


def _default_model(root: Path, profile: str) -> str:
    data = _load_yaml(_profile_patch(profile, root))
    if not isinstance(data, list):
        return ""
    for item in data:
        if isinstance(item, dict) and item.get("id") == "agent-default-model":
            cfg = item.get("config") or {}
            if cfg.get("provider") == PROVIDER:
                return str(cfg.get("model") or "")
    return ""


def _patch_parses(path: Path) -> bool:
    """Whether a dsh patch file is valid YAML (parsed, not constructed, so `!!js` tags need no
    constructor). A missing file is valid: dsh treats it as an empty layer."""
    import yaml
    if not path.exists():
        return True
    try:
        yaml.compose(path.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def _bridge_bundle_listed(root: Path, profile: str) -> bool:
    bundles = ((_load_json(_profile_package(profile, root)).get("dsh") or {}).get("profile") or {}).get("bundles")
    return isinstance(bundles, list) and BRIDGE_ID in bundles


def health(config: dict = None, profile: str = None) -> list:
    """The DSH link checks, as (label, state, note) rows with state "ok", "bad" (broken: note says how to
    fix it) or "info" (optional or off by choice). The one source for `bob dsh doctor` and `bob doctor`."""
    import generate
    if config is None:
        from bob_core import load_config
        config = load_config()
    if not link_enabled(config):
        return [("DeepSeek Harness link", "info", "off (agent.dshEnabled is false); bob dsh install")]
    exe = dsh_bin()
    if not exe:
        return [("DeepSeek Harness", "info", f"not installed (optional); bob dsh install "
                                             f"installs @deepseek-ai/dsh@{pinned_dsh_version()}")]
    root = home()
    target = profile or default_profile(root)
    have, want = dsh_version(), pinned_dsh_version()
    hk, wk = _version_key(have), _version_key(want)
    rows = [("dsh installed", "bad" if (hk and wk and hk < wk) else "ok",
             f"{have or exe}" + (f", older than the pinned {want}; bob update" if hk and wk and hk < wk else ""))]
    if not root.is_dir():
        return rows + [("dsh home", "bad", f"{root} missing; bob dsh install")]
    for label, path in (("home patch", root / "cordis.patch.yml"),
                        (f"profile {target} patch", _profile_patch(target, root))):
        if not _patch_parses(path):
            rows.append((label, "bad", f"{path} is not valid YAML, so dsh will not start; fix it by hand"))
    rows.append(("bob provider route", "ok" if _provider_route_ok(root) else "bad",
                 "settings.yaml" if _provider_route_ok(root) else "missing; bob dsh install"))
    rows.append(("bob credential", "ok" if _credential_ok(root) else "bad",
                 ".credentials.yaml" if _credential_ok(root) else "missing; bob dsh install"))
    wired = _plugin_present(root / "cordis.patch.yml", MCP_ID)
    wanted = generate.dsh_tools_enabled(config)
    if wired and wanted:
        rows.append(("bob tools in dsh", "ok", "on"))
    elif wired:
        rows.append(("bob tools in dsh", "bad", "wired into dsh but Bob's MCP server is off, so every tool "
                                                "call fails; bob dsh tools on (or off)"))
    elif wanted:
        rows.append(("bob tools in dsh", "bad", "enabled but not wired into dsh; bob dsh tools on"))
    else:
        rows.append(("bob tools in dsh", "info", "off (optional); bob dsh tools on"))
    if _bridge_bundle_listed(root, target):
        rows.append(("session bridge", "bad", f"listed as a profile bundle, so dsh will not start; "
                                              f"bob dsh bridge on --profile {target}"))
    elif _bridge_installed(root, target):
        rows.append(("session bridge", "ok", f"on (profile {target})"))
    else:
        rows.append(("session bridge", "info", f"off (profile {target}); bob dsh bridge on"))
    model = _default_model(root, target)
    rows.append(("dsh default model", "ok" if model else "info",
                 f"{model} (profile {target})" if model else f"not Bob's (profile {target}); bob dsh use"))
    return rows


def doctor(profile: str = None) -> str:
    rows = health(profile=profile)
    width = max(len(label) for label, _state, _note in rows)
    mark = {"ok": "OK  ", "bad": "FAIL", "info": "--  "}
    return "bob dsh doctor\n" + "\n".join(f"  {mark[state]} {label:<{width}}  {note}"
                                             for label, state, note in rows)


def status(profile: str = None) -> str:
    root = home()
    target = profile or default_profile(root)
    return "\n".join([
        "bob dsh status",
        f"  dsh:      {dsh_bin() or 'not found'} {dsh_version()}".rstrip(),
        f"  home:     {root}",
        f"  profile:  {target}",
        f"  provider: {'bob' if _provider_route_ok(root) else 'not configured'}",
        f"  tools:    {'on' if _plugin_present(root / 'cordis.patch.yml', MCP_ID) else 'off'}",
        f"  bridge:   {bridge_status(target).split(': ', 1)[-1]}",
        f"  model:    {_default_model(root, target) or 'not set to bob'}",
    ])


def uninstall(profile: str = None) -> str:
    """Remove Bob's MCP entry and bridge, and set agent.dshEnabled=false so setup, update and `bob gen`
    leave dsh alone until `bob dsh install`."""
    root = home()
    removed = _remove_mcp_entries(root)
    lines = ["removed " + ", ".join(removed) if removed else "bob MCP tools were not installed",
             bridge_off(profile)]
    try:
        lines.append(_set_agent_flags(dshEnabled=False))
    except ConfigWriteRefused as e:
        lines.append(f"agent.dshEnabled not persisted, so setup/update will re-add the link: {e}")
    if _provider_route_ok(root):
        lines.append("provider route left in settings.yaml; remove the bob provider by hand if desired")
    return "\n".join(lines)


_HELP = """usage: bob dsh <command> [--profile NAME]

Set up (stop `dsh web` first, and have Bob running: bob up):
  install [--tools] [--no-use] [--no-bridge] [--mode quick|deep]
                      route + key + session bridge + Bob as the dsh default model;
                      --tools also gives dsh Bob's tools, --no-use keeps your default model
  tools on|off        Bob's tools in dsh (off leaves Bob's MCP server on for other clients)
  bridge on|off       import dsh sessions into Bob's memory
  use                 make Bob (coder-deep) the dsh default model
  mode quick|deep     switch the default between Bob's -quick and -deep aliases
  trust [--tier read|write|execute|all] [--project [PATH]] [--off] [TOOL ...]
                      which Bob tools dsh may run unattended

Check:
  status              one-line view of the link
  doctor              every check, with the fix for anything broken (also in `bob doctor`)

Sessions:
  sessions list | show ID | consolidate ID | forget ID

Remove:
  uninstall           remove Bob's entries and stop setup/update from re-adding them

Then start dsh with: dsh web"""


def main(argv: list) -> int:
    args = list(argv)
    cmd = (args.pop(0).lower() if args else "status")
    profile = None
    if "--profile" in args:
        i = args.index("--profile")
        profile = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if cmd == "status":
        print(status(profile)); return 0
    if cmd == "doctor":
        print(doctor(profile)); return 0
    if cmd == "install":
        tools = "--tools" in args
        bridge = "--no-bridge" not in args
        use_default = "--no-use" not in args
        harness = "--no-harness" not in args
        mode = None
        if "--mode" in args:
            j = args.index("--mode")
            mode = args[j + 1] if j + 1 < len(args) else None
        flags = {} if link_enabled() else {"dshEnabled": True}
        if tools:
            # The same switches `bob dsh tools on` sets: without mcpEnabled, the `bob agent mcp` that dsh
            # starts refuses to serve, so the wired tools would fail on every call.
            flags.update(mcpEnabled=True, dshTools=True)
        enable = ""
        if flags:
            try:
                enable = _set_agent_flags(**flags) + "\n"
            except ConfigWriteRefused as e:
                print(f"dsh link not installed: {e}", file=sys.stderr)
                return 1
        print(enable + install(profile=profile, tools=tools, bridge=bridge, use_default=use_default,
                               mode=mode, harness=harness))
        return 0
    if cmd == "use":
        root = home()
        print(set_default_model(profile or default_profile(root), DEFAULT_MODEL)); return 0
    if cmd == "mode":
        mode = args[0] if args else "deep"
        root = home()
        print(set_mode(profile or default_profile(root), mode)); return 0
    if cmd == "tools":
        sub = args[0] if args else "status"
        if sub == "on":
            print(tools_on(profile)); return 0
        if sub == "off":
            print(tools_off(profile)); return 0
        root = home()
        print("tools: " + ("on" if _plugin_present(root / "cordis.patch.yml", MCP_ID) else "off"))
        return 0
    if cmd == "bridge":
        sub = args[0] if args else "status"
        if sub == "on":
            print(bridge_on(profile)); return 0
        if sub == "off":
            print(bridge_off(profile)); return 0
        print(bridge_status(profile)); return 0
    if cmd == "sessions":
        sub = args[0] if args else "list"
        if sub == "list":
            print(sessions_list()); return 0
        if sub == "show" and len(args) > 1:
            print(sessions_show(args[1])); return 0
        if sub == "consolidate" and len(args) > 1:
            print(sessions_consolidate(args[1])); return 0
        if sub == "forget" and len(args) > 1:
            print(sessions_forget(args[1])); return 0
        print("usage: bob dsh sessions <list|show <id>|consolidate <id>|forget <id>>", file=sys.stderr)
        return 2
    if cmd == "trust":
        if "--tiers" in args or "--list-tiers" in args:
            print("trust tiers: " + trust_tier_names()); return 0
        if "--status" in args or not args:
            print(trust_status()); return 0
        off = "--off" in args
        all_tools = "--all" in args
        scope = "project" if "--project" in args else "global"
        project = None
        if "--project" in args:
            i = args.index("--project")
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                project = args[i + 1]
        tier = None
        if "--tier" in args:
            j = args.index("--tier")
            if j + 1 >= len(args):
                print("usage: bob dsh trust --tier <read|write|execute|all>", file=sys.stderr); return 2
            tier = args[j + 1]
        skip = {"--off", "--all", "--global", "--project", "--tier", "--status"}
        skip_values = {project, tier}
        names = [a for a in args if a not in skip and a not in skip_values and not a.startswith("--")]
        print(trust_tools(names, all_tools=all_tools, off=off, tier=tier, scope=scope, project=project))
        return 0
    if cmd == "import-session":
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw or "{}")
        except Exception as e:
            print(f"invalid import payload: {e}", file=sys.stderr)
            return 2
        try:
            result = import_session(payload)
        except Exception as e:
            print(f"session import failed: {e}", file=sys.stderr)
            return 1
        skipped = f", skipped {result['skipped']} malformed" if result.get("skipped") else ""
        print(f"imported {result['sessions']} session(s), {result['new_turns']} new of "
              f"{result['turns']} turn(s){skipped}")
        return 0
    if cmd == "logs":
        print(f"dsh home: {home()}")
        print("Start the web UI with: dsh web")
        return 0
    if cmd == "uninstall":
        print(uninstall(profile)); return 0
    if cmd in ("help", "-h", "--help"):
        print(_HELP)
        return 0
    print(f"unknown dsh command: {cmd}", file=sys.stderr)
    return 2
