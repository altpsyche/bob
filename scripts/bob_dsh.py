"""DeepSeek Harness (dsh) link management for Bob — one writer for every Bob-owned dsh entry.

Ownership map (no key is written to two layers):
  $DSH_HOME/settings.yaml                    llm-pi-ai.providers.bob
  $DSH_HOME/.credentials.yaml                BOB_LITELLM_KEY
  $DSH_HOME/cordis.patch.yml                 Bob MCP and hook plugin entries
  $DSH_HOME/profiles/<name>/cordis.patch.yml agent-default-model

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
DEFAULT_MODEL = "coder-deep"
FALLBACK_MODEL = "chat-deep"
PROVIDER = "bob"
MCP_ID = "bob-tools"
HOOK_ID = "bob-hooks"
HOOK_PLUGIN = "@deepseek-ai/dsh-hooks-claude-code"


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


def _hook_config_path() -> Path:
    return CONFIG_DSH / "hooks.json"


def _write_hooks_config() -> str:
    hooks = {
        "hooks": {
            "SessionStart": [{
                "matcher": "startup|resume|clear|compact",
                "hooks": [{"type": "command", "command": "bob dsh hook session-start"}],
            }],
            "Stop": [{
                "hooks": [{"type": "command", "command": "bob dsh hook stop"}],
            }],
        }
    }
    CONFIG_DSH.mkdir(parents=True, exist_ok=True)
    _hook_config_path().write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    return str(_hook_config_path())


def _hook_entry_lines() -> list:
    return _plugin_entry_lines(HOOK_ID, HOOK_PLUGIN, [f"configPath: {_hook_config_path()}"])


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


def tools_on(profile: str = None) -> str:
    root = home()
    if not root.is_dir():
        return _missing_home()
    return _install_mcp(root)


def tools_off(profile: str = None) -> str:
    root = home()
    removed = []
    if _remove_plugin(root / "cordis.patch.yml", MCP_ID):
        removed.append(str(root / "cordis.patch.yml"))
    for name in profiles(root):
        if _remove_plugin(_profile_patch(name, root), MCP_ID):
            removed.append(str(_profile_patch(name, root)))
    return "removed " + ", ".join(removed) if removed else "bob MCP tools were not installed"


def hooks_on() -> str:
    root = home()
    if not root.is_dir():
        return _missing_home()
    _write_hooks_config()
    return _upsert_plugin(root / "cordis.patch.yml", HOOK_ID, _hook_entry_lines())


def hooks_off() -> str:
    root = home()
    removed = []
    if _remove_plugin(root / "cordis.patch.yml", HOOK_ID):
        removed.append(str(root / "cordis.patch.yml"))
    for name in profiles(root):
        if _remove_plugin(_profile_patch(name, root), HOOK_ID):
            removed.append(str(_profile_patch(name, root)))
    return "removed " + ", ".join(removed) if removed else "bob DSH hooks were not installed"


def _missing_home() -> str:
    return (f"no DeepSeek Harness home at {home()}. Install dsh first: "
            "npm i -g @deepseek-ai/dsh, then run `dsh web` once, then `bob dsh install`.")


def install(profile: str = None, tools: bool = False, hooks: bool = False,
            use_default: bool = False, mode: str = None) -> str:
    import generate
    from bob_core import load_config

    root = home()
    if not root.is_dir():
        return _missing_home()
    cfg = load_config()
    generate.configure(cfg)
    generate.gen_dsh()
    lines = [generate._install_dsh_settings(root), generate._install_dsh_credential(root)]
    target = profile or default_profile(root)
    if tools:
        lines.append(_install_mcp(root))
    if hooks:
        lines.append(hooks_on())
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
        ("bob context hooks", _plugin_present(root / "cordis.patch.yml", HOOK_ID),
         "home cordis.patch.yml"),
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
        f"  hooks:    {'on' if _plugin_present(root / 'cordis.patch.yml', HOOK_ID) else 'off'}",
        f"  model:    {_default_model(root, target) or 'not set to bob'}",
    ])


def uninstall(profile: str = None) -> str:
    root = home()
    lines = [tools_off(profile), hooks_off()]
    if _provider_route_ok(root):
        lines.append("provider route left in settings.yaml; remove the bob provider by hand if desired")
    return "\n".join(lines)


def _hook_context() -> str:
    try:
        from bob_core import (core_blocks_block, load_config, memory_profile_block,
                              project_memory_block)
        cfg = load_config()
        cwd = os.getcwd()
        owner = cfg.get("agent", {}).get("defaultOwner", "local")
        parts = []
        for block in (memory_profile_block(owner=owner, config=cfg),
                      project_memory_block(cwd, config=cfg),
                      core_blocks_block(owner=owner, scope=cwd, config=cfg)):
            if block:
                parts.append(block)
        return "\n\n".join(parts)
    except Exception:
        return ""


def hook(event: str, stdin_text: str = None) -> int:
    """Handle one DSH/Claude-Code hook event. Only SessionStart emits context today."""
    event = (event or "").strip().lower()
    if event != "session-start":
        return 0
    raw = stdin_text if stdin_text is not None else sys.stdin.read()
    try:
        json.loads(raw or "{}")
    except Exception:
        return 0
    context = _hook_context()
    if context:
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }))
    return 0


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
        hooks = "--hooks" in args
        use_default = "--no-use" not in args
        mode = None
        if "--mode" in args:
            j = args.index("--mode")
            mode = args[j + 1] if j + 1 < len(args) else None
        print(install(profile=profile, tools=tools, hooks=hooks, use_default=use_default, mode=mode))
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
    if cmd == "hooks":
        sub = args[0] if args else "status"
        if sub == "on":
            print(hooks_on()); return 0
        if sub == "off":
            print(hooks_off()); return 0
        root = home()
        print("hooks: " + ("on" if _plugin_present(root / "cordis.patch.yml", HOOK_ID) else "off"))
        return 0
    if cmd == "hook":
        return hook(args[0] if args else "")
    if cmd == "logs":
        print(f"dsh home: {home()}")
        print("Start the web UI with: dsh web")
        return 0
    if cmd == "uninstall":
        print(uninstall(profile)); return 0
    if cmd in ("help", "-h", "--help"):
        print("usage: bob dsh <status|doctor|install|use|mode|tools|hooks|logs|uninstall>")
        return 0
    print(f"unknown dsh command: {cmd}", file=sys.stderr)
    return 2
