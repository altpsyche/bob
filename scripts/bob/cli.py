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
    # ONE-C §1a — the deterministic invoker. `bob --run <cap> '{json}'` runs a capability through the
    # EXACT agent path (ToolRegistry.dispatch_call), no model, no parallel dispatcher, so CI/scripts and
    # the loop hit identical code. A mode flag (D5), not a verb — kept out of the registry/catalog.
    if argv and argv[0] == "--run":
        return _handle_run(argv[1:])
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


# --- the deterministic invoker (ONE-C §1a) -------------------------------------------------------

def _build_registry(config: dict):
    """Build the same ToolRegistry the agent loop builds (same disabledTools parsing, bob_loop.py:1147),
    so `--run` dispatches through an identical toolset. scripts/tools must be importable first."""
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from tool_registry import ToolRegistry

    disabled_raw = config.get("agent", {}).get("disabledTools", [])
    if isinstance(disabled_raw, str):
        disabled = {t.strip() for t in disabled_raw.split(",") if t.strip()}
    else:
        disabled = set(disabled_raw)
    return ToolRegistry.build(config, disabled, quiet=True)


def _handle_run(rest: list) -> int:
    """`bob --run <cap> '{json}'` — invoke one capability deterministically (D5: flag, JSON-only, single
    surface). Prints the tool's returned string; exits non-zero when the result is an error (unknown cap,
    bad JSON, or a tool error) so CI can gate on it. Same fn/dispatch as the loop — no parallel path."""
    if not rest:
        print("usage: bob --run <capability> ['{json args}']", file=sys.stderr)
        return 2
    cap = rest[0]
    args_json = rest[1] if len(rest) > 1 else "{}"
    import json as _json

    try:
        parsed = _json.loads(args_json)
    except _json.JSONDecodeError as e:
        print(f"bob --run: arguments must be a JSON object ({e})", file=sys.stderr)
        return 2
    if not isinstance(parsed, dict):
        print("bob --run: arguments must be a JSON object, e.g. '{\"query\": \"x\"}'", file=sys.stderr)
        return 2

    from bob.shell import _is_error_result
    from bob_core import load_config

    registry = _build_registry(load_config())
    result = registry.dispatch_call(cap, args_json)
    print(result)
    return 1 if _is_error_result(result) else 0


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


_CHAT_KNOWN_ROLES = {"chat", "coder", "planner", "fim", "embed",
                     "chat-pro", "coder-pro", "planner-pro"}


def _chat(task: str, rest: list) -> int:
    """Unified `bob chat|code|think` (S2 — one loop, no tools). One-shot when a prompt is given, else
    the interactive shell in chat mode. Preserves the pwsh flags: --pro/--think/--code role routing
    (via get_role), --max N, --raw, --sys <text>, and legacy `bob chat <role> <prompt>`."""
    from bob_core import get_role, load_config

    rest = list(rest)
    pro = "--pro" in rest
    raw = "--raw" in rest
    if "--think" in rest:
        task = "think"
    elif "--code" in rest:
        task = "code"

    max_tokens = None
    sys_prompt = None
    prompt: list = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("--pro", "--think", "--code", "--raw"):
            i += 1
        elif tok == "--max" and i + 1 < len(rest):
            try:
                max_tokens = int(rest[i + 1])
            except ValueError:
                max_tokens = None
            i += 2
        elif tok == "--sys" and i + 1 < len(rest):
            sys_prompt = rest[i + 1]
            i += 2
        else:
            prompt.append(tok)
            i += 1

    config = load_config()
    # Legacy `bob chat <knownRole> <prompt...>` — first token is an explicit model/role.
    if len(prompt) >= 2 and prompt[0] in _CHAT_KNOWN_ROLES:
        role = prompt[0]
        prompt = prompt[1:]
    else:
        role = get_role(config, task, pro=pro)
    if sys_prompt:
        config = {**config, "persona": {**config.get("persona", {}), "systemPrompt": sys_prompt}}

    if not prompt:
        # Interactive: the NE shell in chat mode (preset role, tools off) — inherits persisted
        # sessions, MEM-3/autoRecall/consolidate, rich streaming, and approval.
        from bob.shell import run as shell_run
        return shell_run(config=config, role=role, no_tools=True)

    # One-shot: run_agent prints the answer (streams + newline unless --raw, which prints bare text).
    import bob_loop
    bob_loop.run_agent(" ".join(prompt), config, role=role, agency="silent",
                       stream=not raw, no_tools=True, max_tokens=max_tokens)
    return 0


def _handle_chat(rest: list) -> int:
    return _chat("chat", rest)


def _handle_code(rest: list) -> int:
    return _chat("code", rest)


def _handle_think(rest: list) -> int:
    return _chat("think", rest)


def _describe(image: str, rest: list) -> int:
    """ONE-B2 — `bob describe <image> [--pro] [prompt]` on the agent loop: resize (cross-platform) →
    one-shot vision turn via run_agent(images=[…]). Replaces the pwsh handler + Invoke-BobStream +
    System.Drawing. Role is pinned to vision (so the loop uses it verbatim, no auto-route needed)."""
    from bob_core import get_role, load_config
    import bob_loop
    import bob_vision

    rest = list(rest)
    pro = "--pro" in rest
    rest = [t for t in rest if t != "--pro"]
    if not os.path.exists(image):
        print(f"File not found: {image}", file=sys.stderr)
        return 1
    prompt = " ".join(rest) if rest else "Describe this image."
    config = load_config()
    role = get_role(config, "vision", pro=pro)
    prepared = bob_vision.resize_image(image)
    try:
        bob_loop.run_agent(prompt, config, role=role, agency="silent",
                           stream=True, no_tools=True, images=[prepared])
    finally:
        if prepared != image:
            try:
                os.remove(prepared)
            except OSError:
                pass
    return 0


def _handle_describe(rest: list) -> int:
    rest = list(rest)
    positional = [t for t in rest if t != "--pro"]
    if not positional:
        print("usage: bob describe <image> [--pro] [prompt]", file=sys.stderr)
        return 1
    image = positional[0]
    rest_wo_image = list(rest)
    rest_wo_image.remove(image)   # drop the image path; _describe keeps --pro + prompt tokens
    return _describe(image, rest_wo_image)


def _handle_screenshot(rest: list) -> int:
    """ONE-B2 — `bob screenshot [--pro] [prompt]`: capture the screen (cross-platform), then describe
    it through the loop. Replaces the pwsh handler + System.Windows.Forms capture."""
    import bob_vision

    try:
        shot = bob_vision.capture_screen()
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 1
    try:
        return _describe(shot, rest)
    finally:
        try:
            os.remove(shot)
        except OSError:
            pass


def _handle_voice(rest: list) -> int:
    """bob voice [--pro] [--agent] — spoken conversation (ONE-B5). Launches the shell straight into
    /voice mode (mic→STT→loop→TTS): the pwsh voice loop + Invoke-BobStream are deleted, so voice now
    runs on the one engine and inherits memory + write-back + one persona + retry + logging + tools.
    Default is a plain chat-role conversation (no tools); --agent keeps the full agent toolset; --pro
    uses the pro voice model."""
    from bob.shell import run_voice
    from bob_core import get_role, load_config

    rest = list(rest)
    pro = "--pro" in rest
    config = load_config()
    if "--agent" in rest:
        return run_voice(config=config)                     # agent role + tools (shell default)
    return run_voice(config=config, role=get_role(config, "voice", pro=pro), no_tools=True)


def _handle_listen(rest: list) -> int:
    """bob listen — record the mic until silence, print the transcript (whisper). ONE-B5: the STT client
    is bob_voice (shared with the /voice mode); replaces the pwsh handler that shelled out to a script."""
    import bob_voice
    from bob_core import load_config

    print("Listening... (speak now; recording stops after silence)", file=sys.stderr)
    try:
        transcript = bob_voice.listen(load_config())
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not transcript:
        print("Error: no speech detected", file=sys.stderr)
        return 1
    print(transcript)
    return 0


def _handle_transcribe(rest: list) -> int:
    """bob transcribe <file> — transcribe an audio file via whisper-server. ONE-B5."""
    import bob_voice
    from bob_core import load_config

    if not rest:
        print("usage: bob transcribe <audio-file>", file=sys.stderr)
        return 1
    if not os.path.exists(rest[0]):
        print(f"File not found: {rest[0]}", file=sys.stderr)
        return 1
    try:
        transcript = bob_voice.transcribe(rest[0], bob_voice.stt_port(load_config()))
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if not transcript:
        print("Error: empty transcript", file=sys.stderr)
        return 1
    print(transcript)
    return 0


def _handle_speak(rest: list) -> int:
    """bob speak [text] — synthesize speech with piper, reading stdin if no text is given. ONE-B5: the
    TTS synth is bob_voice.speak (piper → osenv.play_audio); replaces the pwsh SoundPlayer/paplay branch."""
    import bob_voice
    from bob_core import load_config

    text = " ".join(rest) if rest else sys.stdin.read()
    if not text.strip():
        print("Nothing to speak.", file=sys.stderr)
        return 0
    return 0 if bob_voice.speak(text, load_config()) else 1


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
    "chat": _handle_chat,     # S2 — unified text conversation onto the loop
    "code": _handle_code,
    "think": _handle_think,
    "describe": _handle_describe,     # ONE-B2 — vision doors on the loop
    "screenshot": _handle_screenshot,
    "voice": _handle_voice,           # ONE-B5 — voice doors on the loop (pwsh loop deleted)
    "listen": _handle_listen,
    "transcribe": _handle_transcribe,
    "speak": _handle_speak,
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
