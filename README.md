# Bob Agent

**Bob: Your Private AI Assistant for Windows**
Leverages your GPU for local processing, with optional cloud connectivity (DeepSeek, OpenAI-compatible) for advanced capabilities. **local by default, cloud on demand.**

Bob chats, listens, speaks, sees, and acts as an agent that can search your code, summarise files, draft documents, and schedule tasks on your behalf.

## What Bob does

| Command | What it does |
|---|---|
| `bob chat` | Conversational assistant. `--think` for deep reasoning, `--pro` for cloud. |
| `bob voice` | Continuous voice loop: speak, Bob replies out loud. Whisper STT + piper TTS. |
| `bob describe <image>` | Describe an image or screenshot. `--pro` routes to DeepSeek vision. |
| `bob agent "goal"` | Agentic task loop: plans, uses tools, executes steps. Schedulable via cron. |
| `bob summarise <file>` | Summarise a file or piped text. `--length short/medium/long`. |
| `bob draft "<prompt>"` | Draft an email, PR description, Slack message, or doc from a one-liner. |
| `bob search "<query>"` | Ripgrep your codebase and synthesise the results. |
| `bob play <music>` | Open a song or artist in Spotify or YouTube. |
| `bob clip <url>` | Fetch a page, summarise it, and store it to memory. |
| `bob remember "<text>"` | Store a fact for future sessions. |
| `bob recall "<query>"` | Search Bob's memory (blended semantic + recency + importance rank). |
| `bob memory <cmd>` | Inspect/curate memory: `list`, `show`, `edit`, `pin`, `forget`, `export`, … |

Agent tools (callable by `bob agent` autonomously): memory, web, git, file, shell, fabric, summarise, draft, search, play.

## What the stack includes

| Tool | Role |
|---|---|
| Open WebUI `:3000` | Browser chat, RAG, image input, voice (once wired in admin panel) |
| Continue.dev | VS Code autocomplete, chat, `@web`, `@codebase`, `@filesystem` |
| Cline | VS Code agent: reads and writes files, runs commands |
| aider | Terminal coding agent: review the plan before any file is touched |
| fabric | 254 named LLM patterns, pipe any text through them |
| n8n `:5678` | Visual workflow automation calling the local LLM |
| SearXNG `:8888` | Private web search, powers Continue's `@web` and the agent |
| Langfuse `:3001` | LLM observability: traces, latency, token counts |
| API `:8081/v1` | OpenAI-compatible inference endpoint, drop-in for any existing tool |
| Agent API `:8084` | `bob agent serve` — REST + SSE agent loop with per-client Bearer auth and owner-scoped sessions (loopback by default) |

## Hardware

Windows 11 with an NVIDIA RTX 3000 series card or newer. Three VRAM profiles are included:

| Profile | Target cards | Model download |
|---|---|---|
| `16gb` (default) | RTX 5080, 4090, 4080 | ~38 GB |
| `12gb` | RTX 4070 Ti, 3080 Ti, 4070 | ~21 GB |
| `8gb` | RTX 3070, 4060 (unvalidated) | ~12 GB |
| `24gb` | RTX 3090, 4090, 4080 (near-lossless quants) | ~42 GB |
| `32gb` | RTX 5090, A6000, 3090 Ti | ~54 GB |

Setup detects your GPU and selects the best-fit profile automatically. RTX 5000 (Blackwell) requires CUDA 12.8; `install_prereqs.bat` handles version selection. On an RTX 5080 with the default profile: pp512 ~4600 t/s, tg128 ~89 t/s.

## Supported matrix

What is actually tested, versus what is expected to work but is not in the per-PR gate. Kept honest by
the ND2 CI acceptance matrix — no "works everywhere" claims.

Legend: **✅ gated** = proven every PR by CI on hosted runners · **🟡 supported** = shipped and used, exercised by the release-tag GPU tier (not per-PR) · **❌ not yet** = unsupported.

| OS | CPU tier (no GPU, tiny model — wiring/correctness only) | NVIDIA GPU (CUDA, real inference) |
|---|---|---|
| **Windows 11** | ✅ gated (`acceptance-cpu`, every PR) | 🟡 supported — day-to-day driver; native-from-source CUDA build proven in the release-tag `acceptance-gpu` tier |
| **Linux** (glibc; apt/dnf/pacman) | ✅ gated (`acceptance-cpu`, every PR) | 🟡 supported — provisioner shipped (NC); native CUDA proven in the release-tag `acceptance-gpu` tier |
| **macOS** | ❌ not yet | ❌ not yet |
| **AMD / ROCm** | ❌ not yet | ❌ not yet |

The GPU rows use the VRAM profiles above (`16gb` default down to `8gb`, up to `32gb`); the CPU tier is a
single tiny model (`bob profile cpu`) that proves the provision → serve → agent-loop path without a GPU.
Per contract C7 the per-PR gate is the CPU/portable tier only, so a fragile native CUDA build can never
block a merge; native-from-source is verified when a release is tagged. See
[`versions.lock`](versions.lock) for the exact pinned, checksum-verified build each release ships.

## Quick start

Git, Scoop, and PowerShell 7 are required before running these. Everything else installs automatically.

**Step 1: install prerequisites (once per machine)**

```powershell
git clone --recurse-submodules <your-remote> C:\bob
cd C:\bob
.\install_prereqs.bat
```

Installs CUDA, Python 3.12, Go, Node.js, cmake, and Docker Desktop. If Docker Desktop was just installed, log out and back in before step 2.

**Step 2: build, configure, and start**

```powershell
.\setup.bat
bob up
```

Builds the inference engine and proxy from source, downloads models, wires VS Code and terminal clients, and starts Docker services. Open a new terminal after setup so the PATH update takes effect. `bob up` starts llama-swap (`:8080`), the LiteLLM proxy (`:8081`), and Open WebUI (`:3000`) in the background. Tail logs with `bob logs`.

**Step 3: register the agent scheduler (once):**
```powershell
bob agent install
```
Registers a Windows scheduled task that runs background agent tasks on cron schedules. Optional; skip if you won't use the agent.

Both scripts are safe to re-run if something fails partway through.

The server speaks the same chat completions protocol as OpenAI. Any tool already pointed at OpenAI works here unchanged by redirecting its base URL to `http://localhost:8081/v1`.

Flags for `setup.bat`: `-Profile 12gb` (smaller model set), `-SkipModels` (skip downloads), `-Launch` (start the stack when setup finishes).

## Docs

[DAY-IN-THE-LIFE](docs/DAY-IN-THE-LIFE.md): hands-on walkthrough of every feature structured as a working session. Start here after setup.

[SETUP](docs/SETUP.md): prerequisites, two-step install flow, build steps, verification.

[USAGE](docs/USAGE.md): full command reference, API details, agent system, client configuration, Docker services, customization.

[MEMORY](docs/MEMORY.md): the memory engine + persisted sessions — typed/owner/project-scoped store, blended recall, conflict-aware consolidation, `BOB.md` project files, the `bob memory` CLI, and every `memory.*` config key.

[MANUAL-INSTALL.md](docs/MANUAL-INSTALL.md): step-by-step for advanced users with exact cmake flags, venv creation, and Docker wiring.

[TUNING](docs/TUNING.md): per-model launch flags, VRAM sizing, performance checks, updating the engine.

[FALLBACKS](docs/FALLBACKS.md): alternatives and workarounds for failed builds or installs.
