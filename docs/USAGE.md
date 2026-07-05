# USAGE

This document covers day-to-day use: starting and stopping the server, what each client does and how to configure it, and how to manage model profiles. For installation, see [SETUP.md](SETUP.md). For performance tuning and updating the engine, see [TUNING.md](TUNING.md).

> **New here?** [DAY-IN-THE-LIFE.md](DAY-IN-THE-LIFE.md) walks through every feature in one hands-on session. It's a better starting point than reading this document top to bottom.

## One-time client setup

```powershell
.\scripts\setup-clients.ps1
```

Run this once per machine. It links the VS Code Continue config and the aider config from the repo into your home directory, so both tools work without any in-app configuration. Open WebUI is opt-in (install it with `setup … -WithWebui`); when present, it's wired automatically as the stack starts. If you don't have symlink privileges, the script copies the files instead; re-run it after editing the repo configs to sync the copies.

## The `bob` command

`setup.bat` (Windows) or `./setup.sh` (Linux) puts `bob` on your PATH. Bob runs cross-platform under
PowerShell 7 — the same commands work on both OSes (see [PORTABILITY.md](PORTABILITY.md)). Open a
terminal and these commands are available:

```
Chat (Bob identity):
  bob chat                             Interactive REPL — multi-turn conversation (empty line to exit)
  bob chat [--pro] [--think] [--code]  REPL with routed role (local or cloud)
  bob chat "question"                  One-shot with default role
  bob chat <role> "prompt"             One-shot legacy syntax (still works)
  bob think [--pro] ["prompt"]         Alias: planner / planner-pro
  bob code  [--pro] ["prompt"]         Alias: coder / coder-pro
  bob remember "fact"                  Store a fact (SQLite + BGE-M3 embeddings)
  bob recall "query"                   Blended-rank search (semantic+recency+importance); prints JSON
  bob memory list|show|edit|pin|forget|export|migrate|init-profile   Inspect / curate memory
  bob memory status                    Memory DB size, entry count, per-type breakdown
  bob memory clear [--yes]             Wipe all memories
  bob budget                           Token and cost usage summary (LiteLLM + configured caps)
  # Full memory + sessions reference: docs/MEMORY.md

Voice (Phase 2 — run `bob setup-voice` once to download whisper + piper; flip voice.enabled in bob.psd1):
  bob setup-voice                      Download and wire whisper STT, piper TTS, and vision model
  bob listen                           Record mic until silence → print transcript (whisper STT)
  bob transcribe <file>                Transcribe an audio file via whisper
  bob speak ["text"]                   Synthesise text to audio and play it (piper TTS); accepts stdin
  bob voice [--pro] [--agent]          Continuous loop: listen → chat → speak (--agent: route through full tool loop)
  bob whisper start|stop|status|logs  Manage the whisper STT server (port 8082)
  bob piper [stop|status]              Start/control piper TTS HTTP server (:8083, OpenAI-compatible)

Vision (Phase 2 — requires vision model download; model loads on demand, TTL 30 s):
  bob describe <image> [--pro] ["prompt"]  Describe an image file (local Qwen2-VL or --pro cloud vision)
  bob screenshot [--pro] ["prompt"]        Take a screenshot and describe it (--pro for cloud vision)

Agent (Phase 3 — Hermes 3 8B, local tool use, optional scheduled tasks):
  bob agent "goal"                     One-shot: reason + call tools until done, print answer
  bob agent --role chat "goal"         Use a different role (e.g. chat = Qwen3) instead of agent
  bob agent --agency confirm "goal"    Prompt before each tool execution
  bob agent serve                      Start HTTP server (:8084, Bearer auth) — REST + SSE stream + sessions (n8n/WebUI)
  bob agent "goal" --stream            Stream the final answer token-by-token to the terminal
  bob clip <url>                       Fetch URL → summarise → store to memory (one LLM call)
  bob tools list                       List all discovered tool modules (auto-discovered; disable via agent.disabledTools)
  bob tools test <name>                Run a tool's self-test (e.g. bob tools test git)
  bob tools info <name>                Show full JSON schema for a tool
  bob setup check                      Verify agent deps (venv, model, tools, task, services)
  bob doctor                           Full pre-flight: setup checks + endpoint, GPU/VRAM, writable dirs, config parse
  bob agent schedule add <name> --cron "* * * * *" --goal "summarise my git log"
  bob agent schedule list              List scheduled tasks with next-run times
  bob agent schedule run <name>        Run a scheduled task immediately
  bob agent schedule enable/disable <name>
  bob agent schedule remove <name>
  bob agent log                        Tail the agent log (logs/bob-agent.log)
  bob agent install                    Register the recurring BobAgent task (Windows Scheduled Task / Linux cron)
  bob agent uninstall                  Remove the BobAgent task
  bob agent status                     Show BobAgent task state (running/ready/disabled)

Plugins (drop-in scripts in plugins/<name>/invoke.ps1 or .py):
  bob plugins list                     List installed plugins with type and description
  bob summarise [file]                 Summarise a file or piped text via LLM
  bob summarise [file] --length short|medium|long
  bob draft "<prompt>"                 Draft text from a one-liner (email, PR, Slack, doc)
  bob draft "<prompt>" --type email|pr|slack|doc
  bob search "<query>"                 Search files in current dir, synthesise results via LLM
  bob search "<query>" --path <dir>    Search in a specific directory
  bob search "<query>" --raw           Show raw grep output, skip LLM
  bob play <search query>              Open Spotify (or YouTube Music) with a search query

Inference:
  bob serve                            Start API endpoint (interactive, Ctrl+C to stop)
  bob up [-NoOpen]                     Start endpoint + Open WebUI silently (no popup windows) [+ browser]
  bob stop                             Stop all services and free VRAM
  bob restart                          Stop then start endpoint (interactive, shows logs)
  bob status                           Show which models are loaded and VRAM usage
  bob ps                               Show daemon processes with PID, RAM, and uptime
  bob logs [-n N]                      Tail the server log (default: last 50 lines)

Models:
  bob models                           List models with backing names and load state
  bob show <role>                      Model details: file, VRAM, SHA256, disk status
  bob bench [gguf]                     Throughput benchmark

Management:
  bob profiles                         List VRAM profiles with sizes and active marker
  bob profile <name|auto>              Switch profile (auto = detect from GPU VRAM)
  bob fetch [--list] [profile]         Download models for active/specified profile
  bob build [--cpu] [--force]          Build llama.cpp (CUDA; --cpu for a no-GPU build, auto when no GPU)
  bob verify-urls [<profile>]          Check all HuggingFace download URLs (needs network)
  bob update                           Pull latest llama.cpp and rebuild
  bob gen                              Regenerate llama-swap.yaml, litellm.yaml, and Open WebUI system prompts

Tools:
  bob aider [args]                     Run aider in architect mode in the current folder
  bob webui                            Start Open WebUI only
  bob diagnose                         GPU, VRAM, CUDA, and model file health check
  bob mlock                            Check or grant SeLockMemoryPrivilege (needed for --mlock)
  bob version                          Show binary versions and submodule commits

Ecosystem:
  bob fabric-setup                     Build fabric from source and configure it for the local endpoint
  bob litellm [-NoWindow]              Start LiteLLM proxy (:8081), foreground or background
  bob litellm stop                     Stop the background LiteLLM proxy
  bob litellm status                   Show PID and uptime of the background LiteLLM proxy
  bob services start|stop|status|logs  Docker services: Langfuse (:3001) SearXNG (:8888) n8n (:5678)
  bob eval <role> [task] [--shots N]   Benchmark model quality (humaneval, mmlu, gsm8k)
```

If `bob` isn't found after setup, run `scripts\install-cli.ps1` and open a fresh terminal. That script also registers tab completions in your PowerShell profile (`bob <TAB>` completes subcommands, model roles, and profile names).

## What's ready after setup

Everything in this table works after `setup.bat` completes and the endpoint is running.
No extra steps.

| Feature | Entry point |
|---------|------------|
| Inference endpoint (API + streaming) | `bob serve` or `bob up` |
| Open WebUI browser chat | `bob up` (auto-opens `:3000`) |
| VS Code autocomplete + chat | Install Continue extension, then `bob serve` |
| VS Code agentic edits | Install Cline extension, configure Base URL `:8081` once |
| Terminal AI coding | `bob aider` from any project folder |
| Shell pattern pipes | `bob fabric-setup` once, then `git diff \| fabric --pattern write_git_commit` |
| LiteLLM proxy (retry + logging) | `bob litellm` |
| Model quality benchmarks | `bob eval coder humaneval` (needs endpoint running) |
| Continue MCP: read files (`@filesystem`) | Wired automatically; use `@filesystem` in Continue chat |
| Continue MCP: fetch URLs (`@url`) | Wired automatically; use `@url https://...` in Continue chat |

**Requires Docker Desktop** (installed by `install_prereqs.bat`; services start automatically at the end of `setup.bat` if Docker is present):

| Feature | What to run |
|---------|------------|
| Langfuse observability | Starts automatically; or manually: `.\scripts\setup-docker.ps1` then `bob services start` |
| SearXNG private web search | Same; starts as part of the Docker stack |
| n8n workflow automation | Same |
| Continue MCP `@web` search | Requires SearXNG running (`bob services start`) |
| Continue MCP `@github` | Set `GITHUB_TOKEN` environment variable (GitHub PAT with `repo` scope) |
| Langfuse tracing via LiteLLM | After Docker setup: get API keys from Langfuse UI → set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` env vars → set `langfuseEnabled = $true` in `config/user.psd1` → `bob gen && bob litellm` |

### Quick test scenarios

**1. Verify the endpoint is up and serving all models:**
```powershell
bob up -NoOpen
bob status          # should show: planner, coder, chat, fim, embed
bob chat coder "write a PowerShell one-liner that lists the 5 largest files in the current folder"
```

**2. Continue.dev autocomplete and inline edit:**
```
bob serve
# Open VS Code, open any source file
# Start typing a function (ghost text should appear within 1–2 seconds)
# Select a block of code, press Ctrl+I, type "add error handling"
# Accept or reject the diff that appears
```

**3. aider plan-then-edit workflow:**
```powershell
cd C:\my-project
bob up -NoOpen
bob aider
# In aider: /add src/parser.py
# Type: "add input validation. Raise ValueError if the input string is empty or contains only whitespace"
# Review the plan, press Enter to apply edits
# /undo   (rolls back if you don't like the result)
```

**4. fabric for quick prompt patterns:**
```powershell
bob up -NoOpen
git diff --staged | fabric --pattern write_git_commit
cat meeting-notes.txt | fabric --pattern extract_wisdom
cat error.log | fabric --pattern explain_code
fabric -l    # see all 254 patterns
```

**5. LiteLLM proxy with stop/status:**
```powershell
bob litellm -NoWindow    # starts proxy in background on :8081, logs to logs/litellm.log
bob litellm status       # shows PID and uptime
# Point Cline at http://localhost:8081/v1 to get retry-on-failure
bob litellm stop
```

**6. Model quality benchmark:**
```powershell
bob serve
bob eval coder gsm8k --limit 100   # quick smoke test (~8 min); math word problems
bob eval coder gsm8k               # full benchmark (~90 min)
bob eval planner mmlu --shots 5    # general knowledge, 5-shot (~90 min)
# Results in results/eval-coder-gsm8k-<timestamp>/
```

**7. Docker services (Langfuse + SearXNG + n8n):**
```powershell
.\scripts\setup-docker.ps1   # first time only (installs Docker Desktop if needed)
bob services start
bob services status          # verify all three containers are Up
# Langfuse: http://localhost:3001  (admin@local.dev / admin123)
# SearXNG:  http://localhost:8888
# n8n:      http://localhost:5678
# In Continue chat: @web what is the latest llama.cpp release?
```

## Starting the stack each session

```powershell
bob up        # endpoint on configured port (default 8080) + Open WebUI (default 3000)
```

`bob up` starts both services silently in the background (no terminal windows pop up), then waits inline for each to be ready: a spinner resolves to a green "ready (Ns)" line for the endpoint, then again for Open WebUI, then the browser opens. Typical wait: 5–15 s for the endpoint, 10–25 s for Open WebUI. If either doesn't respond within its timeout (60 s / 120 s) a warning is printed with a fallback URL.

The endpoint logs go to `logs/llama-swap.log`; tail them live with `bob logs`. Pass `-NoOpen` to skip the WebUI wait and browser open entirely:

```powershell
bob up -NoOpen    # start services, wait for endpoint only, don't open the browser
```

Check what's running and how much RAM each service is using:

```powershell
bob status    # which models are loaded in VRAM
bob ps        # daemon PIDs, RAM, and uptime
```

If you only need the API for IDE and terminal tools, use interactive mode instead: it stays in your terminal, shows output directly, and stops with Ctrl+C:

```powershell
bob serve     # inference endpoint at http://localhost:<port>/v1  (default: 8080)
```

The server loads a model into VRAM when it first receives a request, and unloads it when it's been idle for a while. The exception is `fim` (autocomplete) and `embed` (embeddings), which are pinned in VRAM and never unloaded. Only one large model (`planner`, `coder`, or `chat`) is resident at a time; switching between them takes a few seconds.

**mlock:** `fim` and `embed` are pinned in physical RAM with `--mlock`, preventing the OS from paging their weights to disk under memory pressure (e.g. simultaneous VS Code autocomplete, chat, and Open WebUI load). This locks approximately 4 GB of physical RAM permanently. On systems with less than 32 GB of RAM, disable it by setting `mlock = $false` on the `fim` and `embed` entries in `config/user.psd1` (gitignored per-machine override; re-run `bob gen` after editing).

Setting `mlockBig = $true` in `config/user.psd1` extends mlock to the swap-group models (planner, coder, chat), pinning CPU-offloaded weight pages against pagefile eviction. On Windows this requires `SeLockMemoryPrivilege` — run `bob mlock` to check status and grant the privilege automatically (UAC prompt; restart terminal after). On Linux, raise the memlock limit instead (`ulimit -l unlimited` or `/etc/security/limits.conf`).

To start automatically at login: on Windows put a shortcut to `up.ps1` in `shell:startup`, or create a Task Scheduler task set to "At log on" running `pwsh -File C:\bob\scripts\up.ps1 -NoOpen`. On Linux, add a user systemd unit or a `@reboot` cron entry running `pwsh -File <repo>/scripts/up.ps1 -NoOpen`.

## Available models (16gb profile)

| Name | Role | Backing model |
|---|---|---|
| `planner` | heavy reasoning and architecture | Qwen3-30B-A3B Q4 |
| `coder` | coding chat and agentic edits | Qwen2.5-Coder-14B Q4_K_M |
| `chat` | general conversation | Qwen3-14B Q4_K_M |
| `fim` | autocomplete (pinned) | Qwen-Coder-3B Q8_0 |
| `embed` | RAG embeddings (pinned) | bge-m3 Q8 |
| `vision` | image description and visual Q&A | Qwen2-VL-7B Q4_K_M + mmproj (Phase 2) |
| `agent` | local tool use and autonomous tasks | Hermes-3-Llama-3.1-8B Q5_K_M |

Every model's GGUF file, HuggingFace source, context size, and launch flags are defined once in [config/models.psd1](../config/models.psd1). The downloader and the runtime config both read from it. Clients reference the role names above (`coder`, `planner`, etc.), so swapping the backing model for a role never requires touching any client configuration.

The `12gb` profile uses smaller variants (about 21 GB on disk instead of 38 GB). The `8gb` profile targets cards like the RTX 3070 and 4060 and is marked unvalidated; it ships with the repo but has not been tested on physical hardware yet. Switch with `bob profile 12gb` or `bob profile 8gb`, or pass `-Profile` to `setup.bat` before the first model download.

There is also a `cpu` profile (a single tiny ~0.5 GB model) for **no-GPU** boxes — CI runners and dev laptops. It proves the serve/agent path works without a GPU (correctness and wiring, not performance); `bob profile auto` selects it automatically when no GPU is detected, and `bob build --cpu` produces a CUDA-off engine to run it. See [PORTABILITY.md](PORTABILITY.md).

### Pro models (API-backed, no platform fee)

Three additional model names are available via the LiteLLM proxy (`:8081`) when the corresponding API keys are set. They route **litellm → API provider directly** — no llama-swap hop, no OpenRouter markup.

| Name | Role | Provider | Backing model | Approx. cost |
|---|---|---|---|---|
| `chat-pro` | general conversation | DeepSeek | deepseek-chat (V4) | ~$0.27/M in |
| `planner-pro` | heavy reasoning | DeepSeek | deepseek-reasoner (R1) | ~$0.55/M in |
| `coder-pro` | coding | DeepSeek | deepseek-chat (V4) | ~$0.27/M in |
| `vision-pro` | cloud vision | DeepSeek | deepseek-chat (V4, vision-capable) | ~$0.27/M in |

**API keys** — all four pro roles currently route through DeepSeek, so only one key is needed. The system supports multiple providers; set additional keys to enable other peers:

```powershell
$env:DEEPSEEK_API_KEY = 'sk-...'   # platform.deepseek.com → API keys  (active: chat, coder, planner)
$env:ZHIPU_API_KEY    = 'sk-...'   # open.bigmodel.cn → API keys         (peer disabled by default)
```

Pro models are only available through `:8081` (LiteLLM). Direct `:8080` requests return "model not found" because llama-swap only serves local models.

**Override providers or models** in `config/user.psd1` (see the `peers` block in `config/user.psd1.example`). You can disable individual peers, change which model a role uses, or add OpenRouter as a fallback (5.5% platform fee applies). Run `bob gen` after any change.

**Bill control:** set a per-key spending limit on the provider dashboard as a hard stop independent of local config. Optionally add `budget` and `budgetPeriod` to the deepseek peer block in `user.psd1` for a LiteLLM-side cap (e.g. `budget = 15.0; budgetPeriod = '30d'`). Run `bob gen` after any change.

## Calling the API directly

The endpoint speaks the OpenAI chat completions API, so any HTTP client works:

```powershell
curl http://localhost:8081/v1/chat/completions -H "Content-Type: application/json" -d '{
  "model": "coder", "messages": [{"role":"user","content":"write a fizzbuzz in rust"}] }'
```

Or use the built-in streaming CLI (tokens appear as they generate):

```powershell
bob chat coder "write fizzbuzz in rust"
bob chat planner "design a caching layer" --sys "Be concise." --max 1024
```

The LiteLLM proxy port defaults to `8081` (`litellmPort` in `defaults`). The underlying llama-swap engine is on `8080` (`port` in `defaults`) but clients should use `8081` for retry logic, Langfuse tracing, and pro model access.

### Embeddings API

The `embed` model (bge-m3) exposes an embeddings endpoint:

```powershell
curl http://localhost:8081/v1/embeddings `
  -H "Content-Type: application/json" `
  -d '{"model": "embed", "input": "The quick brown fox"}'
```

Response shape:
```json
{
  "object": "list",
  "data": [{ "object": "embedding", "index": 0, "embedding": [0.023, -0.011, ...] }],
  "model": "embed",
  "usage": { "prompt_tokens": 5, "total_tokens": 5 }
}
```

The vector dimension is 1024. `embed` is pinned in VRAM and never unloads, so embedding calls never trigger a model swap. Use this endpoint to build your own RAG pipeline, or point any tool that accepts an embeddings endpoint at `http://localhost:8081/v1`.

**From Python (openai SDK):**
```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-local")
resp = client.embeddings.create(model="embed", input=["your text here"])
vector = resp.data[0].embedding   # list of 1024 floats
```

## Bob: the interactive assistant

Phase 1 of the Bob roadmap wires a persona, interactive chat, and memory on top of the inference stack. All of this is opt-in; the raw API, Continue, aider, and every other client are unaffected.

### The `bob` shell (default front door)

Run `bob` with no arguments on a terminal to open the interactive shell — a splash (big header, model/agency/session, live tool/command/skill counts, endpoint health) and a prompt:

```powershell
bob                 # opens the shell (piped/redirected `bob` prints help instead)
```

In the shell:

| Input | Does |
|-------|------|
| *(type anything)* | an agent turn — Bob answers and can use tools (streamed, Markdown-rendered) |
| `/agent <goal>` | run the agent loop explicitly on a goal |
| `/model [role]` · `/agency [show\|confirm\|silent]` | switch model / tool-approval mode |
| `/tools` · `/skills` · `/help` | the catalog (grouped commands + tools + skills) |
| `/session new\|list\|resume <id>\|show` · `/status` · `/clear` | persisted sessions (`data/sessions.db`) + state; leaving a session consolidates it into memory |
| `/theme [reload]` | show/reload the theme ([config/ui.json](../config/ui.json)) |
| `/exit` | leave |

Type `/` to filter the command list. Gated tools (e.g. `shell_run`, or any tool under `/agency confirm`) show an inline **y/N/a** approval; **Ctrl-C** cancels the in-flight turn and returns to the prompt. The look is themeable — see [TUNING.md → System prompts](TUNING.md) and `config/ui.json`.

`bob help` (from the terminal) prints the same generated command catalog.

### `bob chat` (routed REPL)

```powershell
bob chat          # opens the REPL — multi-turn, history in session, empty line to exit
bob think         # same but uses the planner (Qwen3-30B) — deeper reasoning
bob code          # same but uses the coder (Qwen2.5-Coder-14B) — code focus
```

Banner on entry:
```
Bob [chat | Qwen3-14B-Instruct-Q4_K_M]  (empty line to exit, !recall <query> to inject memory)
```

**Routing flags** — combine freely:

| Command | Routes to |
|---------|----------|
| `bob chat` | chat (local) |
| `bob chat --pro` | chat-pro (DeepSeek API) |
| `bob chat --think` | planner (local) |
| `bob chat --think --pro` | planner-pro (DeepSeek R1) |
| `bob chat --code` | coder (local) |
| `bob chat --code --pro` | coder-pro (DeepSeek API) |
| `bob think --pro` | planner-pro |
| `bob code --pro` | coder-pro |

**One-shot mode** — prompt as argument, no interactive loop:
```powershell
bob chat "explain what a semaphore is"
bob chat --pro "what is the fastest sorting algorithm for nearly-sorted data?"
bob think "design a caching layer for this service"
bob code "write a PowerShell function that retries a command N times"
```

**Legacy syntax** still works unchanged:
```powershell
bob chat coder "write fizzbuzz in rust"
bob chat planner "design a caching layer" --sys "Be concise." --max 1024
```

### Persona config

Bob's persona `name`/`style` and routing defaults live in `config/bob.psd1`; the base **`systemPrompt`** lives in the neutral `config/defaults.json` → `runtime.persona.systemPrompt` (shared by both OSes — see [TUNING.md → System prompts](TUNING.md)). Both are committed to the repo. Override any key in `config/user.psd1` under a `bob` section:

```powershell
# config/user.psd1
@{
  bob = @{
    persona = @{
      name = 'Bob'
      systemPrompt = 'You are Bob...'
    }
    routing = @{
      defaultRole = 'chat'
      proRole     = 'chat-pro'
    }
  }
}
```

### Memory

Bob stores and retrieves facts using SQLite + BGE-M3 embeddings. The `embed` model is already pinned
in VRAM, so memory costs 0 extra VRAM and one embed call per store/recall. **Memory is on by default**
(`memory.enabled = true`); disable it in `config/bob.psd1` with `memory = @{ enabled = $false }`.

This is a summary — the full reference (typed store, ranking, scoping, sessions, every config key) is
in **[MEMORY.md](MEMORY.md)**.

**From the terminal:**
```powershell
bob remember "I prefer dark mode in all editors"
bob recall  "editor preferences"    # blended-rank search (not plain semantic), prints JSON
bob memory list                     # browse what Bob knows
bob memory show 42                   # one row incl. its provenance (which session taught it)
bob memory pin 42                    # protect a fact from pruning
bob memory forget --session <id>    # retract everything a session taught Bob
bob memory status                   # DB path, size, per-type counts
bob memory clear --yes              # wipe all memories
```

**Automatic in the `bob` shell.** You don't manage memory by hand:

- At **session start** your stable profile (and any project [`BOB.md`](MEMORY.md#project-instruction-files))
  is injected once.
- At **session end** (`/exit`, `/session new`, …) durable facts are **consolidated** — the model
  extracts typed facts and *supersedes* contradictions instead of accumulating them ("I use vim" →
  later "I switched to vscode" leaves only vscode).
- Set `memory.autoRecall = true` to also inject relevant memories on **every turn** (off by default;
  otherwise the agent recalls on demand via the `memory_recall` tool).

Injected memory is capped at `memory.maxInjectedTokens` so it can't overflow the context window.
Memory is always local — even with `--pro`, recall and embedding stay on BGE-M3 at `:8081`.

> The legacy `bob chat` REPL still supports the manual `!recall <query>` / `!memory` meta-commands
> (single replaceable slot, no auto-injection). The modern front door is the `bob` shell, where
> memory is automatic.

Memory DB defaults to `data/bob.db` (gitignored); override with `memory.dbPath`.

### First run: onboarding

`setup.bat` triggers an interactive onboarding flow at the end of setup if `config/user.psd1` has no `bob` section yet:

```
Bob: Hi. What's your name?
> Siva
Bob: What kind of work do you do most?
> Game dev and AI tooling
Bob: Got a DeepSeek API key? (Enter to skip)
> sk-...
Bob: Ready. Type 'bob chat' to start.
```

This writes your name and work context to `data/bob.db` (profile table) and your API key to `config/user.psd1` (gitignored). Run `scripts\onboard.ps1` manually to redo it.

### Budget tracking

```powershell
bob budget    # shows LiteLLM spend (if proxy is running) + configured caps
```

Shows the `max_budget` and `budget_duration` from `config/litellm.yaml`, queries the LiteLLM proxy for spend data if it's running, and reports memory DB size at `$0 cost` (fully local). For detailed per-request cost breakdown, enable Langfuse tracing (see [Langfuse section](#langfuse-bob-observability)).

## Voice (Phase 2)

Voice adds two-way audio to the terminal using whisper.cpp (STT) and piper (TTS). All processing is local — no cloud, no microphone data leaving the machine.

**One-time setup:**
```powershell
bob setup-voice
```
Downloads `ggml-base.en.bin` (~74 MB), piper Windows release, and the Qwen2-VL mmproj file. Then flip the flag in `config/bob.psd1`:
```powershell
voice = @{ enabled = $true; ... }
```
`bob up` auto-starts the whisper server on port 8082 when `voice.enabled = $true` and `bin/whisper-server.exe` is present.

**Commands:**
```powershell
bob listen                          # record mic until 1.5 s silence → print transcript
bob transcribe path\to\audio.wav    # transcribe a file instead of recording
bob speak "Hello, I am Bob."        # synthesise and play (piper TTS)
echo "some text" | bob speak        # pipe stdin to TTS
bob voice                           # continuous loop: listen → chat → speak (Ctrl+C to stop)
bob voice --pro                     # same loop but routes chat to cloud (DeepSeek API)
```

**Pipeline use:**
```powershell
bob listen | bob chat | bob speak   # one-shot voice turn
```
`bob chat` streams ANSI to the terminal and returns clean text when piped — the spinner and colour codes are suppressed, so `bob speak` receives plain text.

**Whisper server management:**
```powershell
bob whisper start                   # start whisper STT server (port 8082)
bob whisper stop                    # stop the whisper server
bob whisper status                  # show PID and uptime
bob ps                              # shows whisper and piper rows alongside other services
bob status                          # includes whisper and piper UP/down lines
```

**Piper TTS HTTP server** (for Open WebUI integration):
```powershell
bob piper                           # start piper HTTP server on :8083 (OpenAI /v1/audio/speech)
bob piper stop                      # stop the piper server
bob piper status                    # show PID and uptime
```
Wire into Open WebUI: Admin Panel → Audio → Text-to-Speech Engine → `http://localhost:8083`

Wire whisper into Open WebUI STT: Admin Panel → Audio → Speech-to-Text Engine → `http://localhost:8082`
(Requires whisper-server to expose `/v1/audio/transcriptions` — verify with:
`curl -X POST http://localhost:8082/v1/audio/transcriptions -F "file=@test.wav" -F "model=whisper-1"`)

**Pipeline examples:**
```powershell
bob listen | bob chat | bob speak               # one-shot voice turn
bob listen | bob chat --pro | bob speak         # voice turn routed to cloud
cat article.txt | fabric --pattern extract_wisdom | bob speak   # read fabric output aloud
```

**Audio quality tips:**
- Use headphones to prevent the mic picking up speaker output.
- The energy-gate in `bob-voice-capture.py` silences blank audio before it reaches whisper.
- Whisper small (multilingual, ~300 ms on GPU) is the default. It handles accented English and non-English languages. To upgrade to medium (better accuracy, ~1.4 GB), set `sttModel = 'medium'` in `bob.psd1` and re-run `bob setup-voice`.

**Voice response tuning (all in `config/bob.psd1` under the `voice` block):**

| Key | Default | Effect |
|-----|---------|--------|
| `maxTokens` | `512` | Caps the voice reply length. Lower (e.g. `256`) for faster one-liners; raise if Bob cuts off mid-sentence on long answers. |
| `silenceSec` | `1.5` | Seconds of mic silence before recording stops. Raise if Bob cuts off while you're still speaking. |
| `systemPrompt` | *(voice-specific)* | The system prompt used only in `bob voice` — instructs the model to reply in plain spoken sentences with no markdown. Override in `config/user.psd1` under `bob.voice.systemPrompt`. |
| `sttModel` | `'small'` | Whisper model size: `tiny`, `base`, `small`, `medium`. Larger = more accurate, slower. Re-run `bob setup-voice` after changing. |

The voice loop also runs a `Format-ForSpeech` text sanitizer before sending to piper: it strips markdown symbols (`*`, `#`, `` ` ``, `_`, bullet dashes, numbered list markers, links) so stray markdown from the model never reaches the TTS engine. Combined with the voice system prompt, Bob should reply in natural spoken language without reading punctuation symbols aloud.

## Vision (Phase 2)

Vision uses Qwen2-VL-7B (a 5 GB GGUF + a ~1.5 GB mmproj) to describe images and answer visual questions. The model loads on demand and unloads after 30 seconds of idle to free VRAM for chat/coder.

**Setup:** `bob setup-voice` also downloads the mmproj. The GGUF itself downloads via `bob fetch` (it's part of the 16gb model profile). Flip the flag in `config/bob.psd1`:
```powershell
vision = @{ enabled = $true; visionRole = 'vision'; visionProRole = 'vision-pro' }
```

**Commands:**
```powershell
bob describe C:\path\to\image.png
bob describe C:\path\to\image.png "What text is visible?"
bob describe C:\path\to\image.png --pro "Analyse this diagram in detail"   # DeepSeek V4 vision
bob screenshot
bob screenshot "What application is open and what does it show?"
bob screenshot --pro "Explain the code on screen"                           # cloud vision
```

`--pro` routes to DeepSeek V4 (deepseek-chat), which supports vision input, using the existing
`DEEPSEEK_API_KEY`. Useful when Qwen2-VL output is insufficient or the image requires
stronger OCR/reasoning.

`bob describe` resizes the image to max 1024 px on the longest edge before encoding (large screenshots fit comfortably in the 4096-token context). `bob screenshot` captures the primary display, saves a temp PNG, calls `bob describe`, then deletes the temp file.

**Pipeline examples:**
```powershell
bob screenshot | fabric --pattern analyze_claims    # screenshot → vision → fabric analysis
bob describe img.png | fabric --pattern summarize   # describe image, pipe to fabric
bob screenshot --pro "Explain the code on screen"   # cloud vision for complex screenshots
```

**Known limitation:** `--flash-attn on` is incompatible with multimodal projection — `gen-llama-swap.ps1` automatically omits it when `mmproj` is set, so no manual config is needed.

## Agent (Phase 3)

The agent system runs Hermes 3 (8B) in a loop: the model reasons about what tools to call, executes them, and iterates until it has a final answer. All processing is local — tools include web search (SearXNG), git, file access, shell commands, and memory.

### Running a goal

```powershell
bob agent "what did I commit today and what files changed?"
bob agent "search the web for the latest Unreal Engine 5 release notes and summarise them"
bob agent "check git status, find any TODO comments in modified files, and list them"
```

The agent prints tool calls to stderr (cyan) and results (dark gray), then the final answer to stdout. Pipe to suppress:

```powershell
bob agent "summarise the last 10 commits" 2>$null
```

**Agency modes** — how much the agent asks before acting:

| Mode | Behaviour | Use when |
|------|-----------|----------|
| `show` (default) | Prints tool calls + results, runs automatically | Normal interactive use |
| `confirm` | Prompts `Execute? [y/N]` before each tool execution | Untrusted goals or destructive tools |
| `silent` | No output during execution; only the final answer | Scheduler, scripts, piped output |

Override for a single run: `bob agent --agency confirm "goal"`.  
Set permanently in `config/bob.psd1` → `agent.agency`.

### Available tools

| Tool | What it does | Needs |
|------|-------------|-------|
| `memory` | Store and recall facts from Bob's memory DB | `embed` model running |
| `web` | Search via SearXNG, fetch URLs | SearXNG running (:8888) |
| `git` | `git_status`, `git_log`, `git_diff` on any repo | git on PATH |
| `file` | `file_read` (within allowed paths), `file_write` (disabled by default) | `allowedReadPaths` set |
| `shell` | Run PowerShell commands (always prompts user — ignores agency mode) | Interactive terminal |
| `fabric` | Run any fabric pattern on text input | fabric on PATH |

Tools are **auto-discovered** from `scripts/tools/*.py` and `plugins/*/tool.py` — dropping in a file is the only registration step, there is no allowlist. To exclude one without deleting it, add its name to `config/bob.psd1` → `agent.disabledTools` (a denylist). `bob tools list` shows every discovered tool and its enabled/disabled status.

### Scheduling background goals

```powershell
# Add a daily git summary at 09:00
bob agent schedule add morning-summary --cron "0 9 * * *" --goal "check git log for today's work and write a one-paragraph summary to data/daily-summary.txt"

# List schedules
bob agent schedule list

# Run immediately (ignores cron, runs now)
bob agent schedule run morning-summary

# Enable/disable without removing
bob agent schedule disable morning-summary
bob agent schedule enable morning-summary

# Remove permanently
bob agent schedule remove morning-summary
```

Schedules are stored in `data/schedules.json`. A recurring task — a Windows Scheduled Task or a Linux cron entry, registered by `bob agent install` — fires `bob-agent.ps1` every minute, which checks which entries are due (5-field cron, 60 s double-fire guard). `bob agent status` shows its state.

The scheduler runs with `agency = 'silent'`. Results are stored in `data/schedules.json` under `lastRunResult`. If `notify = true` is set on an entry, a desktop notification fires with the result (a Windows toast, or `notify-send` on Linux).

### Memory clip

```powershell
bob clip https://example.com/article
```

One-shot web clip: fetches the URL, strips HTML, sends to the chat model for a 3–5 sentence summary, prints it, then stores `url: summary` to Bob's memory DB. No agent loop — one LLM call, fast.

### Tool calling format

Hermes 3 uses its own tool-calling format: tool schemas are injected into the system prompt and the model responds with `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` XML. Bob's agent loop handles this transparently — Qwen3 or other OpenAI-format models also work by setting `agent.toolFormat = 'openai'` in `config/bob.psd1`.

### HTTP server (REST + SSE)

```powershell
bob agent serve            # binds agent.serveHost:agent.agentPort (default 127.0.0.1:8084)
```

Exposes the agent loop over HTTP for n8n/WebUI/other clients. Every endpoint except `/health`
requires `Authorization: Bearer <token>` (the litellm key or an `agent.apiTokens` entry). Each token
maps to an owner, and sessions are owner-scoped — a token only sees sessions its owner created (N1).
Supports one-shot `POST /v1/agent/completions`, token-streaming `POST /v1/agent/completions/stream`
(SSE; cancels on client disconnect), and multi-turn `POST/GET/DELETE /v1/sessions`. Full endpoint
contract, event schema, and n8n wiring: [AGENT-SERVER.md](AGENT-SERVER.md).

### Check agent health

```powershell
bob setup check     # dependency + registration checks
bob doctor          # the above, plus runtime: endpoint reachable, GPU/VRAM, writable dirs, config.json parses
```

`bob setup check` verifies agent dependencies in order: venv, Python packages, config.json, tools
directory, schedules file, fabric, SearXNG, n8n, LiteLLM proxy, BobAgent task, Hermes 3 model file,
tool loading (delegated to the Python loader, honoring `agent.disabledTools`), litellm.yaml. Prints ✓
or ✗ with a fix command for each failure. `bob doctor` runs all of those plus a runtime pre-flight —
use it as the first thing to run when something's off.

## Plugins (Phase 3)

Plugins are direct CLI commands that live in `plugins/<name>/invoke.ps1` or `invoke.py`. Unlike agent tools (which the LLM decides to call autonomously), plugins are invoked explicitly by you — `bob search "..."` runs immediately.

Python plugins run in `venv-litellm`; PowerShell plugins run directly. Each plugin is self-contained: a directory with one invoke file and an optional `description.txt`.

```powershell
bob plugins list    # show all installed plugins
```

### bob summarise

Summarise a file or piped text via the local chat model.

```powershell
# From a file
bob summarise README.md
bob summarise docs/USAGE.md --length short

# From stdin
cat meeting-notes.txt | bob summarise
git diff | bob summarise --length long

# Length options: short (2-3 sentences), medium (paragraph, default), long (structured with key points)
bob summarise report.md --length long
```

Content is capped at 12 000 characters before sending to the model (truncation noted in output).

### bob draft

Draft text from a one-line prompt. Use `--type` to set the tone and format; the right model role is chosen automatically (`planner` for email/doc, `chat` for slack, etc.).

```powershell
# Email
bob draft "apologise for missing the deadline, propose a new date of Thursday" --type email

# PR description
bob draft "add streaming support to the chat completions endpoint" --type pr

# Slack message
bob draft "let the team know the prod deploy is done and to monitor for errors" --type slack

# Technical documentation
bob draft "explain how the plugin fallback works in bob.ps1" --type doc

# Free-form (no type)
bob draft "write a LinkedIn post about building a local AI assistant"
```

Output is clean and ready to paste — no preamble, no "Here is your draft:" wrapper.

### bob search

Search files in the current directory (or `--path`) using ripgrep, then feed the results to the local LLM for synthesis. Use `--raw` to see raw grep output without LLM synthesis.

```powershell
# Search current directory
bob search "TODO"
bob search "error handling"

# Search a specific directory
bob search "API endpoints" --path src/

# Filter by extension
bob search "config loading" --ext .py

# Raw output (no LLM, just grep)
bob search "memory" --raw

# The LLM output references file names and line numbers from the grep results
```

Requires ripgrep (`rg`) for best results; falls back to Windows `findstr` if `rg` is not on PATH.

### bob play

Open Spotify with a search query (if Spotify is installed). Falls back to YouTube Music in the browser.

```powershell
bob play lofi hip hop
bob play pink floyd dark side of the moon
bob play focus instrumental for coding
bob play --ytm jazz piano    # force YouTube Music even if Spotify is installed
```

Spotify detection checks `%APPDATA%\Spotify\Spotify.exe` and the Windows Store location.

### Adding your own plugins

Create `plugins/<name>/invoke.ps1` or `invoke.py` — any language, just read `$args` (PowerShell) or `sys.argv` (Python) and write to stdout.

```
plugins/
  my-plugin/
    invoke.py          # or invoke.ps1
    description.txt    # one-line description shown by bob plugins list
```

Python plugins have `venv-litellm` available — `from bob_core import load_config, get_llm_client` gives you config and an LLM client pointed at the local proxy. See `plugins/summarise/invoke.py` for a complete example.

There is no registration step. `bob my-plugin` dispatches to the plugin automatically via the default-case fallback in `bob.ps1`.

---

## Qwen3 Thinking Mode

Qwen3 models (`planner`, `chat`) support a reasoning scratchpad. `planner` has it on by default — appropriate for deep analysis. `chat` has it off by default via its system prompt (`/no_think`) because conversational responses don't benefit from the added latency. When enabled, the model internally reasons through the problem before responding. This produces better answers but:

- **Consumes `max_tokens` silently.** The scratchpad counts toward your token limit.
  For complex tasks, set `max_tokens` to at least 2000 (or 8192 for deep planning).
- **Increases first-token latency.** The scratchpad runs before any visible output.
  For quick questions, use `/no_think` to skip it.

### Disabling the scratchpad

Append `/no_think` to your prompt:

```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "chat",
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "What is 2+2? /no_think"}]
  }'
```

Or via `bob chat`:

```powershell
bob chat chat "What is 2+2? /no_think" --max 128
```

### When to use each mode

| Mode | When to use | `max_tokens` |
|------|------------|-------------|
| Default (thinking on) | Complex reasoning, code architecture, planning | 2000–8192 |
| `/no_think` | Quick Q&A, simple edits, autocomplete-like tasks | 128–512 |

**In Continue.dev:** The `chat` model has `/no_think` set in its system prompt by default. For `planner`, add `/no_think` to your message to skip the scratchpad on simpler tasks. Continue always uses the configured `maxTokens`; make sure it's large enough for planning tasks.

**In aider:** The planner model is used for architecture; thinking mode is appropriate.
aider auto-adjusts context size; no special configuration needed.

## Function Calling (Tool Use)

The `coder` model supports function calling. Define functions the model can request,
then execute them in your application:

```powershell
$tools = @(
    @{
        type = 'function'
        function = @{
            name = 'read_file'
            description = 'Read the contents of a file'
            parameters = @{
                type = 'object'
                properties = @{ path = @{ type = 'string'; description = 'Path to the file' } }
                required = @('path')
            }
        }
    }
)

$body = @{
    model    = 'coder'
    messages = @(@{ role = 'user'; content = 'What is in the file README.md?' })
    tools    = $tools
    tool_choice = 'auto'
} | ConvertTo-Json -Depth 10

$response = Invoke-RestMethod http://localhost:8081/v1/chat/completions `
    -Method POST -ContentType 'application/json' -Body $body

$choice = $response.choices[0]
if ($choice.finish_reason -eq 'tool_calls') {
    foreach ($tc in $choice.message.tool_calls) {
        $args = $tc.function.arguments | ConvertFrom-Json
        Write-Host "Model calls: $($tc.function.name)($($args | ConvertTo-Json -Compress))"
        # Execute the function, add result to messages, continue conversation...
    }
}
```

**Supported:** `coder` (Qwen2.5-Coder-14B). **Not supported:** `planner`, `chat`. Qwen3 tool-use quality varies; use `coder` for agentic tasks.

**In aider:** Tool use handled internally. **In Cline:** Point at `coder` for best results.

## VS Code: Continue.dev (autocomplete and chat)

Continue.dev provides inline autocomplete and a chat panel inside VS Code. `setup-clients.ps1` links the repo's config into `~/.continue/config.yaml`, so all models are wired with no in-editor setup needed.

To get started, run `.\scripts\setup-clients.ps1` once, install the **Continue** extension from the VS Code Marketplace, then start the endpoint with `bob serve` or `bob up`. Open the Continue panel with the sidebar icon or `Ctrl+L` and the `coder` and `planner` models should appear immediately.

**How models map to Continue roles:**

| Continue role | Model | Purpose |
|---|---|---|
| Chat, edit, apply | `coder` (Qwen2.5-Coder-14B) | default coding chat and inline edits |
| Chat, edit | `planner` (Qwen3-30B-A3B) | architecture discussion and heavy reasoning |
| Chat | `chat` (Qwen3-14B) | general conversation; thinking off by default |
| Chat, edit | `chat-pro` (DeepSeek V4, API) | general conversation via API |
| Chat, edit, apply | `coder-pro` (DeepSeek V4, API) | coding via API |
| Chat | `planner-pro` (DeepSeek R1, API) | heavy reasoning via API |
| Chat | `vision` (Qwen2-VL-7B, local) | image description and visual Q&A |
| Autocomplete | `fim` (Qwen-Coder-3B, pinned) | as-you-type ghost text completions |
| Embed | `embed` (bge-m3, pinned) | `@codebase` and `@docs` RAG indexing |

System prompts are set per-model: `coder` uses a direct engineering style prompt; `chat` includes `/no_think` to suppress the Qwen3 scratchpad for conversational use; `planner` has no prompt so thinking runs freely. Pro model prompts are configured in the `peers.deepseek.pro` block in `models.psd1` and synced to Open WebUI by `bob gen`. The `systemMessage` field in `config/continue/config.yaml` sets prompts for Continue specifically.

`Ctrl+L` opens a new chat with any selected code attached as context. `Ctrl+I` opens an inline edit on the selected lines and shows a diff for you to accept or reject. Autocomplete fires as ghost text; `Tab` accepts it. Use the model dropdown at the bottom of the chat panel to switch between roles.

Context is 32768 tokens for all models except `planner` (16384). Large `@codebase` queries get truncated to fit; use `@file` when you need to be precise about what's included. The first message to a large model is slower while it loads into VRAM. `fim` and `embed` stay pinned so autocomplete and RAG never trigger a reload.

If you used a copied config rather than a symlink and later edited the repo's config, re-run `setup-clients.ps1` after deleting the copy, or edit `~/.continue/config.yaml` directly.

### Continue.dev MCP Servers

Four MCP servers are wired into Continue automatically via `config/continue/config.yaml`. They activate in the Continue chat panel as context providers after `setup-clients.ps1` runs.

| Server | How to invoke | What it does |
|--------|--------------|-------------|
| `filesystem` | `@filesystem` then a path | Read files in `C:\Users\vsiva\dev` and `C:\bob` (strict whitelist; paths outside return permission denied) |
| `fetch` | `@url https://...` | Fetch any URL and include its text as context |
| `github` | `@github` then a query | Search GitHub issues, PRs, and code |
| `searxng-search` | `@web` then a query | Private web search via local SearXNG |

**Prerequisites:**
- `filesystem`, `github` require Node.js (installed by `setup.bat`).
- `fetch` requires uv / `uvx` (installed by `setup.bat`).
- `github` requires `GITHUB_TOKEN` set as a user or system environment variable. Without it the server loads but all queries return auth errors. Create a token at GitHub → Settings → Developer Settings → Personal access tokens (classic) with `repo` scope.
- `searxng-search` requires the Docker stack running (`bob services start`). If Docker is stopped, `@web` queries return nothing silently.

If a server fails to load, Continue shows a warning badge on its name in the chat panel. Click it to see the error. Most failures are a missing `node`, `uvx`, or `GITHUB_TOKEN`.

## VS Code: Cline (agentic)

Cline is a more autonomous agent that reads and writes files, runs commands, and works across many turns. It's not auto-wired; configure it once in its settings panel.

Install the **Cline** extension, start the endpoint, then open Cline settings and set the API provider to `OpenAI Compatible`:

| Field | Value |
|---|---|
| Base URL | `http://localhost:8081/v1` (replace `8081` if you changed `defaults.litellmPort`) |
| API Key | `sk-local` (any non-empty string; the server ignores it) |
| Model ID | `coder` |

Set the context window to `16384` to match the server's limit. Leaving it higher causes Cline to send requests the server can't handle. Leave image support off; these models are not multimodal.

To use separate models for planning and editing, enable **Use different models for Plan and Act** in Cline settings and set the Plan Model ID to `planner` and the Act Model ID to `coder`. Switching between modes evicts the other model from VRAM, so there is a brief load pause.

Cline burns through its 16k context window quickly on multi-step tasks. Keep tasks focused and start a new task when the history grows large. For tasks that need deeper reasoning, set the Model ID to `planner`; it's slower but handles complex planning better. Switching models evicts the other from VRAM.

## Terminal: aider (plan and edit separately)

Aider is the one client here with a genuine planning-versus-editing split. `planner` (Qwen3-30B) drafts the change, and `coder` (Qwen2.5-Coder-14B) turns that draft into file edits. You review the plan before any edit lands.

Run `.\scripts\setup-clients.ps1` to link the aider config (`config/aider/.aider.conf.yml`) into your home directory. aider picks it up automatically from there. Then start the endpoint and run aider from any project:

```powershell
cd <your-project>
bob aider
```

The config sets `architect: true`, which sends your request to `planner` first. It writes a prose description of what needs to change, then `coder` turns that into a diff. With `auto-accept-architect: false`, you see the plan and press Enter to apply it, or refine it before anything is written. Each turn triggers a VRAM swap between `planner` and `coder`, which takes a few seconds.

Useful in-session commands:

| Command | What it does |
|---|---|
| `/add <file>` | add a file to the editable context |
| `/read <file>` | add a file as read-only reference |
| `/ask <question>` | ask a question without triggering any edits |
| `/diff` | show pending changes |
| `/undo` | revert aider's last committed edit |
| `/drop` | remove files from context when it gets large |

aider commits each accepted edit to git automatically. Work on a branch so `/undo` can roll back cleanly. Both models use a 16k context window; on large repos, prefer `/read` over `/add` for files you're only referencing, and use `/drop` to remove files you no longer need. The `openai/` prefix in the config (`openai/planner`, `openai/coder`) is required for aider to route through a local endpoint and is already set correctly.

## Shell AI Patterns: fabric

fabric transforms piped text through a named prompt pattern: a structured prompt with a
specific output format baked in. Where `bob chat` is a blank canvas, fabric patterns encode
the _format_ of the answer (commit message, executive summary, code review checklist) so you
don't rewrite the same prompt structure every time. Patterns live in
`~/.config/fabric/patterns/`, each a directory with a `system.md` you can inspect or copy to
build your own.

It ships as a Go binary built from the `external/fabric` submodule; no winget, no download.
Run `bob fabric-setup` once to build and configure it:

```powershell
bob fabric-setup
```

This builds `bin/fabric.exe` from `external/fabric/cmd/fabric/` and copies the 254 patterns
from `external/fabric/data/patterns/` to `~/.config/fabric/patterns/`. Then pipe any text:

```powershell
# Write a commit message from the staged diff
git diff --staged | fabric --pattern write_git_commit

# Summarize a document or log
cat notes.txt | fabric --pattern summarize

# Explain an error
cat error.log | fabric --pattern explain

# Code review
cat myfile.py | fabric --pattern code_review

# Extract action items from meeting notes
cat meeting.txt | fabric --pattern extract_wisdom
```

fabric uses the `coder` model by default. Pass `--model planner` for complex analysis tasks.
Run `fabric -l` to see all 254 available patterns.

To update patterns after a submodule bump: re-run `bob fabric-setup` (patterns re-copied,
binary rebuilt only if `bin/fabric.exe` is missing; delete it first to force a rebuild).

## Ecosystem Services

### LiteLLM proxy

LiteLLM sits between clients and llama-swap, adding retry logic and structured request logging.

```powershell
# Start proxy on port 8081 (foreground, Ctrl+C to stop)
bob litellm

# Start in background (PID tracked, stop/status commands work)
bob litellm -NoWindow
```

All clients (Continue, aider, Cline, fabric, Open WebUI, `bob chat`) are configured to use `:8081` by default. The proxy exposes all local model names (`coder`, `planner`, `chat`, `fim`, `embed`) plus pro model names (`chat-pro`, `planner-pro`, `coder-pro`) when API keys are set. Direct `:8080` access to llama-swap still works for local models but bypasses retry logic and Langfuse tracing.

`config/litellm.yaml` is generated automatically by `bob gen` and `bob serve` — do not edit it by hand.

### Docker services (Langfuse + SearXNG + n8n)

CPU-only services run in Docker Desktop. GPU tools (llama.cpp, Open WebUI) stay native for maximum performance.

```powershell
# One-time setup (see SETUP.md for the full install walkthrough)
.\scripts\setup-docker.ps1

# After setup, manage with:
bob services start    # writes .env from models.psd1, starts containers, prints state table
bob services stop     # stops containers (data is preserved)
bob services status   # show container names, state, and uptime
bob services logs     # tail all container logs (Ctrl+C to stop)
```

Docker Desktop must be running before `bob services start`; it does not auto-launch. Start it from the Start menu or system tray (look for the whale icon) if needed.

Override ports or timezone in `config/user.psd1` (file is gitignored, safe for per-machine settings):
```powershell
@{ defaults = @{ langfusePort = 3001; searxngPort = 8888; n8nPort = 5678; n8nTimezone = 'UTC' } }
```
After changing any of these, re-run `.\scripts\setup-docker.ps1` to regenerate `.env` and restart containers.

**Persistent data** lives in gitignored directories under `tools/`:
- `tools/langfuse-data/`: Postgres database containing all Langfuse traces, projects, and API keys
- `tools/n8n-data/`: n8n workflows, credentials, and execution history

These survive `bob services stop` and `bob services start`. They are deleted if you run `docker system prune -af` (used for fixing corrupted images; see Troubleshooting below). Back them up if you have valuable Langfuse history or n8n workflows.

---

#### Langfuse: bob observability

Open `http://localhost:3001`. Default login: `admin@local.dev` / `admin123`.

Langfuse records every bob request routed through LiteLLM: the full prompt, response, model name, latency (time to first token + total), token counts, and any retry events. Use it to:

- **Debug unexpected answers**: see the exact system prompt and user message the model received, not what your code sent before transformation
- **Compare quant levels**: run `bob eval` before and after a profile switch, then look at Langfuse to see if latency changed alongside accuracy
- **Audit agentic tools**: see every turn aider or Cline makes, including tool calls and their results
- **Track token burn**: spot which workflows are expensive before they become a problem

**Enabling tracing (required; Langfuse doesn't auto-capture requests):**

Tracing only works through LiteLLM. Direct `:8080` requests are invisible to Langfuse.

1. Start Docker services: `bob services start`
2. Open `http://localhost:3001` → **Settings → API Keys** → create a key pair, copy the **Public Key** and **Secret Key**
3. Set the keys as environment variables (add to your PowerShell profile for persistence):
   ```powershell
   $env:LANGFUSE_PUBLIC_KEY = 'pk-lf-...'   # paste your public key
   $env:LANGFUSE_SECRET_KEY = 'sk-lf-...'   # paste your secret key
   ```
4. Enable Langfuse callbacks by adding one line to `config/user.psd1`:
   ```powershell
   @{ defaults = @{ langfuseEnabled = $true } }
   ```
5. Regenerate `config/litellm.yaml` and restart the proxy:
   ```powershell
   bob gen
   bob litellm stop
   bob litellm -NoWindow
   ```
6. Point your client at `:8081` instead of `:8080` (or use `bob chat` which goes through LiteLLM automatically)
7. Requests appear in the Langfuse dashboard under **Traces** within a few seconds

> **Note:** `config/litellm.yaml` is generated on every `bob gen` and `bob serve`. Do not edit it directly — changes are overwritten. Use `user.psd1` for all persistent customization.

---

#### SearXNG: private web search

Open `http://localhost:8888` for a search UI. Queries go to Google, Bing, DuckDuckGo, and others in parallel; SearXNG aggregates the results. Your IP talks to SearXNG locally; SearXNG talks to search providers on your behalf.

**Using `@web` in Continue.dev:**

When Docker services are running, the Continue MCP server `searxng-search` becomes active. In any Continue chat message, prefix your query:

```
@web what is the latest llama.cpp release?
@web python asyncio best practices 2025
@web site:github.com llama.cpp quantization
```

Continue sends the query to SearXNG, fetches the top results, and includes them as context before sending to the model. If Docker is stopped, `@web` returns nothing silently; start services first.

**As a browser search engine:** Go to browser settings → Search engines → Add:
- Name: `local`
- URL: `http://localhost:8888/search?q=%s`
- Shortcut: `s`

Type `s <query>` in the address bar to search privately.

Config lives at `config/searxng/settings.yml` (committed to the repo; edit it to enable or disable specific search engines or change safe-search level).

---

#### n8n: workflow automation

Open `http://localhost:5678`. No login required on first run; set up an account on first visit (credentials stay local in `tools/n8n-data/`).

n8n is a visual workflow builder. Each workflow is a graph of nodes: triggers (webhook, schedule, file watch) connected to actions (HTTP request, email, code). Build without writing scripts.

**Connecting to the local LLM:**

Inside n8n containers, the host machine is reachable at `host.docker.internal`. The inference endpoint is:
```
http://host.docker.internal:8081/v1/chat/completions
```

Add an **HTTP Request** node:
- Method: `POST`
- URL: `http://host.docker.internal:8081/v1/chat/completions`
- Header: `Authorization: Bearer sk-local` (any non-empty string)
- Body (JSON):
  ```json
  {
    "model": "coder",
    "messages": [{"role": "user", "content": "{{ $json.text }}"}]
  }
  ```

The response is `choices[0].message.content`; wire that to whatever you want (Slack, email, file, another bob call).

**Example workflows:**
- **PR summarizer**: GitHub webhook trigger → fetch PR diff → HTTP Request to `coder` → post summary comment
- **Daily digest**: Schedule trigger → fetch RSS feed → HTTP Request to `planner` → email summary
- **Commit message generator**: Webhook from git hook → send staged diff → return message to terminal

n8n schedules run in UTC by default. To use local time, set `n8nTimezone` in `config/user.psd1` (e.g. `'America/New_York'`) and re-run `.\scripts\setup-docker.ps1`.

**Tip:** prefer the LiteLLM proxy at `:8081` over the direct endpoint `:8080` — it adds automatic retry when the model is mid-swap.

**Starter workflows:**

A ready-to-import workflow lives at `tools/n8n-workflows/daily-research-digest.json`. See `tools/n8n-workflows/README.md` for the full import guide. Quick summary:

1. Open `http://localhost:5678` → top-right menu (≡) → **Import from file** → select the `.json`
2. Open the imported workflow → edit the **Config** node:
   - `discord_url` — paste your Discord webhook URL (Server Settings → Integrations → Webhooks → New Webhook)
   - `rss_feed_url` — RSS feed to follow (default: Hacker News)
   - `keywords_csv` — optional comma-separated filter (empty = all articles)
3. Click **Save** → toggle **Active** to enable the daily 8am schedule

The workflow has two modes:
- **Scheduled digest**: fetches RSS, verifies each article via SearXNG, summarizes one-by-one with the local LLM, posts Discord embeds with clickable links and a ✅/⚠️ corroboration badge
- **On-demand research**: POST `{"topic": "your topic"}` to `http://localhost:5678/webhook/research-digest` → SearXNG searches the topic → bob synthesizes → Discord

---

#### Troubleshooting Docker

| Symptom | Cause | Fix |
|---------|-------|-----|
| `exec /bin/sh: exec format error` on any container | Image layers corrupted by interrupted download (e.g. daemon crashed mid-pull) | `docker system prune -af` then re-run `.\scripts\setup-docker.ps1`; this deletes all cached images and re-downloads clean copies (~3 GB) |
| `langfuse-postgres unhealthy`, `dependency failed to start` | Postgres container failed to start; almost always the corrupted-layer issue above | Same: `docker system prune -af` + re-run setup |
| `500 Internal Server Error` on all `docker` commands | WSL2 backend not started or crashed | Restart Docker Desktop from system tray → wait for whale icon to go solid (60–90 s) |
| `@web` in Continue returns nothing | Docker services not running, or SearXNG container stopped | `bob services status`; if any container is not `Up`, run `bob services start` |
| Langfuse dashboard shows no traces | Tracing not enabled; LiteLLM not configured or not running | Follow the "Enabling tracing" steps above; confirm `bob litellm status` shows running |
| Port already in use | Another process on 3001, 8888, or 5678 | Set override ports in `config/user.psd1`, re-run `.\scripts\setup-docker.ps1` |
| Containers stop after reboot | `restart: unless-stopped` is set but Docker Desktop didn't start | Enable Docker Desktop → Settings → General → "Start Docker Desktop when you log in" |
| Lost n8n workflows or Langfuse history | `docker system prune -af` deleted `tools/langfuse-data` and `tools/n8n-data` | These are not recoverable without a backup; back them up before running prune |
| `bob services logs` shows nothing for SearXNG | SearXNG logs are suppressed by design (noisy access logs) | To debug, temporarily change `logging.driver: none` to `logging.driver: json-file` in `tools/compose/docker-compose.yml`, re-run `docker compose -f tools/compose/docker-compose.yml up -d` |

#### Updating Docker service images

To pull newer versions of Langfuse, SearXNG, or n8n:

1. Update the image tag in `tools/compose/docker-compose.yml` (e.g. `langfuse/langfuse:2` → bump the date tag on SearXNG, or `n8nio/n8n:1.101.0`)
2. Pull the new images and restart:
   ```powershell
   docker compose -f tools\compose\docker-compose.yml pull
   bob services stop
   bob services start
   ```

Persistent data in `tools/langfuse-data/` and `tools/n8n-data/` is preserved across image updates. Back them up before a major version upgrade in case the new container runs a migration that isn't backwards-compatible.

### Model quality benchmarks

`bob eval` uses [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness), an open-source benchmarking framework that runs standardized tasks against any compatible endpoint and returns a reproducible accuracy score. This is separate from `bob bench`, which measures throughput (tokens/sec); `bob eval` measures *answer quality*.

**Why run it:** VRAM savings from lower quant levels come at an accuracy cost. Speed and VRAM are easy to measure; `bob eval` closes the loop on whether a model or quant change actually degraded the answers.

Requires `bob serve` running first. The eval venv is created by `setup.bat`; no separate bootstrap needed.

```powershell
# Quick smoke test (recommended first run, ~8 min)
bob eval coder gsm8k --limit 100

# Full benchmarks (saved to results/)
bob eval coder gsm8k            # math word problems (~90 min)
bob eval coder humaneval        # code generation (~3 hr)
bob eval planner mmlu           # general knowledge (~90 min)
bob eval coder gsm8k --shots 5  # 5-shot variant (slightly higher scores, longer)
```

Results are saved as JSON under `results/eval-<role>-<task>-<timestamp>/<role>/results_<timestamp>.json`. The primary metric is `exact_match,flexible-extract` (accuracy 0.0–1.0; the flexible extractor finds the final number in the response). Reference points for 14B Q4 quant models at **5-shot**:

| Task | Measures | Expected (5-shot) | Expected (0-shot) |
|------|---------|-------------------|-------------------|
| `gsm8k` | math word problems | 0.72–0.82 | 0.60–0.72 |
| `humaneval` | code generation pass@1 | 0.60–0.72 | 0.50–0.65 |
| `mmlu` | general knowledge | 0.62–0.70 | 0.55–0.65 |

Scores well below these ranges usually mean the chat template wasn't applied correctly. Run the same task before and after a quant change or profile switch to measure quality delta.

## Browser chat and RAG: Open WebUI

`bob up` starts Open WebUI on port 3000, pre-wired to the local inference endpoint and embedding model. There's no manual admin setup. If you want it without the inference stack, use `bob webui`.

Open WebUI uses the `embed` model for document search automatically. Add documents through the workspace panel; they're indexed locally and available in any chat via the RAG interface. You can create model presets in Workspace → Models, for example a "Planner" preset with low temperature for precise answers, or a "Chat" preset for general conversation.

> **Agent model in WebUI:** Selecting the `agent` model in Open WebUI runs raw Hermes 3 inference — tool schemas are not injected and `<tool_call>` blocks appear as plain text. For full tool use, run `bob agent "goal"` in the terminal, or start `bob agent serve` and call `http://localhost:8084/v1/agent/completions` from n8n or any HTTP client.

## Customizing your setup: config/user.psd1

`config/user.psd1` is a per-machine override file that is gitignored. Create it to change ports, timezones, mlock behaviour, or the active model profile without touching the shared `config/models.psd1`. Every script that reads config merges `user.psd1` on top of `models.psd1`, so anything you set here wins.

Create the file if it doesn't exist:

```powershell
# Minimal example: override just the things you need
@{
    activeProfile = '16gb'        # or '12gb', '8gb'
    defaults = @{
        port         = 8080       # inference endpoint port
        langfusePort = 3001
        searxngPort  = 8888
        n8nPort      = 5678
        n8nTimezone  = 'America/New_York'  # IANA timezone string
    }
} | Export-Clixml config\user.psd1
```

Or just write it as a plain PSD1 hashtable:

```powershell
# config/user.psd1
@{
    defaults = @{
        n8nTimezone = 'America/New_York'
    }
}
```

After changing ports or timezone, re-run `.\scripts\setup-docker.ps1` to regenerate `.env` and restart containers. After changing `activeProfile`, run `bob gen` to regenerate the server config, then `bob fetch` to download any missing model files.

`bob gen` regenerates `config/llama-swap.yaml`, `config/litellm.yaml`, and Open WebUI system prompts from `models.psd1` + `user.psd1` without restarting the server; useful after editing model parameters, peer configuration, or system prompts:

```powershell
bob gen       # regenerate both configs (no restart needed for the next bob serve)
```

---

## Managing model profiles

`config/models.psd1` defines all models grouped into profiles. The `activeProfile` key at the top selects which one is used.

```powershell
bob profiles             # list all profiles with VRAM footprints and current selection
bob profile 12gb         # switch profiles and regenerate the server config
bob profile auto         # detect GPU VRAM and switch to the best-fit profile automatically
bob fetch --list 12gb    # preview what the 12gb profile would download, without downloading
bob fetch                # download any models the current profile is missing
bob show coder           # file path, size, SHA256, and disk status for a specific role
```

Switching profiles does not delete models from previous profiles; they stay in `models/`. Run `bob fetch` after switching to pull any files the new profile needs that aren't already there.

To add or change a model, edit its entry in `config/models.psd1` (setting `repo`, `path`, `gguf`, `ctx`, and any optional flags), then run `bob fetch` to download it and `bob serve` to pick it up. The server configs (`config/llama-swap.yaml` and `config/litellm.yaml`) are generated automatically on each launch and should never be edited by hand.

To add a new profile for a different VRAM tier, add a new key under `profiles` in the PSD1 file and switch to it with `bob profile <name>`.

## Keeping the stack current

**Inference engine (llama.cpp):**
```powershell
bob update    # pulls latest llama.cpp submodule commit and rebuilds
```
This is equivalent to bumping the submodule, running `build-llama.ps1 -Force`, and copying the new binary. See [TUNING.md](TUNING.md#bumping-the-llamacpp-submodule) for how to verify performance didn't regress after an update.

**Docker services (Langfuse, SearXNG, n8n):** bump image tags in `tools/compose/docker-compose.yml` and re-pull (see [Updating Docker service images](#updating-docker-service-images) above).

**Python venv dependencies:** delete the relevant `tools/venv-*` directory and re-run `setup.bat`; it recreates missing venvs automatically.

**Fabric patterns:** re-run `bob fabric-setup` after bumping the `external/fabric` submodule; it re-copies the pattern directory.
