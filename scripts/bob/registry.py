"""ONE-E (contract C6) — the command registry: the SINGLE source for dispatch (C1), help, and the NE
catalog. Each entry is {name, group, summary, args, handler} (+ optional hidden). Adding a command is
one entry here plus its cli.py handler — no generated table to regenerate, no runtime field (every verb
is Python since ONE-D/E retired PowerShell), no sync gate.

  name     fully-qualified command path ("agent", "agent serve", "setup")
  group    catalog grouping for help/splash (Talk/Act/Make/Know/Run/Config)
  summary  one-line description
  args     usage hint ("" if none)
  handler  cli.py handler key
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/bob/registry.py -> repo

# NE1 — grouped mental-model catalog. `group` is one of the six below; `hidden` (optional, default
# False) keeps a command dispatchable but out of the human catalog (Decision B's sanctioned exception,
# e.g. an internal dev utility). This registry is the sole catalog — help/dispatch both read it.
COMMANDS = [
    # --- Talk: converse + senses --------------------------------------------------------------
    {"name": "chat", "group": "Talk", "summary": "Chat with Bob — one-shot or REPL, routed role (S2: on the agent loop)",
     "args": "[--pro|--think|--code] [--raw] [--max N] [--sys <text>] [prompt]", "handler": "chat"},
    {"name": "code", "group": "Talk", "summary": "Code-focused chat (coder / coder-pro)",
     "args": "[--pro] [--raw] [--max N] [prompt]", "handler": "code"},
    {"name": "think", "group": "Talk", "summary": "Deep-reasoning chat (planner / planner-pro)",
     "args": "[--pro] [--raw] [--max N] [prompt]", "handler": "think"},
    {"name": "voice", "group": "Talk", "summary": "Spoken conversation: mic -> loop -> speech (on the agent loop)",
     "args": "[--pro] [--agent]", "handler": "voice"},
    {"name": "listen", "group": "Talk", "summary": "Record mic until silence, print transcript",
     "args": "", "handler": "listen"},
    {"name": "transcribe", "group": "Talk", "summary": "Transcribe an audio file (whisper)",
     "args": "<file>", "handler": "transcribe"},
    {"name": "speak", "group": "Talk", "summary": "Synthesize text to speech (reads stdin if no arg)",
     "args": "[text]", "handler": "speak"},
    {"name": "describe", "group": "Talk", "summary": "Describe an image (local Qwen2-VL or --pro)",
     "args": "<image> [--pro] [prompt]", "handler": "describe"},
    {"name": "screenshot", "group": "Talk", "summary": "Capture the screen and describe it",
     "args": "[--pro] [prompt]", "handler": "screenshot"},

    # --- Act: agent + automation + capability inspection --------------------------------------
    {"name": "agent", "group": "Act", "summary": "Run the agent loop on a one-shot goal",
     "args": "<goal>", "handler": "agent_run"},
    {"name": "agent serve", "group": "Act", "summary": "Start the agent HTTP server (FastAPI, Bearer auth)",
     "args": "", "handler": "agent_serve"},
    {"name": "agent mcp", "group": "Act", "summary": "Expose Bob's tools over MCP (stdio)",
     "args": "", "handler": "agent_mcp"},
    {"name": "agent tools", "group": "Act", "summary": "List the agent's discovered tools",
     "args": "", "handler": "agent_tools"},
    {"name": "agent schedule", "group": "Act", "summary": "Manage scheduled agent goals",
     "args": "<add|list|run|remove|enable|disable|install|status>", "handler": "agent_schedule"},
    {"name": "agent log", "group": "Act", "summary": "Tail the agent log (live)",
     "args": "[-n N] [-f]", "handler": "agent_log"},
    {"name": "agent install", "group": "Act", "summary": "Register the BobAgent scheduled task",
     "args": "", "handler": "agent_install"},
    {"name": "agent uninstall", "group": "Act", "summary": "Remove the BobAgent scheduled task",
     "args": "", "handler": "agent_uninstall"},
    {"name": "agent status", "group": "Act", "summary": "Show BobAgent task status + recent log",
     "args": "", "handler": "agent_status"},
    {"name": "clip", "group": "Act", "summary": "Fetch a URL, summarize, and save to memory",
     "args": "<url> [--note <text>]", "handler": "clip"},
    {"name": "skill", "group": "Act", "summary": "List or run a skill (tool-sequence or sub-agent)",
     "args": "[name] [args…] [--show]", "handler": "skill"},
    {"name": "shell", "group": "Act", "summary": "Interactive REPL/TUI (the default when you run `bob` on a terminal)",
     "args": "", "handler": "shell"},
    {"name": "tools", "group": "Act", "summary": "List / test / inspect the agent's tools",
     "args": "<list|test|info> [name]", "handler": "tools"},
    {"name": "plugins", "group": "Act", "summary": "List installed plugins",
     "args": "list", "handler": "plugins"},

    # --- Make: generate via the local models --------------------------------------------------
    {"name": "fabric", "group": "Make", "summary": "Run Fabric patterns against the local endpoint",
     "args": "[pattern] [args]", "handler": "fabric"},

    # --- Know: memory -------------------------------------------------------------------------
    {"name": "recall", "group": "Know", "summary": "Search memory",
     "args": "<query>", "handler": "recall"},
    {"name": "remember", "group": "Know", "summary": "Store text to memory",
     "args": "<fact>", "handler": "remember"},
    {"name": "memory", "group": "Know", "summary": "Inspect/curate memory (list, show, edit, pin, forget, export, status, clear)",
     "args": "<list|show|edit|pin|unpin|forget|export|migrate|init-profile|status|clear>", "handler": "memory"},
    {"name": "budget", "group": "Know", "summary": "Token and cost usage summary",
     "args": "", "handler": "budget"},

    # --- Run: lifecycle + status --------------------------------------------------------------
    {"name": "up", "group": "Run", "summary": "Start endpoint + Open WebUI silently",
     "args": "[-NoOpen] [-WithServices]", "handler": "up"},
    {"name": "serve", "group": "Run", "summary": "Start the inference stack (llama-swap + LiteLLM), interactive",
     "args": "", "handler": "serve"},
    {"name": "restart", "group": "Run", "summary": "Stop then start the endpoint",
     "args": "", "handler": "restart"},
    {"name": "stop", "group": "Run", "summary": "Stop all services (frees VRAM)",
     "args": "", "handler": "stop"},
    {"name": "status", "group": "Run", "summary": "Loaded models and VRAM usage",
     "args": "", "handler": "status"},
    {"name": "ps", "group": "Run", "summary": "Daemon processes with PID, RAM, uptime",
     "args": "", "handler": "ps"},
    {"name": "logs", "group": "Run", "summary": "Tail the server log",
     "args": "[-n N]", "handler": "logs"},
    {"name": "services", "group": "Run", "summary": "Docker services: Langfuse / SearXNG / n8n",
     "args": "<start|stop|status|logs>", "handler": "services"},
    {"name": "webui", "group": "Run", "summary": "Launch Open WebUI only",
     "args": "", "handler": "webui"},
    {"name": "aider", "group": "Run", "summary": "Start aider in the current folder",
     "args": "[args]", "handler": "aider"},
    {"name": "litellm", "group": "Run", "summary": "Manage the LiteLLM proxy",
     "args": "[start|stop|status]", "handler": "litellm"},
    {"name": "whisper", "group": "Run", "summary": "Manage the whisper STT server (:8082)",
     "args": "[start|stop|status]", "handler": "whisper"},
    {"name": "piper", "group": "Run", "summary": "Manage the piper TTS server (:8083)",
     "args": "[start|stop|status]", "handler": "piper"},
    {"name": "doctor", "group": "Run", "summary": "Full pre-flight diagnostics",
     "args": "", "handler": "doctor"},
    {"name": "diagnose", "group": "Run", "summary": "System and model health check",
     "args": "", "handler": "diagnose"},
    {"name": "bench", "group": "Run", "summary": "Throughput benchmark",
     "args": "[role]", "handler": "bench"},

    # --- Config: setup + models + provisioning ------------------------------------------------
    {"name": "setup", "group": "Config", "summary": "Pre-flight health check / first-run setup",
     "args": "[check]", "handler": "setup"},
    {"name": "setup-voice", "group": "Config", "summary": "Download piper + whisper, build whisper-server",
     "args": "[--force]", "handler": "setup-voice"},
    {"name": "fabric-setup", "group": "Config", "summary": "Install Fabric and point it at the local endpoint",
     "args": "[--force]", "handler": "fabric-setup"},
    {"name": "gen", "group": "Config", "summary": "Regenerate runtime configs from models.json",
     "args": "[profile]", "handler": "gen"},
    {"name": "fetch", "group": "Config", "summary": "Download models for a profile",
     "args": "[--list] [profile]", "handler": "fetch"},
    {"name": "models", "group": "Config", "summary": "List models with backing names and state",
     "args": "", "handler": "models"},
    {"name": "show", "group": "Config", "summary": "Model info: file, VRAM, SHA256, disk status",
     "args": "<role>", "handler": "show"},
    {"name": "profile", "group": "Config", "summary": "Switch profile (auto = detect from VRAM)",
     "args": "<name|auto>", "handler": "profile"},
    {"name": "profiles", "group": "Config", "summary": "List VRAM profiles with sizes",
     "args": "", "handler": "profiles"},
    {"name": "build", "group": "Config", "summary": "Build llama.cpp (CUDA, or --cpu for no-GPU)",
     "args": "[--cpu] [--force]", "handler": "build"},
    {"name": "update", "group": "Config", "summary": "Pull latest llama.cpp and rebuild",
     "args": "[--tag <ref>]", "handler": "update"},
    {"name": "lock", "group": "Config", "summary": "(Re)generate versions.lock from pinned sources (ND1)",
     "args": "[--check]", "handler": "lock"},
    {"name": "version", "group": "Config", "summary": "Show binary versions and submodule commits",
     "args": "", "handler": "version"},
    {"name": "mlock", "group": "Config", "summary": "Check/grant SeLockMemoryPrivilege (for --mlock)",
     "args": "[--grant]", "handler": "mlock"},
    {"name": "eval", "group": "Config", "summary": "Benchmark model quality (mmlu / humaneval / gsm8k)",
     "args": "<role> [task] [--shots N] [--limit N]", "handler": "eval"},
    {"name": "verify-urls", "group": "Config", "summary": "Check HuggingFace download URLs",
     "args": "[profile]", "handler": "verify-urls", "hidden": True},

    # Meta — the generated help/catalog itself (hidden: it needn't list itself). Registering it routes
    # `bob help` through `python -m bob help` on both front doors, retiring the pwsh here-string (WI-7).
    {"name": "help", "group": "Run", "summary": "Show the command catalog",
     "args": "", "handler": "help", "hidden": True},
]

def commands(include_hidden: bool = True) -> list:
    """All command entries (a copy, so callers can't mutate the registry). `include_hidden=False` drops
    commands flagged hidden — the human catalog/help uses that; dispatch uses all."""
    return [dict(c) for c in COMMANDS if include_hidden or not c.get("hidden")]


def by_name() -> dict:
    return {c["name"]: c for c in COMMANDS}
