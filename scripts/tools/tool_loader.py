"""Auto-discover and load Bob tool modules.

System tools live in scripts/tools/<name>.py.
User tools live in plugins/<name>/tool.py.

Each tool file must export:
  TOOL_DEFS  list[dict]         OpenAI function-calling schemas
  DISPATCH   dict[str,callable] name -> function
  configure(config: dict)       called once at startup with full config

Optionally:
  test() -> str   called by `bob tools test <name>`
"""
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent

_SKIP_STEMS = frozenset({"tool_loader", "tool_registry"})


def _load_module(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"bob_tool_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def discover_tools(config: dict, disabled_names: set = None) -> tuple[list, dict, set]:
    """Load and configure all enabled tool modules. Returns (tool_schemas, dispatch, exit_voice_tools).

    Thin wrapper over ToolRegistry.build() for backward compatibility.
    disabled_names: set of tool names to skip (replaces the old enabled_names allowlist).
    """
    from tool_registry import ToolRegistry
    reg = ToolRegistry.build(config, disabled_names)
    return reg.tool_schemas, reg.dispatch, reg.exit_voice_tools


if __name__ == "__main__":
    import argparse
    import json

    from tool_registry import ToolRegistry
    # Discovery lives in the registry; the CLI reuses it (no second copy here).
    _iter_all_tools = ToolRegistry.iter_all_tools

    parser = argparse.ArgumentParser(description="Bob tool loader CLI")
    parser.add_argument("--list", action="store_true", help="List all available tools")
    parser.add_argument("--test", metavar="NAME", help="Run a tool's test() function")
    parser.add_argument("--info", metavar="NAME", help="Show tool schema")
    parser.add_argument(
        "--disabled",
        default="",
        help="Comma-separated tool names to mark as disabled in --list output",
    )
    args = parser.parse_args()

    # Load config for configure() calls
    _cfg: dict = {}
    try:
        sys.path.insert(0, str(REPO / "scripts"))
        from bob_core import load_config
        _cfg = load_config()
    except Exception:
        pass

    disabled_list = [t.strip() for t in args.disabled.split(",") if t.strip()]
    disabled_set = set(disabled_list)

    if args.list:
        print(f"{'Name':<20} {'Kind':<8} {'Status':<10} Description")
        print("-" * 70)
        for name, kind, path in _iter_all_tools():
            status = "disabled" if name in disabled_set else "enabled"
            try:
                mod = _load_module(name, path)
                first_desc = ""
                defs = getattr(mod, "TOOL_DEFS", [])
                if defs:
                    first_desc = defs[0].get("function", {}).get("description", "")[:40]
            except Exception as e:
                first_desc = f"(load error: {e})"
            print(f"{name:<20} {kind:<8} {status:<10} {first_desc}")

    elif args.test:
        name = args.test
        found = False
        for tname, kind, path in _iter_all_tools():
            if tname == name:
                found = True
                mod = _load_module(name, path)
                mod.configure(_cfg)
                if hasattr(mod, "test"):
                    result = mod.test()
                    print(result)
                else:
                    print(f"Tool '{name}' has no test() function.")
                break
        if not found:
            print(f"Tool '{name}' not found.")
            sys.exit(1)

    elif args.info:
        name = args.info
        found = False
        for tname, kind, path in _iter_all_tools():
            if tname == name:
                found = True
                mod = _load_module(name, path)
                defs = getattr(mod, "TOOL_DEFS", [])
                print(json.dumps(defs, indent=2))
                break
        if not found:
            print(f"Tool '{name}' not found.")
            sys.exit(1)
    else:
        parser.print_help()
