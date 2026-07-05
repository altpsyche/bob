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


# --- ONE-C Slice 5: agent scheduling (scripts/tools/schedule.py) ---------------------------------
# schedule CRUD + the OS-task lifecycle + the runner core. install/uninstall are CLI-only (they touch
# the OS scheduler); the CRUD + status/log cores are also agent tools.

def _sched_mod():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import schedule as sched_mod
    return sched_mod


def _flag(rest: list, name: str, default=None):
    """--flag <value> extractor mirroring the pwsh schedule-arg parsing."""
    if name in rest:
        i = rest.index(name)
        if i + 1 < len(rest):
            return rest[i + 1]
    return default


def _handle_agent_schedule(rest: list) -> int:
    sched, cfg = _sched_mod(), _cfg()
    sub = rest[0] if rest else "list"
    args = rest[1:]
    if sub == "list":
        print(sched.schedule_list(cfg))
    elif sub == "add":
        name = next((a for a in args if not a.startswith("--")), None)
        print(sched.schedule_add(
            cfg, name, cron=_flag(args, "--cron", "0 9 * * 1-5"), goal=_flag(args, "--goal"),
            role=_flag(args, "--role", "agent"), notify=("--notify" in args),
            title=_flag(args, "--title")))
    elif sub == "remove":
        print(sched.schedule_remove(cfg, args[0] if args else None))
    elif sub == "run":
        print(sched.schedule_run(cfg, args[0] if args else None))
    elif sub == "enable":
        print(sched.schedule_enable(cfg, args[0] if args else None))
    elif sub == "disable":
        print(sched.schedule_disable(cfg, args[0] if args else None))
    elif sub == "install":
        print(sched.agent_install(cfg))
    elif sub == "status":
        print(sched.agent_status(cfg))
    else:
        print("bob agent schedule <add|list|run|remove|enable|disable|install|status>")
    return 0


def _handle_agent_install(rest: list) -> int:
    print(_sched_mod().agent_install(_cfg()))
    return 0


def _handle_agent_uninstall(rest: list) -> int:
    print(_sched_mod().agent_uninstall(_cfg()))
    return 0


def _handle_agent_status(rest: list) -> int:
    print(_sched_mod().agent_status(_cfg()))
    return 0


def _handle_agent_log(rest: list) -> int:
    """bob agent log [-n N] — bounded tail, or follow with -f/--wait (CLI-only concern)."""
    rest = list(rest)
    follow = any(f in rest for f in ("-f", "--wait", "-Wait"))
    sched, cfg = _sched_mod(), _cfg()
    if not follow:
        print(sched.agent_log(cfg, n=int(_flag(rest, "-n", 50))))
        return 0
    import time
    log = sched._log_file(cfg)
    print(sched.agent_log(cfg, n=50))
    if not log.exists():
        return 0
    with open(log, encoding="utf-8", errors="replace") as fh:
        fh.seek(0, 2)
        try:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(1)
        except KeyboardInterrupt:
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


# --- ONE-C Slice 1: memory + meta -----------------------------------------------------------------
# Each verb below routes to the same capability the agent uses (no duplicated logic). budget is an
# agent tool (scripts/tools/budget.py) reached here + via `bob --run budget_summary`; the rest are
# CLI-only re-exposures of existing Python (bob_core memory, bob_memory.py, tool_loader.py).

_MEMORY_SUBCOMMANDS = {"status", "clear", "list", "show", "forget", "edit", "pin", "unpin",
                       "export", "migrate", "init-profile"}


def _handle_remember(rest: list) -> int:
    """bob remember <text> — store a memory (wires the verb to bob_core.memory_store, the same write
    path the agent's memory_store tool uses). Replaces the pwsh shell-out to bob-memory.ps1."""
    if not rest:
        print("usage: bob remember <text>", file=sys.stderr)
        return 1
    from bob_core import memory_store

    print(memory_store(" ".join(rest)))
    return 0


def _handle_recall(rest: list) -> int:
    """bob recall <query> — search memory (bob_core.memory_recall, same read path as the tool)."""
    if not rest:
        print("usage: bob recall <query>", file=sys.stderr)
        return 1
    from bob_core import memory_recall

    print(memory_recall(" ".join(rest)))
    return 0


def _handle_memory(rest: list) -> int:
    """bob memory <sub> — inspect/curate memory. 1:1 onto bob_memory.py's argparse subcommands (the
    --db path resolves from config, like the pwsh bob-memory.ps1 wrapper). No args -> status."""
    if rest and rest[0] not in _MEMORY_SUBCOMMANDS:
        print("Usage: bob memory <" + "|".join(sorted(_MEMORY_SUBCOMMANDS)) + "> [args]",
              file=sys.stderr)
        return 1
    import bob_memory
    from bob_core import _get_db_path, load_config

    sub = rest or ["status"]
    db_path = _get_db_path(load_config())
    saved = sys.argv
    try:
        sys.argv = ["bob_memory", "--db", db_path] + sub
        bob_memory.main()
    finally:
        sys.argv = saved
    return 0


def _handle_budget(rest: list) -> int:
    """bob budget — token/cost usage summary. Calls the same budget_summary core the agent tool and
    `bob --run budget_summary` call (scripts/tools/budget.py)."""
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import budget
    from bob_core import load_config

    print(budget.budget_summary(load_config()))
    return 0


def _handle_tools(rest: list) -> int:
    """bob tools <list|test|info> [name] — the tool catalog via the engine's tool_loader.py (already
    Python). list honors agent.disabledTools; test/info need a tool name."""
    from bob_core import load_config

    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    sub = rest[0] if rest else "list"
    tool_args = rest[1:]
    if sub == "list":
        disabled_raw = load_config().get("agent", {}).get("disabledTools", [])
        disabled = ",".join(disabled_raw) if isinstance(disabled_raw, list) else str(disabled_raw)
        argv = ["--list", "--disabled", disabled]
    elif sub in ("test", "info"):
        if not tool_args:
            print(f"Usage: bob tools {sub} <name>", file=sys.stderr)
            return 1
        argv = [f"--{sub}", tool_args[0]]
    else:
        print("bob tools list|test <name>|info <name>", file=sys.stderr)
        return 1
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.argv = ["tool_loader.py"] + argv
    runpy.run_path(str(SCRIPTS / "tools" / "tool_loader.py"), run_name="__main__")
    return 0


def _handle_plugins(rest: list) -> int:
    """bob plugins [list] — enumerate plugins/<name>/ (invoke.ps1|invoke.py + description.txt). Filesystem
    scan; ports the pwsh listing verbatim."""
    if rest and rest[0] != "list":
        print("Usage: bob plugins list", file=sys.stderr)
        return 1
    plugins_root = REPO / "plugins"
    dirs = sorted([d for d in plugins_root.iterdir() if d.is_dir()]) if plugins_root.exists() else []
    if not dirs:
        print("No plugins found in plugins/.")
        return 0
    print("\nInstalled plugins:")
    for p in dirs:
        kind = ("ps1" if (p / "invoke.ps1").exists()
                else "py" if (p / "invoke.py").exists() else "?")
        desc_file = p / "description.txt"
        desc = desc_file.read_text(encoding="utf-8").strip() if desc_file.exists() else ""
        print(f"  bob {p.name:<15} [{kind}]  {desc}")
    print("")
    return 0


def _handle_fabric(rest: list) -> int:
    """bob fabric [pattern] [args] — passthrough to the staged bin/fabric binary (osenv.bin_exe)."""
    import osenv

    exe = osenv.bin_exe("fabric")
    if not exe.exists():
        print("fabric not found. Run: bob fabric-setup", file=sys.stderr)
        return 1
    return subprocess.run([str(exe)] + rest).returncode


def _handle_aider(rest: list) -> int:
    """bob aider [args] — start aider in the current folder (venv-aider console script)."""
    import osenv

    exe = osenv.venv_exe("venv-aider", "aider")
    if not exe.exists():
        print(f"aider not installed: {exe}  (run scripts/bootstrap.ps1 -WithAider)", file=sys.stderr)
        return 1
    return subprocess.run([str(exe)] + rest).returncode


# --- ONE-C Slice 2: lifecycle ---------------------------------------------------------------------
# Each verb routes to the scripts/tools/stack.py capability the agent also calls (no duplicated logic).
# up/stop/restart/ps and the service controls are background/non-blocking; serve/webui are foreground.

def _stack():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import stack
    return stack


def _cfg():
    from bob_core import load_config
    return load_config()


def _handle_up(rest: list) -> int:
    """bob up [-NoOpen] [-WithServices] — background bring-up (endpoint + proxy + WebUI)."""
    rest = list(rest)
    open_browser = not any(f in rest for f in ("-NoOpen", "--no-open"))
    with_services = any(f in rest for f in ("-WithServices", "--with-services"))
    print(_stack().stack_up(_cfg(), open_browser=open_browser, with_services=with_services))
    return 0


def _handle_serve(rest: list) -> int:
    """bob serve — foreground inference stack (llama-swap + LiteLLM), Ctrl+C to stop."""
    return _stack().serve_foreground(_cfg())


def _handle_restart(rest: list) -> int:
    print(_stack().stack_restart(_cfg()))
    return 0


def _handle_stop(rest: list) -> int:
    print(_stack().stack_stop(_cfg()))
    return 0


def _handle_ps(rest: list) -> int:
    print(_stack().stack_ps(_cfg()))
    return 0


def _handle_status(rest: list) -> int:
    print(_stack().stack_status(_cfg()))
    return 0


def _handle_logs(rest: list) -> int:
    """bob logs [-n N | N] — tail-follow the endpoint log."""
    rest = list(rest)
    n = 50
    if "-n" in rest:
        i = rest.index("-n")
        if i + 1 < len(rest) and rest[i + 1].isdigit():
            n = int(rest[i + 1])
    elif rest and rest[0].isdigit():
        n = int(rest[0])
    return _stack().logs_follow(_cfg(), lines=n)


def _handle_webui(rest: list) -> int:
    return _stack().webui_foreground(_cfg())


def _service_action(rest: list) -> str:
    return rest[0] if rest and rest[0] in ("start", "stop", "status") else "start"


def _handle_litellm(rest: list) -> int:
    print(_stack().litellm_control(_cfg(), action=_service_action(rest)))
    return 0


def _handle_whisper(rest: list) -> int:
    print(_stack().whisper_control(_cfg(), action=_service_action(rest)))
    return 0


def _handle_piper(rest: list) -> int:
    print(_stack().piper_control(_cfg(), action=_service_action(rest)))
    return 0


def _handle_services(rest: list) -> int:
    action = rest[0] if rest else "status"
    print(_stack().services_control(_cfg(), action=action))
    return 0


# --- ONE-C Slice 4: model registry (read-only + profile) ------------------------------------------
# Each verb routes to the scripts/tools/models.py capability the agent also calls (built on the neutral
# config/models.json via bob_models.py). profile is mutating (writes data/active-profile.json); the rest
# are read-only. eval stays pwsh (very long, separate venv — ONE-D).

def _models_mod():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import models as models_mod
    return models_mod


def _build_mod():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import build as build_mod
    build_mod.configure(_cfg())
    return build_mod


def _handle_build(rest: list) -> int:
    """bob build [--cpu] [--force] — (re)build llama.cpp. Auto-selects the CPU tier when no GPU (DD1)."""
    import osenv
    rest = list(rest)
    force = "--force" in rest
    cpu = "--cpu" in rest or osenv.gpu_info() is None
    if cpu and "--cpu" not in rest:
        print("No GPU detected — building the CPU-only tier. Use 'bob build --cpu' to force, or install "
              "CUDA for a GPU build.", file=sys.stderr)
    try:
        print(_build_mod().build_llama(cpu=cpu, force=force))
    except RuntimeError as e:
        print(f"build failed: {e}", file=sys.stderr)
        return 1
    return 0


def _handle_fabric_setup(rest: list) -> int:
    """bob fabric-setup [--force] — build fabric (Go) + wire ~/.config/fabric."""
    force = any(f in rest for f in ("--force", "-Force"))
    try:
        print(_build_mod().setup_fabric(force=force))
    except RuntimeError as e:
        print(f"fabric-setup failed: {e}", file=sys.stderr)
        return 1
    return 0


def _handle_update(rest: list) -> int:
    """bob update [--tag <ref>] — release-aware update with rebuild + rollback (ND3)."""
    rest = list(rest)
    tag = None
    if "--tag" in rest:
        i = rest.index("--tag")
        if i + 1 < len(rest):
            tag = rest[i + 1]
    try:
        return _build_mod().update_stack(tag=tag)
    except RuntimeError as e:
        print(f"update failed: {e}", file=sys.stderr)
        return 1


def _provision_mod():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import provision
    return provision


def _handle_fetch(rest: list) -> int:
    """bob fetch [--list] [profile] — download the profile's models (resume + SHA256-verify + manifest)."""
    rest = list(rest)
    list_only = "--list" in rest
    positional = [a for a in rest if a != "--list"]
    profile = positional[0] if positional else None
    mod = _provision_mod()
    mod.configure(_cfg())
    print(mod.fetch_models(profile, list_only=list_only))
    return 0


def _handle_setup_voice(rest: list) -> int:
    """bob setup-voice [--force] — provision whisper + piper (build, download, deps, smoke)."""
    force = any(f in rest for f in ("--force", "-Force"))
    mod = _provision_mod()
    mod.configure(_cfg())
    try:
        print(mod.setup_voice(force=force))
    except RuntimeError as e:
        print(f"setup-voice failed: {e}", file=sys.stderr)
        return 1
    return 0


def _handle_lock(rest: list) -> int:
    """bob lock [--check] — regenerate versions.lock from its sources, or (--check) fail if it drifted."""
    from bob import versions
    if "--check" in rest:
        rc = versions.check_sync()
        if rc == 0:
            print("versions.lock in sync")
        return rc
    path = versions.write_lock()
    print(f"wrote {path}")
    return 0


def _handle_mlock(rest: list) -> int:
    """bob mlock [--grant] — report the mlock privilege status; --grant attempts to grant it (Windows:
    secedit + UAC self-elevation; Linux: prints the ulimit/limits.conf guidance)."""
    import osenv
    st = osenv.mlock_status()
    print("mlock: " + ("granted" if st["granted"] else "NOT granted") + f" — {st['detail']}")
    if "--grant" in rest:
        print(osenv.mlock_grant())
        return 0
    if not st["granted"]:
        print("To grant: bob mlock --grant")
    return 0 if st["granted"] else 1


def _handle_models(rest: list) -> int:
    print(_models_mod().models_list(_cfg()))
    return 0


def _handle_show(rest: list) -> int:
    if not rest:
        print("usage: bob show <role>   (roles: planner coder chat fim embed vision agent)",
              file=sys.stderr)
        return 1
    print(_models_mod().model_show(rest[0], _cfg()))
    return 0


def _handle_profiles(rest: list) -> int:
    print(_models_mod().profiles_list(_cfg()))
    return 0


def _handle_profile(rest: list) -> int:
    print(_models_mod().profile_switch(rest[0] if rest else "auto", _cfg()))
    return 0


def _handle_verify_urls(rest: list) -> int:
    print(_models_mod().verify_urls(rest[0] if rest else "", _cfg()))
    return 0


def _handle_bench(rest: list) -> int:
    print(_models_mod().bench(rest[0] if rest else "coder", _cfg()))
    return 0


def _handle_eval(rest: list) -> int:
    """bob eval <role> [task] [--shots N] [--limit N] — lm-eval quality benchmark (DD3: venv-eval)."""
    rest = list(rest)
    shots = limit = 0
    positional = []
    i = 0
    while i < len(rest):
        if rest[i] == "--shots" and i + 1 < len(rest):
            shots = int(rest[i + 1]); i += 2
        elif rest[i] == "--limit" and i + 1 < len(rest):
            limit = int(rest[i + 1]); i += 2
        else:
            positional.append(rest[i]); i += 1
    role = positional[0] if positional else "coder"
    task = positional[1] if len(positional) > 1 else "mmlu"
    return _models_mod().eval_model(role, task, shots=shots, limit=limit, config=_cfg())


# --- ONE-C Slice 3: health / diagnostics (scripts/tools/health.py) -------------------------------
# setup(check) + doctor share health_check(); version + diagnose are separate cores. diagnose is the
# SPLIT port (registry + light discovery); the deep OS discovery stays in scripts/diagnose.ps1 (ONE-D).

def _health_mod():
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import health as health_mod
    return health_mod


def _handle_setup(rest: list) -> int:
    sub = rest[0] if rest else "check"
    if sub != "check":
        print("Usage: bob setup check  (or: bob doctor for the full pre-flight)")
        return 1
    print(_health_mod().health_check(_cfg(), doctor=False))
    return 0


def _handle_doctor(rest: list) -> int:
    print(_health_mod().health_check(_cfg(), doctor=True))
    return 0


def _handle_version(rest: list) -> int:
    print(_health_mod().version_info(_cfg()))
    return 0


def _handle_diagnose(rest: list) -> int:
    print(_health_mod().diagnose(_cfg()))
    return 0


def _handle_gen(rest: list) -> int:
    """bob gen [profile] — regenerate all runtime configs from config/models.json (ONE-C Slice 6)."""
    tools_dir = str(SCRIPTS / "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    import generate
    generate.configure(_cfg())
    print(generate.gen_all(rest[0] if rest else None))
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
    "agent_schedule": _handle_agent_schedule,   # ONE-C Slice 5 — scheduling (scripts/tools/schedule.py)
    "agent_log": _handle_agent_log,
    "agent_install": _handle_agent_install,
    "agent_uninstall": _handle_agent_uninstall,
    "agent_status": _handle_agent_status,
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
    "remember": _handle_remember,     # ONE-C Slice 1 — memory + meta on Python
    "recall": _handle_recall,
    "memory": _handle_memory,
    "budget": _handle_budget,
    "tools": _handle_tools,
    "plugins": _handle_plugins,
    "fabric": _handle_fabric,
    "aider": _handle_aider,
    "up": _handle_up,                 # ONE-C Slice 2 — lifecycle on Python (scripts/tools/stack.py)
    "serve": _handle_serve,
    "restart": _handle_restart,
    "stop": _handle_stop,
    "status": _handle_status,         # ONE-C follow-up — loaded-models status (scripts/tools/stack.py)
    "ps": _handle_ps,
    "logs": _handle_logs,
    "webui": _handle_webui,
    "litellm": _handle_litellm,
    "whisper": _handle_whisper,
    "piper": _handle_piper,
    "services": _handle_services,
    "models": _handle_models,         # ONE-C Slice 4 — model registry readers (scripts/tools/models.py)
    "show": _handle_show,
    "profiles": _handle_profiles,
    "profile": _handle_profile,
    "verify-urls": _handle_verify_urls,
    "bench": _handle_bench,
    "eval": _handle_eval,             # ONE-D Slice D4 — lm-eval quality benchmark (scripts/tools/models.py)
    "build": _handle_build,           # ONE-D Slice D5 — native llama.cpp build (scripts/tools/build.py)
    "fabric-setup": _handle_fabric_setup,
    "update": _handle_update,         # ONE-D Slice D6 — release-aware update + rollback (build.py)
    "setup-voice": _handle_setup_voice,  # ONE-D Slice D7 — voice provisioning (provision.py)
    "fetch": _handle_fetch,           # ONE-D Slice D1 — model downloads (scripts/tools/provision.py)
    "lock": _handle_lock,             # ONE-D Slice D2 — versions.lock writer + gate (scripts/bob/versions.py)
    "mlock": _handle_mlock,           # ONE-D Slice D3 — mlock privilege status/grant (osenv)
    "gen": _handle_gen,               # ONE-C Slice 6 — config generators (scripts/tools/generate.py)
    "setup": _handle_setup,           # ONE-C Slice 3 — health / diagnostics (scripts/tools/health.py)
    "doctor": _handle_doctor,
    "version": _handle_version,
    "diagnose": _handle_diagnose,
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
