"""The command registry: the SINGLE source for dispatch, help, and the
catalog. Each entry is {name, group, summary, args, handler} (+ optional hidden). Adding a command is
one entry here plus its cli.py handler — no generated table to regenerate, no runtime field (every verb
is Python), no sync gate.

  name     fully-qualified command path ("agent", "agent serve", "setup")
  group    catalog grouping for help/splash (one of GROUP_ORDER)
  summary  one-line description
  args     usage hint ("" if none)
  handler  cli.py handler key
"""
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent  # scripts/bob/registry.py -> repo

# The order + membership of the help/splash buckets, authored HERE once. The plain-string catalog
# (catalog.py), the rich views (render.py), and the CLI help fallback (cli.py) all read this — so the
# grouping is single-sourced and a re-bucket is one edit. Every command's `group` must be one of these
# (unknown groups still render, appended last, but the group test guards against typos). Daily surfaces
# (Run/Services) stay small; the larger provisioning/model catalog is split so no bucket is a wall.
GROUP_ORDER = ["Talk", "Act", "Make", "Know", "Run", "Services", "Models", "Diagnose", "Setup"]

# Grouped mental-model catalog, authored in GROUP_ORDER order. `group` is one of GROUP_ORDER; `hidden`
# (optional, default False) keeps a command dispatchable but out of the human catalog (a sanctioned
# exception, e.g. an internal dev utility). This registry is the sole catalog — help/dispatch both read it.
COMMANDS = [
    # --- Talk: converse + senses --------------------------------------------------------------
    {"name": "chat", "group": "Talk", "summary": "Chat with Bob — one-shot or REPL, routed role (on the agent loop)",
     "args": "[--pro|--think|--code] [--raw] [--max N] [--sys <text>] [prompt]", "handler": "chat"},
    {"name": "code", "group": "Talk", "summary": "Code-focused chat (coder / coder-pro)",
     "args": "[--pro] [--raw] [--max N] [prompt]", "handler": "code"},
    {"name": "think", "group": "Talk", "summary": "Chat with reasoning on (the model thinks first)",
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
    {"name": "task test", "group": "Act", "summary": "Run the configured lint/test commands and summarize failures",
     "args": "[--lint] [--test]", "handler": "task_test"},
    {"name": "code index", "group": "Act", "summary": "Build the semantic code index (embeddings) for code_search",
     "args": "", "handler": "code_index"},
    {"name": "task rewind", "group": "Act", "summary": "Restore the working tree to a checkpointed step of a run",
     "args": "<run-id> [<step>] [--list]", "handler": "task_rewind"},
    {"name": "task start", "group": "Act", "summary": "Run a durable agent task in a detached background worker",
     "args": "\"<goal>\" [--allow-computer]", "handler": "task_start"},
    {"name": "task status", "group": "Act", "summary": "List background tasks, or show one by run id",
     "args": "[<run-id>]", "handler": "task_status"},
    {"name": "task logs", "group": "Act", "summary": "Print the tail of a background task's log",
     "args": "<run-id> [-n N]", "handler": "task_logs"},
    {"name": "task cancel", "group": "Act", "summary": "Stop a running background task",
     "args": "<run-id>", "handler": "task_cancel"},
    {"name": "task resume", "group": "Act", "summary": "Continue a checkpointed background task",
     "args": "<run-id>", "handler": "task_resume"},
    {"name": "computer", "group": "Act", "summary": "Computer-use kill switch: halt or resume desktop automation",
     "args": "stop | status [clear]", "handler": "computer"},
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
    {"name": "aider", "group": "Act", "summary": "Start aider in the current folder",
     "args": "[args]", "handler": "aider"},

    # --- Make: generate via the local models --------------------------------------------------
    {"name": "fabric", "group": "Make", "summary": "Run Fabric patterns against the local endpoint",
     "args": "[pattern] [args]", "handler": "fabric"},
    {"name": "edit", "group": "Make", "summary": "Apply a precise search/replace or unified-diff edit (preview by default)",
     "args": "<path> (--search S --replace R | --diff FILE|-) [--apply]", "handler": "edit"},

    # --- Know: memory -------------------------------------------------------------------------
    {"name": "recall", "group": "Know", "summary": "Search memory",
     "args": "<query>", "handler": "recall"},
    {"name": "remember", "group": "Know", "summary": "Store text to memory",
     "args": "<fact>", "handler": "remember"},
    {"name": "memory", "group": "Know", "summary": "Inspect/curate memory (list, show, edit, pin, forget, export, status, clear)",
     "args": "<list|show|edit|pin|unpin|forget|export|migrate|init-profile|status|clear>", "handler": "memory"},
    {"name": "budget", "group": "Know", "summary": "Token and cost usage summary",
     "args": "", "handler": "budget"},

    # --- Run: run the stack day-to-day (also available live in the shell as /up, /stop, …) -----
    {"name": "up", "group": "Run", "summary": "Start endpoint + Open WebUI silently",
     "args": "[--no-open] [--with-services]", "handler": "up"},
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
    {"name": "services", "group": "Run", "summary": "Opt-in add-ons: n8n (native) / SearXNG / Langfuse (Docker)",
     "args": "[<name>] <start|stop|status|logs>", "handler": "services"},
    {"name": "webui", "group": "Run", "summary": "Launch Open WebUI only",
     "args": "", "handler": "webui"},

    # --- Services: start/stop/status one inference or voice daemon ----------------------------
    {"name": "litellm", "group": "Services", "summary": "Manage the LiteLLM proxy",
     "args": "[start|stop|status]", "handler": "litellm"},
    {"name": "whisper", "group": "Services", "summary": "Manage the whisper STT server (:8082)",
     "args": "[start|stop|status]", "handler": "whisper"},
    {"name": "piper", "group": "Services", "summary": "Manage the piper TTS server (:8083)",
     "args": "[start|stop|status]", "handler": "piper"},

    # --- Models: the model registry, profiles, and downloads ----------------------------------
    {"name": "models", "group": "Models", "summary": "List models with backing names and state",
     "args": "", "handler": "models"},
    {"name": "show", "group": "Models", "summary": "Model info: file, VRAM, SHA256, disk status",
     "args": "<role>", "handler": "show"},
    {"name": "profiles", "group": "Models", "summary": "List VRAM profiles with sizes",
     "args": "", "handler": "profiles"},
    {"name": "profile", "group": "Models", "summary": "Switch profile (auto = detect from VRAM)",
     "args": "<name|auto>", "handler": "profile"},
    {"name": "fetch", "group": "Models", "summary": "Download models for a profile",
     "args": "[--list] [profile]", "handler": "fetch"},
    {"name": "eval", "group": "Models", "summary": "Benchmark model quality (mmlu / humaneval / gsm8k)",
     "args": "<role> [task] [--shots N] [--limit N]", "handler": "eval"},
    {"name": "verify-urls", "group": "Models", "summary": "Check HuggingFace download URLs",
     "args": "[profile]", "handler": "verify-urls", "hidden": True},

    # --- Diagnose: health, benchmarks, versions -----------------------------------------------
    {"name": "doctor", "group": "Diagnose", "summary": "Full pre-flight diagnostics (--quick for a fast health check)",
     "args": "[--quick]", "handler": "doctor"},
    {"name": "diagnose", "group": "Diagnose", "summary": "System and model health check",
     "args": "", "handler": "diagnose"},
    {"name": "bench", "group": "Diagnose", "summary": "Throughput benchmark",
     "args": "[role]", "handler": "bench"},
    {"name": "version", "group": "Diagnose", "summary": "Show binary versions and submodule commits",
     "args": "", "handler": "version"},
    {"name": "traces", "group": "Diagnose", "summary": "View agent traces from the local file sink (agent.tracing)",
     "args": "[list | show <trace-id>]", "handler": "traces"},

    # --- Setup: first-run, install, build, provisioning ---------------------------------------
    {"name": "setup", "group": "Setup", "summary": "First-run setup + quick health check (= doctor --quick)",
     "args": "[check]", "handler": "setup"},
    {"name": "reset", "group": "Setup", "summary": "Wipe ALL local data and return to first-run",
     "args": "[--yes]", "handler": "reset"},
    {"name": "setup-voice", "group": "Setup", "summary": "Download piper + whisper, build whisper-server",
     "args": "[--force]", "handler": "setup-voice"},
    {"name": "fabric-setup", "group": "Setup", "summary": "Install Fabric and point it at the local endpoint",
     "args": "[--force]", "handler": "fabric-setup"},
    {"name": "gen", "group": "Setup", "summary": "Regenerate runtime configs from models.json",
     "args": "[profile]", "handler": "gen"},
    {"name": "build", "group": "Setup", "summary": "Build llama.cpp (CUDA, or --cpu for no-GPU)",
     "args": "[--cpu] [--force]", "handler": "build"},
    {"name": "update", "group": "Setup", "summary": "Pull latest llama.cpp and rebuild",
     "args": "[--tag <ref>]", "handler": "update"},
    {"name": "lock", "group": "Setup", "summary": "(Re)generate versions.lock from pinned sources",
     "args": "[--check]", "handler": "lock"},
    {"name": "mlock", "group": "Setup", "summary": "Check/grant SeLockMemoryPrivilege (for --mlock)",
     "args": "[--grant]", "handler": "mlock"},

    # Meta — the generated help/catalog itself (hidden: it needn't list itself). Registering it routes
    # `bob help` through `python -m bob help` on both front doors.
    {"name": "help", "group": "Run", "summary": "Show the command catalog",
     "args": "", "handler": "help", "hidden": True},
]

def commands(include_hidden: bool = True) -> list:
    """All command entries (a copy, so callers can't mutate the registry). `include_hidden=False` drops
    commands flagged hidden — the human catalog/help uses that; dispatch uses all."""
    return [dict(c) for c in COMMANDS if include_hidden or not c.get("hidden")]


def by_name() -> dict:
    return {c["name"]: c for c in COMMANDS}
