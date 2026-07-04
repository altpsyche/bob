"""NB4 (contract C1) — `python -m bob` dispatch. Resolves a command path against the registry and
routes: python commands are handled here; pwsh (orchestration/phased) commands are exec'd through
`scripts/bob.ps1` when PowerShell is available. Heavy runtime imports are lazy so `bob` help and
pwsh delegation stay light and dependency-free.
"""
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

from bob import registry

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/bob/cli.py -> repo
SCRIPTS = REPO / "scripts"


def _resolve(argv: list):
    """Return (command_name, remaining_args). Prefer a 2-token path (e.g. 'agent serve') over the
    bare verb (e.g. 'agent <goal>'), so subcommands split correctly per C1."""
    cmds = registry.by_name()
    if not argv:
        return (None, [])
    if len(argv) >= 2 and f"{argv[0]} {argv[1]}" in cmds:
        return (f"{argv[0]} {argv[1]}", argv[2:])
    return (argv[0], argv[1:])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    name, rest = _resolve(argv)
    if name is None:
        # NE2 Decision C — no-arg on an interactive terminal launches the shell; piped/redirected/CI
        # keeps today's help. (The Windows front door decides the same way in bob.ps1; the POSIX shim
        # sends the bare `bob` straight here.)
        from bob.shell import is_interactive
        if is_interactive():
            return _handle_shell([])
        _print_help()
        return 0

    entry = registry.by_name().get(name)
    runtime = entry["runtime"] if entry else registry.verbs_json_dict()["default"]

    if runtime == "pwsh":
        return _exec_pwsh(argv)

    handler = _HANDLERS.get(entry["handler"]) if entry else None
    if handler is None:
        print(f"Unknown command: {' '.join(argv)}\n", file=sys.stderr)
        _print_help()
        return 2
    return handler(rest) or 0


# --- python handlers -----------------------------------------------------------------------------

def _handle_agent_run(rest: list) -> int:
    if not rest:
        print("Usage: bob agent <goal>", file=sys.stderr)
        return 1
    import bob_loop  # lazy: pulls in openai etc.

    sys.argv = ["bob-agent"] + rest
    bob_loop.main()  # may sys.exit(42) on --exit-on-tool; that propagates as intended
    return 0


def _handle_agent_serve(rest: list) -> int:
    import uvicorn  # lazy

    import bob_agent_server  # noqa: F401 — defines the FastAPI `app`
    from bob_core import _port, capability_probe, load_config

    config = load_config()
    agent = config.get("agent", {})
    host = agent.get("serveHost", "127.0.0.1")
    port = _port(agent, "agentPort")

    ok, msg = capability_probe(config)
    print(f"[probe] {msg}", file=sys.stderr)  # degrade with a clear message, don't hard-fail
    print(f"Bob agent HTTP server on {host}:{port}  (POST /v1/agent/completions, Bearer auth)",
          file=sys.stderr)
    if host == "0.0.0.0":
        print("  WARNING: bound to 0.0.0.0 (LAN-exposed). Keep agent.allowPrivateFetch = false.",
              file=sys.stderr)
    uvicorn.run(bob_agent_server.app, host=host, port=port)
    return 0


def _handle_agent_mcp(rest: list) -> int:
    import bob_mcp_server  # lazy

    return bob_mcp_server.main() or 0


def _handle_agent_tools(rest: list) -> int:
    # tool_loader's CLI logic lives in its __main__ block and imports its siblings (tool_registry),
    # so scripts/tools must be importable; run it with the right argv.
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    sys.argv = ["tool_loader.py", "--list"] + rest
    runpy.run_path(str(SCRIPTS / "tools" / "tool_loader.py"), run_name="__main__")
    return 0


def _handle_clip(rest: list) -> int:
    import bob_clip  # lazy

    sys.argv = ["bob-clip"] + rest
    bob_clip.main()
    return 0


def _handle_skill(rest: list) -> int:
    """bob skill                 -> list registered skills (catalog)
       bob skill <name> [args…]  -> run a skill: a tool-sequence skill runs its steps; a sub-agent
                                    skill runs as an isolated agent sub-run (O11) with the args as input
       bob skill <name> --show   -> print the skill's manifest summary"""
    from bob import catalog
    from bob_skills import SkillRegistry

    reg = SkillRegistry.build()
    if not rest:
        print(catalog.render_skills(reg))
        return 0
    name = rest[0]
    if name not in reg.skills:
        print(f"Unknown skill: {name}", file=sys.stderr)
        return 1
    if "--show" in rest:
        s = reg.skills[name]
        print(f"{s['name']}: {s['description']}")
        print(f"  group: {s['group']}   steps: {len(s['steps'])}   dir: {s['dir']}")
        return 0
    from bob_core import load_config

    tools_dir = str(SCRIPTS / "tools")   # tool_registry lives in scripts/tools, not on path by default
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from tool_registry import ToolRegistry

    config = load_config()
    tools = ToolRegistry.build(config, set())
    args = " ".join(a for a in rest[1:] if not a.startswith("--"))   # skill input; flags aren't args
    print(reg.run(name, tools, config=config, args=args))
    return 0


def _handle_shell(rest: list) -> int:
    """bob shell — the interactive REPL/TUI (NE2). Behind an isatty gate: a non-TTY invocation prints
    help instead, so scripts/CI never block on a prompt."""
    from bob.shell import run

    return run()


def _handle_help(rest: list) -> int:
    """bob help — the single generated command catalog (WI-7), rendered from the registry so it can't
    drift. Grouped commands + drop-in plugins; the pwsh front door delegates here (no more here-string)."""
    from rich.console import Console

    from bob import render
    from bob.theme import Theme

    console = Console()
    theme = Theme.load(None, console)
    console.print(render.commands_view(theme))
    plugins = render.plugins_view(theme)
    if plugins is not None:
        console.print(plugins)
    console.print(f"\n[{theme.muted}]bob <command> [args] · run [bold]bob[/] with no args for the "
                  f"interactive shell · bob tools · bob skills[/]")
    return 0


_HANDLERS = {
    "agent_run": _handle_agent_run,
    "agent_serve": _handle_agent_serve,
    "agent_mcp": _handle_agent_mcp,
    "agent_tools": _handle_agent_tools,
    "clip": _handle_clip,
    "skill": _handle_skill,
    "shell": _handle_shell,
    "help": _handle_help,
}


# --- pwsh delegation -----------------------------------------------------------------------------

def _exec_pwsh(argv: list) -> int:
    pwsh = shutil.which("pwsh") or shutil.which("powershell")
    if not pwsh:
        print(f"`bob {' '.join(argv)}` is a PowerShell orchestration command and PowerShell "
              "(pwsh) is not installed on this system. See docs/PORTABILITY.md.", file=sys.stderr)
        return 1
    cmd = [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPTS / "bob.ps1")] + argv
    return subprocess.run(cmd).returncode


# --- help ----------------------------------------------------------------------------------------

_GROUP_ORDER = ["Talk", "Act", "Make", "Know", "Run", "Config"]


def _print_help() -> None:
    print("bob — local AI assistant\n", file=sys.stderr)
    groups: dict = {}
    for c in registry.commands(include_hidden=False):  # NE1 — hidden verbs stay out of the catalog
        groups.setdefault(c["group"], []).append(c)
    ordered = [g for g in _GROUP_ORDER if g in groups] + [g for g in groups if g not in _GROUP_ORDER]
    for group in ordered:
        cmds = groups[group]
        print(f"{group}:", file=sys.stderr)
        for c in cmds:
            usage = f"{c['name']} {c['args']}".strip()
            print(f"  {usage:<34} {c['summary']}", file=sys.stderr)
        print("", file=sys.stderr)
