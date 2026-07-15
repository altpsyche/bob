<p align="center">
  <img src="bob.png" alt="Bob" width="480">
</p>

# Bob Agent

**Your private AI assistant.** Local inference on your GPU, with optional cloud models (DeepSeek, any OpenAI-compatible) on demand. Runs on Linux and Windows 11.

Bob chats, listens, speaks, sees, and runs an agent loop over your code, files, and tasks.

## Usage

Run `bob` with no arguments to open the interactive shell. Type a message to chat; `/agent <goal>` runs a task; `/voice`, `/model`, `/stop`, `/help` do the rest. Inference auto-starts the first time you talk.

Or run any capability directly, for quick questions, scripts, and pipes:

| Command | What it does |
|---|---|
| `bob chat "…"` | One-shot chat. `--think` deep reasoning, `--code` coding, `--pro` cloud. |
| `bob agent "goal"` | Agentic task loop: plans, uses tools, executes. Schedulable via cron. |
| `bob voice` | Continuous voice loop: speak, Bob replies aloud. faster-whisper STT + piper TTS. |
| `bob describe <image>` · `bob screenshot` | Describe an image or the screen. `--pro` for cloud vision. |
| `bob clip <url>` | Fetch a page, summarise it, store it to memory. |
| `bob remember "…"` · `bob recall "…"` | Store or search memory (semantic + recency + importance). |
| `bob memory <cmd>` | Curate memory: `list`, `show`, `edit`, `pin`, `forget`, `export`. |
| `bob help` | The full command catalog. |

**Agent tools** run inside the loop (`bob agent` or the shell), not as `bob <verb>` commands: memory, web, git, file, shell, fabric, plus the plugins summarise, draft, search, play. List them with `bob tools` / `bob plugins`, or call one directly with `bob --run <tool> '{json}'`.

## Stack

Core inference (the `:8081` API and the `bob` CLI) works out of the box. Everything else is optional:

| Tool | Status | Role |
|---|---|---|
| API `:8081/v1` | core | OpenAI-compatible inference endpoint, drop-in for any client |
| Agent API `:8084` | on demand (`bob agent serve`) | REST + SSE agent loop, Bearer auth, owner-scoped sessions (loopback) |
| Web search | built in | `ddgs` metasearch (in-process, no Docker); Brave/Tavily optional via key |
| Continue.dev | client | VS Code autocomplete, chat, `@web`, `@codebase`, `@filesystem` |
| Cline | client | VS Code agent: reads and writes files, runs commands |
| aider | client | terminal coding agent: review the plan before any file is touched |
| fabric | client | 254 named LLM patterns, pipe any text through them |
| Open WebUI `:3000` | opt-in at setup (`--with-webui`) | browser chat, RAG, image input, voice |
| n8n `:5678` | opt-in, native (`bob services n8n start`) | visual workflow automation |
| SearXNG `:8888` | opt-in, Docker (`bob services searxng start`) | private self-hosted meta-search |
| Langfuse `:3001` | opt-in, Docker (`bob services langfuse start`) | observability dashboard (default trace sink is a local file, `bob traces`) |

A default install has no Docker and starts none of these. The Docker-backed services (SearXNG, Langfuse) are not installed by default; starting one runs a guided Docker install first. Nothing else needs Docker.

## Hardware

Linux or Windows 11 with an NVIDIA RTX 3000-series card or newer (through Blackwell), or the CPU tier on a box with no GPU. The GPU engine is **driver-only**: it needs the NVIDIA driver, not the CUDA toolkit. VRAM profiles:

| Profile | Target cards | Model download |
|---|---|---|
| `16gb` (default) | RTX 5080, 4090, 4080 | ~38 GB |
| `12gb` | RTX 4070 Ti, 3080 Ti, 4070 | ~21 GB |
| `8gb` | RTX 3070, 4060 (unvalidated) | ~12 GB |
| `24gb` | RTX 3090, 4090, 4080 (near-lossless quants) | ~42 GB |
| `32gb` | RTX 5090, A6000, 3090 Ti | ~54 GB |

Setup detects your GPU and picks the best-fit profile. One engine covers every supported NVIDIA generation. On an RTX 5080 with the default profile: pp512 ~4600 t/s, tg128 ~89 t/s.

## Supported matrix

What CI proves, versus what ships but is not gated. **gated**: proven every PR on hosted runners. **supported**: shipped and used, exercised by the release-tag GPU tier. **not yet**: unsupported.

| OS | CPU tier (no GPU, tiny model) | NVIDIA GPU |
|---|---|---|
| **Windows 11** | gated (`acceptance-cpu`, every PR) | supported; driver-only prebuilt engine, source build available, proven in the release-tag `acceptance-gpu` tier |
| **Linux** (glibc; apt/dnf/pacman/zypper/rpm-ostree) | gated (`acceptance-cpu`, every PR) | supported; driver-only prebuilt engine, source build available, proven in the release-tag `acceptance-gpu` tier |
| **macOS** | not yet | not yet |
| **AMD / ROCm** | not yet | not yet |

The per-PR gate runs the CPU/portable tier (`bob profile cpu`), so a fragile GPU build cannot block a merge; the GPU path is verified at release tags. See [`versions.lock`](versions.lock) for the pinned, checksum-verified engines, submodules, and models each release ships.

## Quick start

One command clones Bob, installs a **prebuilt inference engine that needs only your NVIDIA driver** (no CUDA toolkit, nothing to compile), sets up the supporting tools (Python 3.12, Go, Node.js), downloads models, wires clients, and verifies everything against `versions.lock`. It is idempotent and needs only Git up front (it installs Git too). Add `--cpu` on a GPU-less box, or `--from-source` to build the engine from source instead of downloading it. macOS arrives in 2.0.

Linux:
```bash
curl -fsSL https://raw.githubusercontent.com/altpsyche/bob/main/install/install.sh | sh
```

Windows (PowerShell):
```powershell
irm https://raw.githubusercontent.com/altpsyche/bob/main/install/install.ps1 | iex
```

> The `https://get.bob.sh` short URLs are planned; use the `raw.githubusercontent.com` URLs today.

On Linux you are asked for `sudo` once (system packages). The driver-only engine runs across distros, including atomic Fedora (Bazzite/Silverblue), with no CUDA toolkit and no distrobox. Nothing needs Docker: web search is built in, and add-on services are opt-in.

<details>
<summary>Manual install</summary>

```bash
git clone --recurse-submodules https://github.com/altpsyche/bob.git bob
cd bob
./install_prereqs.sh    # --cpu for a GPU-less box; --from-source to also install the CUDA toolkit
./setup.sh              # GPU-less: --cpu   |   source engine: --from-source
```

Windows: run `install_prereqs.bat` then `setup.bat` (add `--cpu` for no GPU, `--from-source` to build the engine). Both entry scripts hand off to the Python kernel (`python -m bob.kernel`) and are safe to re-run.
</details>

Open a new terminal so `bob` resolves (Linux: `~/.local/bin`; Windows: the `bob.cmd` scoop shim, or add the repo to PATH). Then talk to Bob; inference auto-starts:

```bash
bob                             # interactive shell
bob chat "hi"                   # one-shot
bob agent "summarise README.md" # agentic task
```

`bob up` optionally pre-warms the endpoint (`:8080`) and LiteLLM proxy (`:8081`); `--with-webui` at setup adds Open WebUI (`:3000`). Any OpenAI client works by pointing its base URL at `http://localhost:8081/v1`.

`setup` flags: `--profile 12gb`, `--skip-models`, `--skip-voice`, `--cpu`, `--from-source`, `--launch`. Installs default to the **stable** channel (the latest release, with prebuilt engines); pass `--dev` to track the latest `main` and build from source. Run `bob agent install` once to register the background scheduler (Linux cron / Windows Scheduled Task).

## Docs

- [ROADMAP](ROADMAP.md): what Bob does today and where it is going next.
- [DAY-IN-THE-LIFE](docs/DAY-IN-THE-LIFE.md): hands-on walkthrough of every feature. Start here after setup.
- [SETUP](docs/SETUP.md): prerequisites, install flow, verification.
- [USAGE](docs/USAGE.md): full command reference, API, agent system, client config.
- [MEMORY](docs/MEMORY.md): the memory engine, blended recall, consolidation, `BOB.md` project files, and config keys.
- [MANUAL-INSTALL](docs/MANUAL-INSTALL.md): step-by-step build with exact cmake flags and venv creation.
- [TUNING](docs/TUNING.md): launch flags, VRAM sizing, performance checks, engine updates.
- [FALLBACKS](docs/FALLBACKS.md): alternatives for failed builds or installs.
