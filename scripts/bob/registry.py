"""NB4 (contract C6) — the command registry: the single source for dispatch (C1), help, and NE's
catalog. Each entry is {name, group, summary, args, runtime, handler}:

  name     fully-qualified command path ("agent", "agent serve", "setup")
  group    catalog grouping for help/splash
  summary  one-line description
  args     usage hint ("" if none)
  runtime  "python" (handled by this package) | "pwsh" (the orchestration scripts)
  handler  cli.py handler key for python commands (None for pwsh)

config/verbs.json is *generated from* this registry (verbs_json_dict / write_verbs) and read by the
shim so the shim and `python -m bob` route from the same data. Phased migration (C1): chat/code/think
(S2), describe/screenshot (ONE-B2) and voice/listen/transcribe/speak (ONE-B5) are on the loop;
recall/orchestration/provisioning are pwsh today and stay so until ONE-C/D port them to Python.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/bob/registry.py -> repo
VERBS_FILE = REPO / "config" / "verbs.json"

# NE1 — grouped mental-model catalog. `group` is one of the six below; `hidden` (optional, default
# False) keeps a command dispatchable but out of the human catalog (Decision B's sanctioned exception,
# e.g. an internal dev utility). Every verb in the bob.ps1 switch is registered here (a parity test
# enforces it), so the catalog/help can be generated with no verb missing.
COMMANDS = [
    # --- Talk: converse + senses --------------------------------------------------------------
    {"name": "chat", "group": "Talk", "summary": "Chat with Bob — one-shot or REPL, routed role (S2: on the agent loop)",
     "args": "[--pro|--think|--code] [--raw] [--max N] [--sys <text>] [prompt]", "runtime": "python", "handler": "chat"},
    {"name": "code", "group": "Talk", "summary": "Code-focused chat (coder / coder-pro)",
     "args": "[--pro] [--raw] [--max N] [prompt]", "runtime": "python", "handler": "code"},
    {"name": "think", "group": "Talk", "summary": "Deep-reasoning chat (planner / planner-pro)",
     "args": "[--pro] [--raw] [--max N] [prompt]", "runtime": "python", "handler": "think"},
    {"name": "voice", "group": "Talk", "summary": "Spoken conversation: mic -> loop -> speech (on the agent loop)",
     "args": "[--pro] [--agent]", "runtime": "python", "handler": "voice"},
    {"name": "listen", "group": "Talk", "summary": "Record mic until silence, print transcript",
     "args": "", "runtime": "python", "handler": "listen"},
    {"name": "transcribe", "group": "Talk", "summary": "Transcribe an audio file (whisper)",
     "args": "<file>", "runtime": "python", "handler": "transcribe"},
    {"name": "speak", "group": "Talk", "summary": "Synthesize text to speech (reads stdin if no arg)",
     "args": "[text]", "runtime": "python", "handler": "speak"},
    {"name": "describe", "group": "Talk", "summary": "Describe an image (local Qwen2-VL or --pro)",
     "args": "<image> [--pro] [prompt]", "runtime": "python", "handler": "describe"},
    {"name": "screenshot", "group": "Talk", "summary": "Capture the screen and describe it",
     "args": "[--pro] [prompt]", "runtime": "python", "handler": "screenshot"},

    # --- Act: agent + automation + capability inspection --------------------------------------
    {"name": "agent", "group": "Act", "summary": "Run the agent loop on a one-shot goal",
     "args": "<goal>", "runtime": "python", "handler": "agent_run"},
    {"name": "agent serve", "group": "Act", "summary": "Start the agent HTTP server (FastAPI, Bearer auth)",
     "args": "", "runtime": "python", "handler": "agent_serve"},
    {"name": "agent mcp", "group": "Act", "summary": "Expose Bob's tools over MCP (stdio)",
     "args": "", "runtime": "python", "handler": "agent_mcp"},
    {"name": "agent tools", "group": "Act", "summary": "List the agent's discovered tools",
     "args": "", "runtime": "python", "handler": "agent_tools"},
    {"name": "agent schedule", "group": "Act", "summary": "Manage scheduled agent goals",
     "args": "<add|list|run|remove|enable|disable|install|status>", "runtime": "python", "handler": "agent_schedule"},
    {"name": "agent log", "group": "Act", "summary": "Tail the agent log (live)",
     "args": "[-n N] [-f]", "runtime": "python", "handler": "agent_log"},
    {"name": "agent install", "group": "Act", "summary": "Register the BobAgent scheduled task",
     "args": "", "runtime": "python", "handler": "agent_install"},
    {"name": "agent uninstall", "group": "Act", "summary": "Remove the BobAgent scheduled task",
     "args": "", "runtime": "python", "handler": "agent_uninstall"},
    {"name": "agent status", "group": "Act", "summary": "Show BobAgent task status + recent log",
     "args": "", "runtime": "python", "handler": "agent_status"},
    {"name": "clip", "group": "Act", "summary": "Fetch a URL, summarize, and save to memory",
     "args": "<url> [--note <text>]", "runtime": "python", "handler": "clip"},
    {"name": "skill", "group": "Act", "summary": "List or run a skill (tool-sequence or sub-agent)",
     "args": "[name] [args…] [--show]", "runtime": "python", "handler": "skill"},
    {"name": "shell", "group": "Act", "summary": "Interactive REPL/TUI (the default when you run `bob` on a terminal)",
     "args": "", "runtime": "python", "handler": "shell"},
    {"name": "tools", "group": "Act", "summary": "List / test / inspect the agent's tools",
     "args": "<list|test|info> [name]", "runtime": "python", "handler": "tools"},
    {"name": "plugins", "group": "Act", "summary": "List installed plugins",
     "args": "list", "runtime": "python", "handler": "plugins"},

    # --- Make: generate via the local models --------------------------------------------------
    {"name": "fabric", "group": "Make", "summary": "Run Fabric patterns against the local endpoint",
     "args": "[pattern] [args]", "runtime": "python", "handler": "fabric"},

    # --- Know: memory -------------------------------------------------------------------------
    {"name": "recall", "group": "Know", "summary": "Search memory",
     "args": "<query>", "runtime": "python", "handler": "recall"},
    {"name": "remember", "group": "Know", "summary": "Store text to memory",
     "args": "<fact>", "runtime": "python", "handler": "remember"},
    {"name": "memory", "group": "Know", "summary": "Inspect/curate memory (list, show, edit, pin, forget, export, status, clear)",
     "args": "<list|show|edit|pin|unpin|forget|export|migrate|init-profile|status|clear>", "runtime": "python", "handler": "memory"},
    {"name": "budget", "group": "Know", "summary": "Token and cost usage summary",
     "args": "", "runtime": "python", "handler": "budget"},

    # --- Run: lifecycle + status --------------------------------------------------------------
    {"name": "up", "group": "Run", "summary": "Start endpoint + Open WebUI silently",
     "args": "[-NoOpen] [-WithServices]", "runtime": "python", "handler": "up"},
    {"name": "serve", "group": "Run", "summary": "Start the inference stack (llama-swap + LiteLLM), interactive",
     "args": "", "runtime": "python", "handler": "serve"},
    {"name": "restart", "group": "Run", "summary": "Stop then start the endpoint",
     "args": "", "runtime": "python", "handler": "restart"},
    {"name": "stop", "group": "Run", "summary": "Stop all services (frees VRAM)",
     "args": "", "runtime": "python", "handler": "stop"},
    {"name": "status", "group": "Run", "summary": "Loaded models and VRAM usage",
     "args": "", "runtime": "python", "handler": "status"},
    {"name": "ps", "group": "Run", "summary": "Daemon processes with PID, RAM, uptime",
     "args": "", "runtime": "python", "handler": "ps"},
    {"name": "logs", "group": "Run", "summary": "Tail the server log",
     "args": "[-n N]", "runtime": "python", "handler": "logs"},
    {"name": "services", "group": "Run", "summary": "Docker services: Langfuse / SearXNG / n8n",
     "args": "<start|stop|status|logs>", "runtime": "python", "handler": "services"},
    {"name": "webui", "group": "Run", "summary": "Launch Open WebUI only",
     "args": "", "runtime": "python", "handler": "webui"},
    {"name": "aider", "group": "Run", "summary": "Start aider in the current folder",
     "args": "[args]", "runtime": "python", "handler": "aider"},
    {"name": "litellm", "group": "Run", "summary": "Manage the LiteLLM proxy",
     "args": "[start|stop|status]", "runtime": "python", "handler": "litellm"},
    {"name": "whisper", "group": "Run", "summary": "Manage the whisper STT server (:8082)",
     "args": "[start|stop|status]", "runtime": "python", "handler": "whisper"},
    {"name": "piper", "group": "Run", "summary": "Manage the piper TTS server (:8083)",
     "args": "[start|stop|status]", "runtime": "python", "handler": "piper"},
    {"name": "doctor", "group": "Run", "summary": "Full pre-flight diagnostics",
     "args": "", "runtime": "python", "handler": "doctor"},
    {"name": "diagnose", "group": "Run", "summary": "System and model health check",
     "args": "", "runtime": "python", "handler": "diagnose"},
    {"name": "bench", "group": "Run", "summary": "Throughput benchmark",
     "args": "[role]", "runtime": "python", "handler": "bench"},

    # --- Config: setup + models + provisioning ------------------------------------------------
    {"name": "setup", "group": "Config", "summary": "Pre-flight health check / first-run setup",
     "args": "[check]", "runtime": "python", "handler": "setup"},
    {"name": "setup-voice", "group": "Config", "summary": "Download piper + whisper, build whisper-server",
     "args": "", "runtime": "pwsh", "handler": None},
    {"name": "fabric-setup", "group": "Config", "summary": "Install Fabric and point it at the local endpoint",
     "args": "", "runtime": "pwsh", "handler": None},
    {"name": "gen", "group": "Config", "summary": "Regenerate runtime configs from models.json",
     "args": "[profile]", "runtime": "python", "handler": "gen"},
    {"name": "fetch", "group": "Config", "summary": "Download models for a profile",
     "args": "[--list] [profile]", "runtime": "pwsh", "handler": None},
    {"name": "models", "group": "Config", "summary": "List models with backing names and state",
     "args": "", "runtime": "python", "handler": "models"},
    {"name": "show", "group": "Config", "summary": "Model info: file, VRAM, SHA256, disk status",
     "args": "<role>", "runtime": "python", "handler": "show"},
    {"name": "profile", "group": "Config", "summary": "Switch profile (auto = detect from VRAM)",
     "args": "<name|auto>", "runtime": "python", "handler": "profile"},
    {"name": "profiles", "group": "Config", "summary": "List VRAM profiles with sizes",
     "args": "", "runtime": "python", "handler": "profiles"},
    {"name": "build", "group": "Config", "summary": "Build llama.cpp (CUDA, or --cpu for no-GPU)",
     "args": "[--cpu] [--force]", "runtime": "pwsh", "handler": None},
    {"name": "update", "group": "Config", "summary": "Pull latest llama.cpp and rebuild",
     "args": "", "runtime": "pwsh", "handler": None},
    {"name": "lock", "group": "Config", "summary": "(Re)generate versions.lock from pinned sources (ND1)",
     "args": "[--check]", "runtime": "pwsh", "handler": None},
    {"name": "version", "group": "Config", "summary": "Show binary versions and submodule commits",
     "args": "", "runtime": "python", "handler": "version"},
    {"name": "mlock", "group": "Config", "summary": "Check/grant SeLockMemoryPrivilege (for --mlock)",
     "args": "", "runtime": "pwsh", "handler": None},
    {"name": "eval", "group": "Config", "summary": "Benchmark model quality (mmlu / humaneval / gsm8k)",
     "args": "<role> [task]", "runtime": "pwsh", "handler": None},
    {"name": "verify-urls", "group": "Config", "summary": "Check HuggingFace download URLs",
     "args": "[profile]", "runtime": "python", "handler": "verify-urls", "hidden": True},

    # Meta — the generated help/catalog itself (hidden: it needn't list itself). Registering it routes
    # `bob help` through `python -m bob help` on both front doors, retiring the pwsh here-string (WI-7).
    {"name": "help", "group": "Run", "summary": "Show the command catalog",
     "args": "", "runtime": "python", "handler": "help", "hidden": True},
]

_VALID_RUNTIMES = {"python", "pwsh"}


def commands(include_hidden: bool = True) -> list:
    """All command entries (a copy, so callers can't mutate the registry). `include_hidden=False`
    drops commands flagged hidden — the human catalog/help uses that; dispatch/verbs.json use all."""
    return [dict(c) for c in COMMANDS if include_hidden or not c.get("hidden")]


def by_name() -> dict:
    return {c["name"]: c for c in COMMANDS}


def verbs_json_dict() -> dict:
    """The data the shim reads: command-path -> runtime, plus the unknown-command default."""
    return {"commands": {c["name"]: c["runtime"] for c in COMMANDS}, "default": "python"}


def write_verbs(path: Path = None) -> Path:
    """(Re)generate config/verbs.json from the registry. Atomic write (temp + replace)."""
    import os

    path = path or VERBS_FILE
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(verbs_json_dict(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _check(path: Path = None) -> int:
    """Verify config/verbs.json matches the registry. Returns 0 if in sync, 1 if stale/missing —
    used as a pre-commit / CI gate so a registry edit can't land with a stale verbs.json."""
    path = path or VERBS_FILE
    if not path.exists():
        print(f"verbs.json missing at {path} — run: python -m bob.registry", file=sys.stderr)
        return 1
    disk = json.loads(path.read_text(encoding="utf-8"))
    if disk != verbs_json_dict():
        print("verbs.json is STALE (out of sync with the command registry) — "
              "run: python -m bob.registry", file=sys.stderr)
        return 1
    print("verbs.json in sync")
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv[1:]:
        sys.exit(_check())
    p = write_verbs()
    print(f"wrote {p}")
