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
| `bob voice` | Continuous voice loop: speak, Bob replies aloud. Whisper STT + piper TTS. |
| `bob describe <image>` · `bob screenshot` | Describe an image or the screen. `--pro` for cloud vision. |
| `bob clip <url>` | Fetch a page, summarise it, store it to memory. |
| `bob remember "…"` · `bob recall "…"` | Store or search memory (semantic + recency + importance). |
| `bob memory <cmd>` | Curate memory: `list`, `show`, `edit`, `pin`, `forget`, `export`. |
| `bob help` | The full command catalog. |

**Agent tools** run inside the loop (`bob agent` or the shell), not as `bob <verb>` commands: memory, web, git, file, shell, fabric, plus the plugins summarise, draft, search, play. List them with `bob tools` / `bob plugins`, or call one directly with `bob --run <tool> '{json}'`.

## Stack

| Tool | Role |
|---|---|
| Open WebUI `:3000` | Browser chat, RAG, image input, voice (wired in the admin panel) |
| Continue.dev | VS Code autocomplete, chat, `@web`, `@codebase`, `@filesystem` |
| Cline | VS Code agent: reads and writes files, runs commands |
| aider | Terminal coding agent: review the plan before any file is touched |
| fabric | 254 named LLM patterns, pipe any text through them |
| Web search | Built-in `ddgs` metasearch (in-process, no service, no Docker); Brave/Tavily optional via key |
| n8n `:5678` | Visual workflow automation. Native, opt-in (`bob services n8n start`) |
| SearXNG `:8888` | Private self-hosted meta-search. Opt-in Docker (`bob services searxng start`) |
| Langfuse `:3001` | LLM observability. Opt-in Docker; default trace sink is a local file (`bob traces`) |
| API `:8081/v1` | OpenAI-compatible inference endpoint, drop-in for any client |
| Agent API `:8084` | `bob agent serve`: REST + SSE agent loop, Bearer auth, owner-scoped sessions (loopback by default) |

## Hardware

Linux or Windows 11 with an NVIDIA RTX 3000 series card or newer, or the CPU tier with no GPU. VRAM profiles:

| Profile | Target cards | Model download |
|---|---|---|
| `16gb` (default) | RTX 5080, 4090, 4080 | ~38 GB |
| `12gb` | RTX 4070 Ti, 3080 Ti, 4070 | ~21 GB |
| `8gb` | RTX 3070, 4060 (unvalidated) | ~12 GB |
| `24gb` | RTX 3090, 4090, 4080 (near-lossless quants) | ~42 GB |
| `32gb` | RTX 5090, A6000, 3090 Ti | ~54 GB |

Setup detects your GPU and picks the best-fit profile. RTX 5000 (Blackwell) needs CUDA 12.8+; the installer handles version selection. On an RTX 5080 with the default profile: pp512 ~4600 t/s, tg128 ~89 t/s.

## Supported matrix

What CI proves, versus what ships but is not gated. **gated**: proven every PR on hosted runners. **supported**: shipped and used, exercised by the release-tag GPU tier. **not yet**: unsupported.

| OS | CPU tier (no GPU, tiny model) | NVIDIA GPU (CUDA) |
|---|---|---|
| **Windows 11** | gated (`acceptance-cpu`, every PR) | supported; native CUDA build proven in the release-tag `acceptance-gpu` tier |
| **Linux** (glibc; apt/dnf/pacman/zypper) | gated (`acceptance-cpu`, every PR) | supported; native CUDA proven in the release-tag `acceptance-gpu` tier |
| **macOS** | not yet | not yet |
| **AMD / ROCm** | not yet | not yet |

The per-PR gate runs only the CPU/portable tier (`bob profile cpu`), so a fragile CUDA build cannot block a merge; native-from-source is verified at release tags. See [`versions.lock`](versions.lock) for the pinned, checksum-verified build each release ships.

## Quick start

One command clones Bob with submodules, installs the toolchain (CUDA, Python 3.12, Go, Node.js, cmake), builds from source, downloads models, wires clients, and verifies against `versions.lock`. It is idempotent and needs only Git up front (it installs Git too). Add `--cpu` on a GPU-less box. macOS arrives in 2.0.

Linux:
```bash
curl -fsSL https://raw.githubusercontent.com/altpsyche/bob/main/install/install.sh | sh
```

Windows (PowerShell):
```powershell
irm https://raw.githubusercontent.com/altpsyche/bob/main/install/install.ps1 | iex
```

> The `https://get.bob.sh` short URLs are planned; use the `raw.githubusercontent.com` URLs today.

On Linux you are asked for `sudo` once (system packages); atomic Fedora (Bazzite/Silverblue) layers via `rpm-ostree`. Nothing needs Docker: web search is built in, and add-on services are opt-in.

<details>
<summary>Manual install</summary>

```bash
git clone --recurse-submodules https://github.com/altpsyche/bob.git bob
cd bob
./install_prereqs.sh    # add --cpu for a GPU-less box
./setup.sh              # GPU-less: ./setup.sh --cpu
```

Windows: run `install_prereqs.bat` then `setup.bat` (add `--cpu` for no GPU). Both entry scripts hand off to the Python kernel (`python -m bob.kernel`) and are safe to re-run.
</details>

Open a new terminal so `bob` resolves (Linux: `~/.local/bin`; Windows: the `bob.cmd` scoop shim, or add the repo to PATH). Then talk to Bob; inference auto-starts:

```bash
bob                             # interactive shell
bob chat "hi"                   # one-shot
bob agent "summarise README.md" # agentic task
```

`bob up` optionally pre-warms the endpoint (`:8080`) and LiteLLM proxy (`:8081`); `--with-webui` at setup adds Open WebUI (`:3000`). Any OpenAI client works by pointing its base URL at `http://localhost:8081/v1`.

`setup` flags: `--profile 12gb`, `--skip-models`, `--skip-voice`, `--cpu`, `--launch`. Run `bob agent install` once to register the background scheduler (Linux cron / Windows Scheduled Task).

## Docs

- [ROADMAP](ROADMAP.md): what Bob does today and where it is going next.
- [DAY-IN-THE-LIFE](docs/DAY-IN-THE-LIFE.md): hands-on walkthrough of every feature. Start here after setup.
- [SETUP](docs/SETUP.md): prerequisites, install flow, verification.
- [USAGE](docs/USAGE.md): full command reference, API, agent system, client config.
- [MEMORY](docs/MEMORY.md): the memory engine, blended recall, consolidation, `BOB.md` project files, and config keys.
- [MANUAL-INSTALL](docs/MANUAL-INSTALL.md): step-by-step build with exact cmake flags and venv creation.
- [TUNING](docs/TUNING.md): launch flags, VRAM sizing, performance checks, engine updates.
- [FALLBACKS](docs/FALLBACKS.md): alternatives for failed builds or installs.
