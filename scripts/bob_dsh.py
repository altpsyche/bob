"""DeepSeek Harness (dsh) link management for Bob — one writer for every Bob-owned dsh entry.

Ownership map (no key is written to two layers):
  $DSH_HOME/settings.yaml                    llm-pi-ai.providers.bob
  $DSH_HOME/.credentials.yaml                BOB_LITELLM_KEY
  $DSH_HOME/cordis.patch.yml                 Bob MCP plugin entry
  $DSH_HOME/profiles/<name>/cordis.patch.yml agent-default-model and bob-dsh-bridge

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


def _top_level_items(lines: list) -> list:
    """(start, end) line spans for top-level `- ` items in a YAML sequence."""
    starts = [i for i, ln in enumerate(lines) if ln.startswith("- ")]
    spans = []
    for n, i in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        while end > i + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        spans.append((i, end))
    return spans


def _plugin_entry_lines(plugin_id: str, name: str, config_lines: list) -> list:
    out = ["- insert:", f"    - id: {plugin_id}", f"      name: '{name}'", "      config:"]
    out += [f"        {ln}" for ln in config_lines]
    return out


def _upsert_plugin(path: Path, plugin_id: str, entry: list) -> str:
    """Insert or replace one top-level insert item by id, preserving every other byte."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    id_line = re.compile(rf"\s*-\s+id:\s*['\"]?{re.escape(plugin_id)}['\"]?\s*(#.*)?$")
    for start, end in reversed(_top_level_items(lines)):
        if any(id_line.match(ln) for ln in lines[start:end]):
            del lines[start:end]
            while lines and not lines[-1].strip():
                lines.pop()
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            return True
    return False


def _default_model_entry(model: str) -> list:
    return _plugin_entry_lines(
        "agent-default-model", "@deepseek-ai/dsh-agent-default-model",
        [f"provider: {PROVIDER}", f"model: {model}"])


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


def _set_mcp_enabled(enabled: bool) -> str:
    """Set agent.mcpEnabled in the JSON user overlay. TOML overlays are left to the user."""
    import bob_config
    path = bob_config.user_config_path()
    if path is not None and path.suffix == ".toml":
        return f"set agent.mcpEnabled = true in {path} by hand, then re-run"
    json_path = REPO / "config" / "user.json"
    try:
        data = json.loads(json_path.read_text(encoding="utf-8")) if json_path.exists() else {}
    except Exception:
        data = {}
    agent = data.setdefault("agent", {})
    agent["mcpEnabled"] = bool(enabled)
    json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f"config/user.json: agent.mcpEnabled={str(bool(enabled)).lower()}"


def tools_on(profile: str = None) -> str:
    root = home()
    if not root.is_dir():
        return _missing_home()
    return _set_mcp_enabled(True) + "\n" + _install_mcp(root)


def tools_off(profile: str = None) -> str:
    root = home()
    removed = []
    if _remove_plugin(root / "cordis.patch.yml", MCP_ID):
        removed.append(str(root / "cordis.patch.yml"))
    for name in profiles(root):
        if _remove_plugin(_profile_patch(name, root), MCP_ID):
            removed.append(str(_profile_patch(name, root)))
    return "removed " + ", ".join(removed) if removed else "bob MCP tools were not installed"


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
    patch = _profile_patch(target, root)
    pkg = _load_json(_profile_package(target, root))
    deps = pkg.get("dependencies") or {}
    bundles = ((pkg.get("dsh") or {}).get("profile") or {}).get("bundles") or []
    installed = _plugin_present(patch, BRIDGE_ID) and (BRIDGE_ID in deps or BRIDGE_ID in bundles)
    return f"bob-dsh-bridge: {'on' if installed else 'off'} (profile {target})"


def bridge_on(profile: str = None) -> str:
    root = home()
    if not root.is_dir():
        return _missing_home()
    if not (BRIDGE_PACKAGE / "package.json").exists():
        return f"bridge package missing at {BRIDGE_PACKAGE}"
    target = profile or default_profile(root)
    dsh = dsh_bin()
    lines = []
    if dsh:
        try:
            r = subprocess.run([dsh, "plugin", "--profile", target, "add", f"file:{BRIDGE_PACKAGE}"],
                               capture_output=True, text=True, timeout=300)
            lines.append("pnpm: " + (r.stdout or r.stderr or "").strip().splitlines()[-1]
                         if (r.stdout or r.stderr).strip() else f"pnpm exit {r.returncode}")
        except Exception as e:
            lines.append(f"pnpm: {e}")
    try:
        pkg_path = _profile_package(target, root)
        pkg = _load_json(pkg_path)
        deps = pkg.setdefault("dependencies", {})
        deps[BRIDGE_ID] = f"file:{BRIDGE_PACKAGE}"
        prof = pkg.setdefault("dsh", {}).setdefault("profile", {})
        bundles = prof.setdefault("bundles", [])
        if BRIDGE_ID not in bundles:
            bundles.append(BRIDGE_ID)
        _write_json(pkg_path, pkg)
        patch = _profile_patch(target, root)
        _remove_plugin(patch, HOOK_ID)
        _upsert_plugin(patch, BRIDGE_ID, _plugin_entry_lines(BRIDGE_ID, BRIDGE_ID, ["bobCommand: bob"]))
        lines.append(f"profile {target}: installed {BRIDGE_ID}")
    except Exception as e:
        lines.append(f"profile {target}: could not write the bridge entry ({e})")
    return "\n".join(f"  {ln}" for ln in lines)


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
        prof = (pkg.get("dsh") or {}).get("profile") or {}
        bundles = prof.get("bundles") or []
        if BRIDGE_ID in bundles:
            bundles.remove(BRIDGE_ID)
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
    """Forget one imported DSH session: raw events, derived transcript, and any memory it produced."""
    import bob_memory
    from bob_core import _get_db_path, load_config
    cfg = load_config()
    db_path = Path(_get_db_path(cfg))
    owner = cfg.get("agent", {}).get("defaultOwner", "local")
    facts = 0
    turns = 0
    if db_path.exists():
        # Provenance-based memory forget first, then the transcript (it is not audit-retained).
        try:
            facts = bob_memory.forget_by_session(session_id, db_path, owner=owner)
        except Exception:
            facts = 0
        try:
            turns = bob_memory.forget_transcript_session(session_id, db_path, owner=owner)
        except Exception:
            turns = 0
    db, _cfg, _path = _dsh_db()
    if db is not None:
        try:
            if _dsh_tables(db):
                db.execute("DELETE FROM dsh_events WHERE session_id=?", [session_id])
                db.execute("DELETE FROM dsh_sessions WHERE session_id=?", [session_id])
                db.commit()
        finally:
            db.close()
    return (f"forgot DSH session {session_id}: {turns} transcript turn(s), "
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


def trust_tiers(registry) -> dict:
    """The DSH trust tiers as concrete allow-sets, derived from the live registry.

    read    -- no state-changing tools
    write   -- state-changing tools that are not command execution, delegation, scheduling or remote MCP
    execute -- write + command-execution tools
    all     -- every gated tool, including delegation and remote MCP
    """
    gated = _gated_tools(registry)
    dangerous = {"spawn_agent", "schedule_run"} | set(getattr(registry, "remote_tools", set()))
    execute = {n for n in gated if n not in dangerous}
    # `write` is the coding-loop subset: mutations, but not tools whose whole point is running commands
    # or crossing an external boundary. Approval-required tools (shell_run, ...) are command execution.
    write = {n for n in execute
             if n not in getattr(registry, "approval_required_tools", set())}
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
    project_spec = None
    if cwd and isinstance(projects, dict):
        project_spec = projects.get(str(Path(cwd).resolve()))
    active = project_spec if project_spec is not None else trust.get("global")
    return base | _trust_spec_tools(active, tiers)


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


def _trust_config_path() -> Path:
    import bob_config
    path = bob_config.user_config_path()
    return Path(path) if path is not None else (REPO / "config" / "user.json")


def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_dict(path: Path, data: dict) -> None:
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".user-", suffix=".json.tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    path = _trust_config_path()
    if path.suffix == ".toml":
        return (f"edit agent.dshTrust and agent.mcpAllowTools in {path} by hand, then re-run "
                f"(tiers: {trust_tier_names()})")
    data = _load_json_dict(path)
    agent = data.setdefault("agent", {})
    trust = agent.setdefault("dshTrust", {})
    if not isinstance(trust, dict):
        trust = agent["dshTrust"] = {}
    projects = trust.setdefault("projects", {})
    if not isinstance(projects, dict):
        projects = trust["projects"] = {}
    key = _project_key(project)
    if off:
        if scope == "project":
            projects.pop(key, None)
            result = f"cleared trust tier for project {key}"
        else:
            trust.pop("global", None)
            agent["mcpAllowTools"] = []
            result = "cleared global trust tier and mcpAllowTools"
    elif all_tools:
        spec = "all"
        if scope == "project":
            projects[key] = spec
        else:
            trust["global"] = spec
        result = f"trust tier {spec} set for {'project ' + key if scope == 'project' else 'all projects'}"
    elif tier:
        normalized = _TRUST_TIER_ALIASES.get(str(tier).strip().lower())
        if normalized not in _TRUST_TIERS:
            return f"unknown trust tier '{tier}' (known: {trust_tier_names()})"
        if scope == "project":
            projects[key] = normalized
        else:
            trust["global"] = normalized
        result = f"trust tier {normalized} set for {'project ' + key if scope == 'project' else 'all projects'}"
    elif tools:
        names = sorted({str(t) for t in tools if str(t).strip()})
        if scope == "project":
            current = projects.get(key)
            current_names = set(current) if isinstance(current, (list, tuple, set)) else set()
            projects[key] = sorted(current_names | set(names))
        else:
            current = {str(n) for n in (agent.get("mcpAllowTools") or []) if str(n).strip()}
            agent["mcpAllowTools"] = sorted(current | set(names))
        result = f"trusted {', '.join(names)} for {'project ' + key if scope == 'project' else 'all projects'}"
    else:
        return trust_status()
    # Drop an empty trust block so an unconfigured install keeps the old mcpAllowTools-only behavior.
    if not trust.get("global") and not projects:
        agent.pop("dshTrust", None)
    _write_json_dict(path, data)
    return f"{path}: {result}"


def import_session(payload: dict, config: dict = None) -> dict:
    """Import one native-bridge payload through the one Bob transcript pipeline."""
    from bob_core import _get_db_path, load_config
    import bob_memory
    cfg = config or load_config()
    owner = cfg.get("agent", {}).get("defaultOwner", "local")
    return bob_memory.dsh_import_sessions(
        payload.get("sessions") or [], _get_db_path(cfg), owner=owner)


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


def ensure_dsh() -> str:
    """Install or upgrade the pinned DeepSeek Harness when a package manager is available.

    Bob never guesses a floating version: the exact version comes from versions.lock.
    """
    want = pinned_dsh_version()
    have = dsh_version()
    if have and want in have:
        return f"dsh {have} already installed"
    manager = shutil.which("pnpm") or shutil.which("npm")
    if not manager:
        return ("dsh not installed and no pnpm/npm found. Install Node.js + pnpm, then run: "
                f"pnpm add -g @deepseek-ai/dsh@{want}")
    name = "pnpm" if Path(manager).name.lower().startswith("pnpm") else "npm"
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
    return f"installed @deepseek-ai/dsh@{want} with {name}"


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


def install(profile: str = None, tools: bool = False, bridge: bool = True,
            use_default: bool = False, mode: str = None, harness: bool = True) -> str:
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
        lines.append(f"profile {target}: {set_mode(target, mode)}")
    elif use_default:
        lines.append(f"profile {target}: {set_default_model(target, DEFAULT_MODEL)}")
    return "Installed bob dsh link\n" + "\n".join(f"  {ln}" for ln in lines)


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


def doctor(profile: str = None) -> str:
    root = home()
    target = profile or default_profile(root)
    rows = [
        ("dsh binary", bool(dsh_bin()), dsh_version() or dsh_bin() or "not found"),
        ("dsh home", root.is_dir(), str(root)),
        ("bob provider route", _provider_route_ok(root), "settings.yaml"),
        ("bob credential", _credential_ok(root), ".credentials.yaml"),
        ("bob MCP tools", _plugin_present(root / "cordis.patch.yml", MCP_ID),
         "home cordis.patch.yml"),
        ("bob native bridge", _plugin_present(_profile_patch(target, root), BRIDGE_ID),
         f"profile {target}"),
        ("default model", bool(_default_model(root, target)),
         f"profile {target}: {_default_model(root, target) or 'not set to bob'}"),
        ("profile patch", _profile_patch(target, root).exists(), str(_profile_patch(target, root))),
    ]
    width = max(len(name) for name, _ok, _detail in rows)
    lines = []
    for name, ok, detail in rows:
        lines.append(f"  {'OK ' if ok else 'WARN'} {name:<{width}}  {detail}")
    return "bob dsh doctor\n" + "\n".join(lines)


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
    root = home()
    lines = [tools_off(profile), bridge_off(profile)]
    if _provider_route_ok(root):
        lines.append("provider route left in settings.yaml; remove the bob provider by hand if desired")
    return "\n".join(lines)


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
        print(install(profile=profile, tools=tools, bridge=bridge, use_default=use_default, mode=mode,
                      harness=harness))
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
        print(f"imported {result['sessions']} session(s), {result['turns']} turn(s)")
        return 0
    if cmd == "logs":
        print(f"dsh home: {home()}")
        print("Start the web UI with: dsh web")
        return 0
    if cmd == "uninstall":
        print(uninstall(profile)); return 0
    if cmd in ("help", "-h", "--help"):
        print("usage: bob dsh <status|doctor|install|use|mode|tools|trust|bridge|sessions|logs|uninstall>")
        return 0
    print(f"unknown dsh command: {cmd}", file=sys.stderr)
    return 2
