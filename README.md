# Bob Agent

**Bob: Your Private AI Assistant.**
Uses your GPU for local processing, with optional cloud connectivity (DeepSeek, OpenAI-compatible) for advanced capabilities. **local by default, cloud on demand.** Runs on Linux (Arch/CachyOS, Fedora, Ubuntu, and atomic Fedora like Bazzite) and Windows.

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
| Agent API `:8084` | `bob agent serve`: REST + SSE agent loop with per-client Bearer auth and owner-scoped sessions (loopback by default) |

## Hardware

Linux or Windows 11 with an NVIDIA RTX 3000 series card or newer (or run the CPU tier with no GPU). VRAM profiles included:

| Profile | Target cards | Model download |
|---|---|---|
| `16gb` (default) | RTX 5080, 4090, 4080 | ~38 GB |
| `12gb` | RTX 4070 Ti, 3080 Ti, 4070 | ~21 GB |
| `8gb` | RTX 3070, 4060 (unvalidated) | ~12 GB |
| `24gb` | RTX 3090, 4090, 4080 (near-lossless quants) | ~42 GB |
| `32gb` | RTX 5090, A6000, 3090 Ti | ~54 GB |

Setup detects your GPU and selects the best-fit profile automatically. RTX 5000 (Blackwell) requires CUDA 12.8+; the prerequisite installer handles version selection. On an RTX 5080 with the default profile: pp512 ~4600 t/s, tg128 ~89 t/s.

## Supported matrix

What is actually tested, versus what is expected to work but is not in the per-PR gate. Kept honest by
the ND2 CI acceptance matrix. No "works everywhere" claims.

Legend: **gated** means proven every PR by CI on hosted runners. **supported** means shipped and used, exercised by the release-tag GPU tier (not per-PR). **not yet** means unsupported.

| OS | CPU tier (no GPU, tiny model: wiring/correctness only) | NVIDIA GPU (CUDA, real inference) |
|---|---|---|
| **Windows 11** | gated (`acceptance-cpu`, every PR) | supported: day-to-day driver; native-from-source CUDA build proven in the release-tag `acceptance-gpu` tier |
| **Linux** (glibc; apt/dnf/pacman/zypper) | gated (`acceptance-cpu`, every PR) | supported: provisioner shipped (NC); native CUDA proven in the release-tag `acceptance-gpu` tier |
| **macOS** | not yet | not yet |
| **AMD / ROCm** | not yet | not yet |

The GPU rows use the VRAM profiles above (`16gb` default down to `8gb`, up to `32gb`); the CPU tier is a
single tiny model (`bob profile cpu`) that proves the provision → serve → agent-loop path without a GPU.
Per contract C7 the per-PR gate is the CPU/portable tier only, so a fragile native CUDA build can never
block a merge; native-from-source is verified when a release is tagged. See
[`versions.lock`](versions.lock) for the exact pinned, checksum-verified build each release ships.

## Quick start

Only **Git** is required up front. The two entry scripts install the rest of the toolchain (CUDA, Python 3.12, Go, Node.js, cmake) and build everything. Two thin shell stubs hand off to a Python cold-start kernel (`python -m bob.kernel`) — no PowerShell anywhere.

**Step 1: install prerequisites (once per machine)**

```bash
git clone --recurse-submodules <your-remote> bob
cd bob
./install_prereqs.sh            # Linux;  add --cpu for a GPU-less box
# Windows:  install_prereqs.bat
```

You're asked for your `sudo` password **once** (Linux). On atomic Fedora (Bazzite/Silverblue) it layers via `rpm-ostree` and points you at a Fedora distrobox — the recommended path there.

**Step 2: build, configure, and start**

```bash
./setup.sh                      # Windows:  setup.bat   ·   GPU-less:  ./setup.sh --cpu
```

Builds the inference engine and proxy from source, downloads models, wires VS Code and terminal clients, and (if Docker is present) starts the compose services. Open a new terminal afterward so the `~/.local/bin` PATH update takes effect.

Then just talk to Bob — **inference auto-starts on demand**, no separate command needed:

```bash
bob                             # interactive REPL (brings the stack up if it isn't)
bob chat "hi"                   # one-shot
bob agent "summarise README.md" # agentic task loop
```

`bob up` is an optional pre-warm (starts llama-swap `:8080` + the LiteLLM proxy `:8081` in the background; add `--with-services` for Docker, or opt into Open WebUI `:3000` at setup with `./setup.sh --with-webui`). Tail logs with `bob logs`.

**Step 3 (optional): register the agent scheduler**
```bash
bob agent install
```
Registers a recurring background-agent runner (Linux cron / Windows Scheduled Task) for scheduled goals. Skip if you won't use it.

Both scripts are safe to re-run if something fails partway through. `setup` needs **no root** — only `install_prereqs` (system packages) uses sudo, so if your sudo is finicky you can install the toolchain by hand and skip straight to `./setup.sh`.

The server speaks the same chat completions protocol as OpenAI. Any tool already pointed at OpenAI works here unchanged by redirecting its base URL to `http://localhost:8081/v1`.

Flags for `setup`: `--profile 12gb` (smaller model set), `--skip-models` (skip downloads), `--skip-voice`, `--cpu` (force the no-GPU tier), `--launch` (start the stack when setup finishes).

## Docs

[DAY-IN-THE-LIFE](docs/DAY-IN-THE-LIFE.md): hands-on walkthrough of every feature structured as a working session. Start here after setup.

[SETUP](docs/SETUP.md): prerequisites, two-step install flow, build steps, verification.

[USAGE](docs/USAGE.md): full command reference, API details, agent system, client configuration, Docker services, customization.

[MEMORY](docs/MEMORY.md): the memory engine + persisted sessions: typed/owner/project-scoped store, blended recall, conflict-aware consolidation, `BOB.md` project files, the `bob memory` CLI, and every `memory.*` config key.

[MANUAL-INSTALL.md](docs/MANUAL-INSTALL.md): step-by-step for advanced users with exact cmake flags, venv creation, and Docker wiring.

[TUNING](docs/TUNING.md): per-model launch flags, VRAM sizing, performance checks, updating the engine.

[FALLBACKS](docs/FALLBACKS.md): alternatives and workarounds for failed builds or installs.
