# USAGE

The full command and usage reference: interactive shell, one-shot verbs, the lifecycle commands that keep inference running, the OpenAI-compatible API, the agent system, client configuration, opt-in ecosystem services, and customization. For installation, see [SETUP.md](SETUP.md). For performance tuning and updating the engine, see [TUNING.md](TUNING.md).

> **New here?** [DAY-IN-THE-LIFE.md](DAY-IN-THE-LIFE.md) walks through every feature in one hands-on session. It's a better starting point than reading this document top to bottom.

Bob is Python-only and cross-OS. The same `bob <verb>` commands work identically on Linux and Windows. Only install, PATH, and shell-integration steps differ per OS; those are shown in paired blocks below.

## The one way to use Bob

Run **`bob`** with no arguments on a terminal. That opens the interactive shell, Bob's home base. Type a message to chat; slash-commands drive everything else. **Inference auto-starts the first time you talk**, so there is nothing to launch first.

```
bob
```

You can also run any capability directly, without opening the shell, for quick questions, scripts, and pipes. `bob help` prints the live catalog; the sections below cover each group.

| Command | What it does |
|---|---|
| `bob chat "…"` | One-shot chat (great for pipes). `--think` reasoning mode, `--code` coding, `--pro` cloud. |
| `bob agent "goal"` | Agentic task loop: plans, uses tools, executes steps. Schedulable via cron. |
| `bob voice` | Continuous voice loop: speak, Bob replies out loud. faster-whisper STT + piper TTS. |
| `bob describe <image>` · `bob screenshot` | Describe an image or the screen. `--pro` routes to cloud vision. |
| `bob clip <url>` | Fetch a page, summarise it, and store it to memory. |
| `bob remember "…"` · `bob recall "…"` | Store / search Bob's memory (blended semantic + recency + importance). |
| `bob memory <cmd>` | Inspect/curate memory: `list`, `show`, `edit`, `pin`, `forget`, `export`, `status`, `clear`. |
| `bob up` / `bob stop` / `bob status` | Bring the stack up in the background / stop it / see what's loaded. |
| `bob help` | The full command catalog. |

### Verbs vs. tools vs. plugins

Three easily confused things:

- **Verbs** are `bob <name>` commands (everything in `bob help`, sourced from `scripts/bob/registry.py`). `chat`, `agent`, `voice`, `up`, `setup` are verbs.
- **Agent tools** are what the agent loop calls *on your behalf*: memory, web, git, file, shell, fabric. They are not `bob <verb>` commands; they run inside `bob agent` / the shell. List them with `bob tools list`.
- **Plugins** are drop-in capabilities in `plugins/<name>/`: **summarise, draft, search, play**. They are *also not* `bob <verb>` commands. They run inside `bob agent` / the shell, or you can invoke one directly with `bob --run <name> '{json}'`. List them with `bob plugins list`.

> `summarise`, `draft`, `search`, and `play` are **not** `bob` verbs. Writing `bob summarise …` will not work. Use them through the agent (`bob agent "summarise README.md"`), inside the shell, or deterministically with `bob --run summarise '{"file": "README.md"}'`.

## The `bob` shell (default front door)

Run `bob` with no arguments on a terminal to open the interactive shell: a splash (header, model/agency/session, live tool/command/skill counts, endpoint health) and a prompt. (Piped or redirected `bob` prints help instead of opening the shell.)

In the shell:

| Input | Does |
|-------|------|
| *(type anything)* | an agent turn; Bob answers and can use tools (streamed, Markdown-rendered) |
| `/agent <goal>` | run the agent loop explicitly on a goal |
| `/voice` | drop into the spoken voice loop |
| `/model [model]` · `/think [on\|off]` · `/agency [show\|confirm\|silent]` | switch the served model (`coder`, `ponder`, `chat`, `vision`, `agent`, or a `-pro` peer) / toggle reasoning on the current model / tool-approval mode |
| `/tools` · `/skills` · `/help` | the catalog (grouped commands + tools + skills) |
| `/skill [name]` | list or run a skill (tool-sequence or sub-agent) |
| `/session new\|list\|resume <id>\|show` · `/status` · `/clear` | persisted sessions (`data/sessions.db`) + state; leaving a session consolidates it into memory |
| `/logs` · `/stop` | tail the server log / stop the stack |
| `/theme [reload]` | show/reload the theme ([config/ui.json](../config/ui.json)) |
| `/exit` | leave |

Type `/` to filter the command list. Gated tools (e.g. `shell_run`, or any tool under `/agency confirm`) show an inline **y/N/a** approval; **Ctrl-C** cancels the in-flight turn and returns to the prompt. Inference auto-starts on your first turn if the stack isn't already up.

**Two surfaces, one core.** The shell's `/commands` and the terminal's `bob <verb>` are not competing menus: use `bob <verb>` for scripting, cron, and SSH one-shots; use `/command` to drive the same thing from inside the cockpit. The lifecycle/cockpit commands live on **both**: `bob up`/`/up`, `bob stop`/`/stop`, `bob status`/`/status`, plus `restart`, `services`, `webui`, `logs`. Each is a thin front door over one shared core (e.g. [`scripts/tools/stack.py`](../scripts/tools/stack.py)), never a second implementation. Session-only state (`/model`, `/agency`, `/session`, `/theme`, `/clear`) is shell-only by design; provisioning and one-shot conversation (`chat`, `fetch`, `build`, `setup`, …) are terminal-only. From the terminal, `bob help` prints the same generated command catalog and, at the end, lists which commands are also `/commands` in the shell.

## One-shot chat: `chat`, `think`, `code`

For scripting, piping, and quick questions without entering the shell:

```
bob chat          # opens the routed REPL, multi-turn, empty line to exit
bob think         # same, with reasoning turned ON (the chat model thinks before answering)
bob code          # same but uses the coder (Qwen3-Coder-30B-A3B), code focus
```

`think` is a reasoning **mode**, not a model swap: `bob think` and `bob chat --think` keep the chat
model and turn its thinking on. For the bigger 30B reasoning model, pick it explicitly with
`/model ponder` in the shell (and `/think on`). Reasoning runs in the model's reasoning channel and
never enters the answer text or memory.

**Routing flags** (combine freely):

| Command | Model | Reasoning |
|---------|-------|-----------|
| `bob chat` | chat (local) | off |
| `bob chat --pro` | chat-pro (DeepSeek API) | off |
| `bob chat --think` / `bob think` | chat (local) | on |
| `bob chat --code` / `bob code` | coder (local) | off |
| `bob chat --code --pro` / `bob code --pro` | coder-pro (DeepSeek API) | off |

**Common flags** (for `chat`, and where noted `code`/`think`):

| Flag | Effect |
|------|--------|
| `--pro` | Route to the cloud (DeepSeek) peer for this role |
| `--think` | Turn reasoning on for this session (the model thinks before answering) |
| `--code` | (`chat` only) switch to the coder role |
| `--raw` | Emit plain text only, no spinner, no colour, no Markdown rendering (good for pipes) |
| `--max N` | Cap the response at N tokens |
| `--sys <text>` | Override the system prompt for this call (`chat` only) |

**One-shot mode** (prompt as argument, no interactive loop):

```
bob chat "explain what a semaphore is"
bob chat --pro "what is the fastest sort for nearly-sorted data?"
bob think "design a caching layer for this service"
bob code "write a function that retries a command N times"
bob chat "design a caching layer" --sys "Be concise." --max 1024
```

When output is piped, `bob chat` returns clean text (spinner and colour suppressed), so `bob chat … | bob speak` and similar pipelines work without stray control codes. `--raw` forces that mode explicitly.

## Keeping inference running

Inference gets served three ways; pick by how long you want it up:

| Command | What it does | Use when |
|---------|--------------|----------|
| **auto-start** (default) | The first `bob` shell turn or `bob chat`/`bob agent` call brings the stack up on demand if it isn't already reachable. | Normal interactive and one-shot use, you never think about it. |
| `bob serve` | Foreground stack (llama-swap `:8080` + LiteLLM `:8081`). Stays in your terminal, prints logs, stops with Ctrl-C. | You want to watch the logs, or run in a dedicated terminal/pane. |
| `bob up` | Background bring-up (endpoint + proxy, and Open WebUI if it's installed). Returns to your prompt. | You want inference to stay up for IDE/terminal tools and the API without a foreground process. |

`bob up` flags:

```
bob up                     # start endpoint + proxy in the background (opens WebUI if installed)
bob up --no-open           # don't open the browser
bob up --with-services     # also start the opt-in services group (Langfuse / SearXNG / n8n); off by default
```

Check and control what's running:

```
bob status    # which models are loaded in VRAM
bob ps        # daemon PIDs, RAM, and uptime
bob logs      # tail the server log (bob logs -n 100 for more lines)
bob restart   # stop then start the endpoint
bob stop      # stop all services and free VRAM
```

The endpoint logs go to `logs/llama-swap.log`; tail them live with `bob logs`. The server loads a model into VRAM on first request and unloads it after idle. The exceptions are `fim` (autocomplete) and `embed` (embeddings), which are pinned and never unloaded. Only one large model (`ponder`, `coder`, or `chat`) is resident at a time; switching between them takes a few seconds.

**mlock:** `fim` and `embed` are pinned in physical RAM with `--mlock`, preventing the OS from paging their weights to disk under memory pressure (e.g. simultaneous VS Code autocomplete, chat, and Open WebUI load). This locks roughly 4 GB of physical RAM permanently. On systems with less than 32 GB of RAM, disable it by overriding the `fim`/`embed` entries in `config/user.json` and re-running `bob gen`. Setting `mlockBig` on the swap-group models (ponder, coder, chat) extends mlock to their CPU-offloaded pages; on Windows this needs `SeLockMemoryPrivilege` (`bob mlock --grant` checks and grants it), on Linux you raise the memlock limit instead (`ulimit -l unlimited` or `/etc/security/limits.conf`).

**Start automatically at login (optional):**

Linux:
```bash
# add to crontab -e
@reboot cd /path/to/bob && ./bob up --no-open
```

Windows:
```bat
:: create a Task Scheduler task "At log on" that runs:
bob up --no-open
```

## Available models (16gb profile)

| Name | Role | Backing model |
|---|---|---|
| `ponder` | heavy reasoning and architecture | Qwen3-30B-A3B Q4 |
| `coder` | coding chat and agentic edits | Qwen3-Coder-30B-A3B Q4_K_M (MoE) |
| `chat` | general conversation | Qwen3-14B Q4_K_M |
| `fim` | autocomplete (pinned) | Qwen-Coder-3B Q8_0 |
| `embed` | RAG embeddings (pinned) | bge-m3 Q8 |
| `vision` | image description and visual Q&A | Qwen2-VL-7B Q4_K_M + mmproj |
| `agent` | local tool use and autonomous tasks | Hermes-3-Llama-3.1-8B Q5_K_M |

Every model's GGUF file, HuggingFace source, context size, and launch flags are defined once in [config/models.json](../config/models.json). The downloader and the runtime config both read from it. Clients reference the role names above (`coder`, `ponder`, etc.), so swapping the backing model for a role never requires touching any client configuration.

The `12gb` profile uses smaller variants (~21 GB on disk instead of ~38 GB). The `8gb` profile targets cards like the RTX 3070 and 4060 and is marked unvalidated. The `24gb` and `32gb` profiles ship near-lossless quants for bigger cards. Switch with `bob profile 12gb`, `bob profile auto` to detect from VRAM, or pass `--profile <name>` to setup before the first model download.

A `cpu` profile (a single tiny ~0.5 GB model) targets **no-GPU** boxes such as CI runners and dev laptops. It proves the serve → agent path works without a GPU (correctness and wiring, not performance); `bob profile auto` selects it when no GPU is detected, and `bob build --cpu` produces a CUDA-off engine to run it.

### Pro models (API-backed, no platform fee)

Additional model names are available via the LiteLLM proxy (`:8081`) when the corresponding API keys are set. They route **litellm → API provider directly**: no llama-swap hop, no OpenRouter markup.

| Name | Role | Provider | Backing model | Approx. cost |
|---|---|---|---|---|
| `chat-pro` | general conversation | DeepSeek | deepseek-v4-flash | ~$0.27/M in |
| `ponder-pro` | heavy reasoning | DeepSeek | deepseek-v4-pro | ~$0.55/M in |
| `coder-pro` | coding | DeepSeek | deepseek-v4-flash | ~$0.27/M in |
| `vision-pro` | cloud vision | DeepSeek | deepseek-v4-flash (vision-capable) | ~$0.27/M in |

**API keys**: all four pro roles route through DeepSeek by default, so only one key is needed. Set it in the environment:

Linux:
```bash
export DEEPSEEK_API_KEY='sk-...'   # platform.deepseek.com -> API keys
```

Windows:
```bat
setx DEEPSEEK_API_KEY "sk-..."     :: platform.deepseek.com -> API keys
```

Or store it via onboarding (it writes the key to `config/user.json`, gitignored). Pro models are only available through `:8081` (LiteLLM). Direct `:8080` requests return "model not found" because llama-swap only serves local models.

**Other coding peers (opt-in).** Two alternative cloud coders ship defined but disabled in `config/models.json`: **GLM-5.2** (z.ai, key `ZHIPU_API_KEY`) and **Kimi K2.7 Code** (Moonshot, key `MOONSHOT_API_KEY`). Enable one at a time (set its `enabled: true`, export its key, run `bob gen`); each provides `coder-pro`, so run a single coding peer to avoid a name clash. DeepSeek stays the enabled default.

**Override providers or models** in `config/user.json` under a `peers` block (see `config/user.json.example`). You can disable individual peers, change which model a role uses, or add OpenRouter as a fallback (5.5% platform fee applies). Run `bob gen` after any change.

**Bill control:** set a per-key spending limit on the provider dashboard as a hard stop independent of local config. Optionally add `budget`/`budgetPeriod` to the deepseek peer block in `user.json` for a LiteLLM-side cap. Run `bob gen` after any change.

## Calling the API directly

The endpoint speaks the OpenAI chat completions API, so any HTTP client works. Point any tool already configured for OpenAI at `http://localhost:8081/v1`.

Linux:
```bash
curl http://localhost:8081/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"coder","messages":[{"role":"user","content":"write fizzbuzz in rust"}]}'
```

Windows (Command Prompt):
```bat
curl http://localhost:8081/v1/chat/completions -H "Content-Type: application/json" -d "{\"model\":\"coder\",\"messages\":[{\"role\":\"user\",\"content\":\"write fizzbuzz in rust\"}]}"
```

Or use the built-in streaming CLI (identical on every OS):
```
bob chat --code "write fizzbuzz in rust"
bob chat --think "design a caching layer" --sys "Be concise." --max 1024
```

The LiteLLM proxy port defaults to `8081` (`ports.litellmPort` in `config/defaults.json`). The underlying llama-swap engine is on `8080` (`ports.port`), but clients should use `8081` for retry logic, Langfuse tracing, and pro-model access.

### Embeddings API

The `embed` model (bge-m3) exposes an embeddings endpoint:

Linux:
```bash
curl http://localhost:8081/v1/embeddings \
  -H "Content-Type: application/json" \
  -d '{"model":"embed","input":"The quick brown fox"}'
```

Windows (Command Prompt):
```bat
curl http://localhost:8081/v1/embeddings -H "Content-Type: application/json" -d "{\"model\":\"embed\",\"input\":\"The quick brown fox\"}"
```

Response shape:
```json
{
  "object": "list",
  "data": [{ "object": "embedding", "index": 0, "embedding": [0.023, -0.011, "..."] }],
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

Bob wires a persona, interactive chat, and memory on top of the inference stack. All of it is opt-in from any client's perspective: the raw API, Continue, aider, and every other client are unaffected.

### Persona config

Bob's persona `name`/`systemPrompt` and defaults live in the neutral `config/defaults.json` → `runtime.persona` (shared by both OSes; see [TUNING.md → System prompts](TUNING.md)). Override any key per-machine in `config/user.json` (JSON, gitignored). The override is the **runtime-config shape**: top-level keys deep-merged over the defaults (`persona`, `memory`, `agent`, `voice`, `vision`, `peers`, …), not wrapped in a `bob` section:

```json
{
  "persona": {
    "name": "Bob",
    "systemPrompt": "You are Bob..."
  }
}
```

Re-run `bob gen` after editing config. Config resolves the same way on every OS: `config/defaults.json` deep-merged with `config/user.json`.

### Memory

Bob stores and retrieves facts using SQLite + BGE-M3 embeddings. The `embed` model is already pinned in VRAM, so memory costs 0 extra VRAM and one embed call per store/recall. **Memory is on by default** (`runtime.memory.enabled = true`); disable it in `config/user.json` with `{"memory":{"enabled":false}}`.

This is a summary; the full reference (typed store, ranking, scoping, sessions, every config key) is in **[MEMORY.md](MEMORY.md)**.

**From the terminal:**
```
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

- At **session start** your stable profile (and any project [`BOB.md`](MEMORY.md#project-instruction-files)) is injected once.
- At **session end** (`/exit`, `/session new`, …) durable facts are **consolidated**: the model extracts typed facts and *supersedes* contradictions instead of accumulating them ("I use vim" → later "I switched to vscode" leaves only vscode).
- Set `memory.autoRecall = true` to also inject relevant memories on **every turn** (off by default; otherwise the agent recalls on demand via the `memory_recall` tool).

Injected memory is capped at `memory.maxInjectedTokens` so it cannot overflow the context window. Memory is always local: even with `--pro`, recall and embedding stay on BGE-M3 at `:8081`. Memory DB defaults to `data/bob.db` (gitignored); override with `memory.dbPath`.

**Optional context-engineering upgrades** (all off by default, see [MEMORY.md](MEMORY.md)):

- **Cross-encoder rerank** (`memory.rerank`): a second-stage reranker sharpens recall relevance and filters noise (needs a local `reranking` model in the stack).
- **Core-memory blocks** (`memory.coreBlocks`): named, always-injected notes the agent curates for itself with the `memory_block` tool (MemGPT/Letta style).
- **Conversation paging** (`agent.conversationPaging`): persists the full transcript so the agent can `conversation_search` and page back turns that compaction dropped.

### First run: onboarding

Setup runs an interactive onboarding flow at the end when `config/user.json` has no `bob` section yet:

```
Bob: Hi. What's your name?
> Siva
Bob: What kind of work do you do most?
> Game dev and AI tooling
Bob: Got a DeepSeek API key? (Enter to skip)
> sk-...
Bob: Ready. Type 'bob' to start.
```

This writes your name and work context to `data/bob.db` (profile table) and your API key to `config/user.json` (gitignored).

### Budget tracking

```
bob budget    # shows LiteLLM spend (if proxy is running) + configured caps
```

Shows the configured `max_budget`/`budget_duration`, queries the LiteLLM proxy for spend data if it's running, and reports memory DB size at $0 cost (fully local). For a detailed per-request cost breakdown, enable Langfuse tracing (see the [observability section](#observability-file-traces-and-langfuse)).

## Voice

Voice adds two-way audio to the terminal using faster-whisper (STT) and piper (TTS). All processing is local: no cloud, no microphone data leaving the machine. Voice is **enabled by default** (`runtime.voice.enabled = true`); you only download the models once. (whisper.cpp is a built-in fallback backend, selected by `voice.sttEngine = 'whisper.cpp'`.)

**One-time model download:**
```
bob setup-voice
```
Downloads the faster-whisper STT model, the piper voice, and the Qwen2-VL mmproj file, and installs the STT Python deps. `bob up` auto-starts the STT server on port 8082. On an NVIDIA GPU, setup also installs the CUDA-12 runtime libs (cuBLAS/cuDNN) so STT runs on the GPU; otherwise, or if those libs are missing at runtime, the server falls back to CPU int8 automatically (fast enough for single-utterance voice). (Only when `voice.sttEngine = 'whisper.cpp'` does setup build `whisper-server` and fetch the ggml model instead.)

**Commands:**
```
bob listen                          # record mic until silence -> print transcript
bob transcribe path/to/audio.wav    # transcribe a file instead of recording
bob speak "Hello, I am Bob."        # synthesise and play (piper TTS)
echo "some text" | bob speak        # pipe stdin to TTS
bob voice                           # continuous loop: listen -> chat -> speak (Ctrl-C to stop)
bob voice --pro                     # same loop but routes chat to cloud (DeepSeek API)
bob voice --agent                   # route each turn through the full agent tool loop
```

**Pipeline use:**
```
bob listen | bob chat | bob speak   # one-shot voice turn
```
`bob chat` returns clean text when piped (spinner and colour suppressed), so `bob speak` receives plain text.

**Whisper / piper server management:**
```
bob whisper start|stop|status       # STT server, faster-whisper by default (port 8082)
bob piper start|stop|status         # piper TTS HTTP server (:8083, OpenAI /v1/audio/speech)
bob ps                              # shows whisper and piper rows alongside other services
bob status                          # includes whisper and piper UP/down lines
```

Wire piper into Open WebUI TTS: Admin Panel → Audio → Text-to-Speech Engine → `http://localhost:8083`.
Wire whisper into Open WebUI STT: Admin Panel → Audio → Speech-to-Text Engine → `http://localhost:8082`
(verify with `curl -X POST http://localhost:8082/v1/audio/transcriptions -F "file=@test.wav" -F "model=whisper-1"`).

**Pipeline examples:**
```
bob listen | bob chat | bob speak               # one-shot voice turn
bob listen | bob chat --pro | bob speak         # voice turn routed to cloud
cat article.txt | fabric --pattern extract_wisdom | bob speak   # read fabric output aloud
```

**Audio quality tips:**
- Use headphones to prevent the mic picking up speaker output.
- An energy gate silences blank audio before it reaches whisper.
- The default STT model handles accented English and non-English languages. For higher accuracy set `sttModel = 'medium'` in `config/user.json` under `bob.voice` and re-run `bob setup-voice`.

**Voice response tuning** (all in `config/user.json` under `bob.voice`):

| Key | Default | Effect |
|-----|---------|--------|
| `maxTokens` | `512` | Caps the voice reply length. Lower (e.g. `256`) for faster one-liners; raise if Bob cuts off. |
| `silenceSec` | `1.5` | Seconds of mic silence before recording stops. Raise if Bob cuts off while you're still speaking. |
| `systemPrompt` | *(voice-specific)* | The system prompt used only in `bob voice`; instructs the model to reply in plain spoken sentences with no markdown. |
| `sttModel` | `'small'` | STT model size (faster-whisper CT2): `tiny`, `base`, `small`, `medium`, `large-v3`. Larger = more accurate, slower. Re-run `bob setup-voice` after changing. |
| `sttEngine` | `'faster-whisper'` | STT backend: `faster-whisper` (default) or `whisper.cpp` (fallback). |
| `sttComputeType` | `'auto'` | faster-whisper compute type: `auto` (float16 on GPU, int8 on CPU), or a CT2 type. |

The voice loop sanitises text before sending it to piper: it strips markdown symbols so stray markdown from the model never reaches the TTS engine. Combined with the voice system prompt, Bob replies in natural spoken language without reading punctuation aloud.

## Vision

Vision uses Qwen2-VL-7B (a ~5 GB GGUF + a ~1.5 GB mmproj) to describe images and answer visual questions. The model loads on demand and unloads after 30 s idle to free VRAM. Vision is **enabled by default** (`runtime.vision.enabled = true`).

**Setup:** `bob setup-voice` downloads the mmproj; the GGUF itself downloads via `bob fetch` (it's part of the 16gb profile).

**Commands:**
```
bob describe path/to/image.png
bob describe path/to/image.png "What text is visible?"
bob describe path/to/image.png --pro "Analyse this diagram in detail"   # DeepSeek V4 vision
bob screenshot
bob screenshot "What application is open and what does it show?"
bob screenshot --pro "Explain the code on screen"                       # cloud vision
```

`--pro` routes to DeepSeek V4 (deepseek-v4-flash), which supports vision input, using the existing `DEEPSEEK_API_KEY`. Useful when local Qwen2-VL output is insufficient or the image needs stronger OCR/reasoning.

`bob describe` resizes the image to max 1024 px on the longest edge before encoding. `bob screenshot` captures the primary display, saves a temp PNG, describes it, then deletes the PNG.

**Pipeline examples:**
```
bob screenshot | fabric --pattern analyze_claims    # screenshot -> vision -> fabric analysis
bob describe img.png | fabric --pattern summarize   # describe image, pipe to fabric
```

## Agent

The agent runs a local model in a loop: it reasons about which tools to call, executes them, and iterates until it has a final answer. All processing is local; tools include web search (in-process `ddgs` metasearch, no service or Docker needed), git, file access, shell commands, and memory.

### Running a goal

```
bob agent "what did I commit today and what files changed?"
bob agent "search the web for the latest Unreal Engine 5 release notes and summarise them"
bob agent "check git status, find any TODO comments in modified files, and list them"
```

The agent prints tool calls and results to stderr, then the final answer to stdout. Redirect stderr to suppress the trace:

Linux:
```bash
bob agent "summarise the last 10 commits" 2>/dev/null
```

Windows (Command Prompt):
```bat
bob agent "summarise the last 10 commits" 2>nul
```

**Agency modes** (how much the agent asks before acting):

| Mode | Behaviour | Use when |
|------|-----------|----------|
| `show` (default) | Prints tool calls + results, runs automatically | Normal interactive use |
| `confirm` | Prompts before each tool execution | Untrusted goals or destructive tools |
| `silent` | No output during execution; only the final answer | Scheduler, scripts, piped output |

Override for a single run with `--agency confirm`; set the default in `config/user.json` under `bob.agent.agency`. `runtime.agent.maxSteps` (default 10) caps the tool iterations per goal.

### Available tools

| Tool | What it does | Needs |
|------|-------------|-------|
| `memory` | Store and recall facts from Bob's memory DB | `embed` model running |
| `web` | Search the web, fetch URLs | nothing (in-process `ddgs`; optional providers below) |
| `git` | `git_status`, `git_log`, `git_diff` on any repo | git on PATH |
| `file` | `file_read` (within allowed paths), `file_write` (disabled by default) | `allowedReadPaths` set |
| `shell` | Run shell commands (always prompts the user, ignores agency mode) | Interactive terminal |
| `fabric` | Run any fabric pattern on text input | fabric on PATH |

The `web` tool searches through the in-process `ddgs` metasearch provider by default (pure-Python, aggregates DuckDuckGo, Bing, and others). It needs no service, no daemon, and no Docker, and behaves identically on every OS. Optional providers: Brave or Tavily via an API key (`agent.searchProvider`), or the opt-in `searxng` Docker service. All providers fall back to `ddgs`, then to a last-ditch DuckDuckGo HTML scrape. Config keys: `agent.searchProvider` (default `ddgs`) and `agent.webSearchFallback` (default true).

The drop-in **plugins** (summarise, draft, search, play) are also callable by the agent. Tools and plugins are **auto-discovered** from `scripts/tools/*.py` and `plugins/*/tool.py`; dropping in a file is the only registration step, there is no allowlist. To exclude one without deleting it, add its name to `agent.disabledTools` (a denylist) in config.

```
bob tools list         # every discovered tool + enabled/disabled status
bob tools test <name>  # run a tool's self-test (e.g. bob tools test git)
bob tools info <name>  # show a tool's full JSON schema
bob plugins list       # every installed plugin with type + description
```

### Scheduling background goals

```
# Add a daily git summary at 09:00
bob agent schedule add morning-summary --cron "0 9 * * *" --goal "check git log for today's work and write a one-paragraph summary to data/daily-summary.txt"

bob agent schedule list                     # list schedules with next-run times
bob agent schedule run morning-summary      # run now (ignores cron)
bob agent schedule disable morning-summary  # disable without removing
bob agent schedule enable morning-summary
bob agent schedule remove morning-summary   # remove permanently
```

Schedules are stored in `data/schedules.json`. A recurring task (a Windows Scheduled Task or a Linux cron entry) registered by `bob agent install` fires the runner every minute, checks which entries are due (5-field cron, 60 s double-fire guard), and runs them with `agency = 'silent'`. `bob agent status` shows the task state and recent log; `bob agent log` tails the agent log live (`-f` to follow). Remove the recurring task with `bob agent uninstall`. If `notify = true` is set on an entry, a desktop notification fires with the result (a Windows toast, or `notify-send` on Linux).

### Memory clip

```
bob clip https://example.com/article
bob clip https://example.com/article --note "read for the caching section"
```

One-shot web clip: fetches the URL, strips HTML, sends it to the chat model for a 3-to-5-sentence summary, prints it, then stores `url: summary` to Bob's memory DB. No agent loop, one LLM call, fast.

### Tool-calling format

The default agent model (Hermes 3) uses its own tool-calling format: tool schemas are injected into the system prompt and the model responds with `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` XML. Bob's agent loop handles this transparently. OpenAI-format models (Qwen3 and others) also work by setting `agent.toolFormat = 'openai'` in config.

### HTTP server (REST + SSE)

```
bob agent serve            # binds agent.serveHost:agent.agentPort (default 127.0.0.1:8084)
```

Exposes the agent loop over HTTP for n8n / WebUI / other clients. Every endpoint except `/health` requires `Authorization: Bearer <token>` (the litellm key or an `agent.apiTokens` entry). Each token maps to an owner, and sessions are owner-scoped: a token sees only sessions its owner created. Supports one-shot `POST /v1/agent/completions`, token-streaming `POST /v1/agent/completions/stream` (SSE; cancels on client disconnect), and multi-turn `POST/GET/DELETE /v1/sessions`. Full endpoint contract, event schema, and n8n wiring: [AGENT-SERVER.md](AGENT-SERVER.md).

Bob's tools are also exposed over MCP (stdio) with `bob agent mcp`, for MCP-aware clients.

### Check agent health

```
bob setup check     # dependency + registration checks  (alias of `bob doctor --quick`)
bob doctor          # the above, plus runtime: endpoint reachable, GPU/VRAM, writable dirs, config parses
bob diagnose        # GPU, VRAM, CUDA, and model-file health check
```

`bob setup check` (equivalently `bob doctor --quick`) verifies agent dependencies in order (venv, Python packages, config, tools directory, schedules file, fabric, SearXNG, n8n, LiteLLM proxy, BobAgent task, agent model file, tool loading honoring `agent.disabledTools`) and prints a fix command for each failure. `bob doctor` runs all of those plus a runtime pre-flight; run it first when something's off. Both are one core (`health.health_check`) behind a depth flag.

## Deterministic invocation: `bob --run`

For scripts and CI, `bob --run <tool> '{json}'` runs exactly one capability through the real agent dispatch, no model, no reasoning loop, just the tool:

```
bob --run summarise '{"file": "README.md", "length": "short"}'
bob --run search '{"query": "TODO", "path": "src/"}'
bob --run git '{"op": "status"}'
```

This is the plumbing consumers (and outside-terminal clients) use to invoke a single tool deterministically. List available tools with `bob tools list` and plugins with `bob plugins list`.

## Skills

A skill is a named tool-sequence or sub-agent: a reusable multi-step routine.

```
bob skill              # list available skills
bob skill <name>       # run a skill
bob skill <name> --show # show a skill's definition without running it
```

In the shell, `/skills` lists them and `/skill <name>` runs one.

## Reasoning mode (`/think`)

The Qwen3 models (`chat`, `ponder`) can reason before answering. In Bob this is a **mode** on whatever
model is active, not a separate model: toggle it with `/think on|off` in the shell, `bob think` /
`bob chat --think` from the terminal, or the `agent.think` config default (off). It applies to whichever
role is current, so your `chat` model can reason without switching to `ponder`.

When on, the model reasons internally first. Bob passes the `enable_thinking` chat-template kwarg to
llama-server, so the reasoning trace lands in the model's separate reasoning channel and **never enters
the answer text, the transcript, or memory** (no `/no_think` string is injected into your message).

Reasoning has two costs:

- **Consumes output tokens.** The reasoning counts toward the token budget. If you cap output with
  `--max`, set it high enough (2000+, or 8192 for deep planning) or the reasoning can crowd out the
  answer.
- **Increases first-token latency.** Reasoning runs before any visible output. For quick questions,
  leave `/think` off.

### When to use it

| Mode | When | `--max` guidance |
|------|------|------------------|
| `/think off` (default) | Quick Q&A, simple edits, conversation | 128 to 512 |
| `/think on` | Complex reasoning, architecture, planning | 2000 to 8192 |

For the bigger 30B reasoning model, switch to it explicitly with `/model ponder` (and turn `/think on`).

**External clients (Continue.dev, aider):** they call the proxy directly and bypass Bob's `/think`, so
they get the model's native default (Qwen3 reasons by default). To suppress reasoning there, append
`/no_think` to a message; the Qwen3 chat template honors it.

## Function Calling (Tool Use)

The `coder` model supports OpenAI-style function calling. Define functions the model can request, then execute them in your app:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key="sk-local")

tools = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read the contents of a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path to the file"}},
            "required": ["path"],
        },
    },
}]

resp = client.chat.completions.create(
    model="coder",
    messages=[{"role": "user", "content": "What is in the file README.md?"}],
    tools=tools,
    tool_choice="auto",
)

choice = resp.choices[0]
if choice.finish_reason == "tool_calls":
    for tc in choice.message.tool_calls:
        print("Model calls:", tc.function.name, tc.function.arguments)
        # Execute the function, add the result to messages, continue the conversation...
```

**Supported:** `coder` (Qwen3-Coder-30B-A3B, tuned for agentic tool use). **Not supported:** `ponder`, `chat`. Use `coder` for agentic tasks. In Cline, point at `coder` for best results; in aider, tool use is handled internally.

## Clients

Client configs (Continue, aider) are linked into your home directory during setup, so both tools work with no in-app configuration. Without symlink privileges, setup copies the files instead; re-run setup after editing the repo configs to sync the copies.

### VS Code: Continue.dev (autocomplete and chat)

Continue.dev provides inline autocomplete and a chat panel inside VS Code. Setup links the repo's config into `~/.continue/config.yaml`, so all models are wired with no in-editor setup.

Install the **Continue** extension from the VS Code Marketplace, then start the endpoint (`bob up`, or talk to Bob and it auto-starts). Open the Continue panel (`Ctrl+L`) and the `coder` and `ponder` models appear immediately.

**How models map to Continue roles:**

| Continue role | Model | Purpose |
|---|---|---|
| Chat, edit, apply | `coder` (Qwen3-Coder-30B-A3B) | default coding chat and inline edits |
| Chat, edit | `ponder` (Qwen3-30B-A3B) | architecture discussion and heavy reasoning |
| Chat | `chat` (Qwen3-14B) | general conversation; thinking off by default |
| Chat, edit | `chat-pro` (DeepSeek V4, API) | general conversation via API |
| Chat, edit, apply | `coder-pro` (DeepSeek V4, API) | coding via API |
| Chat | `ponder-pro` (DeepSeek R1, API) | heavy reasoning via API |
| Chat | `vision` (Qwen2-VL-7B, local) | image description and visual Q&A |
| Autocomplete | `fim` (Qwen-Coder-3B, pinned) | as-you-type ghost text completions |
| Embed | `embed` (bge-m3, pinned) | `@codebase` and `@docs` RAG indexing |

System prompts are set per-model and synced to clients by `bob gen`. `Ctrl+L` opens a new chat with any selected code attached; `Ctrl+I` opens an inline edit and shows a diff to accept or reject. Autocomplete fires as ghost text; `Tab` accepts. Use the model dropdown to switch roles.

Context is 32768 tokens for all models except `ponder` (16384). The first message to a large model is slower while it loads into VRAM; `fim` and `embed` stay pinned, so autocomplete and RAG never trigger a reload.

#### Continue.dev MCP Servers

Four MCP servers are wired into Continue automatically, activating as context providers in the Continue chat panel:

| Server | How to invoke | What it does |
|--------|--------------|-------------|
| `filesystem` | `@filesystem` then a path | Read files within a configured whitelist (paths outside return permission denied) |
| `fetch` | `@url https://...` | Fetch any URL and include its text as context |
| `github` | `@github` then a query | Search GitHub issues, PRs, and code |
| `searxng-search` | `@web` then a query | Private web search via the opt-in SearXNG service |

**Prerequisites:**
- `filesystem`, `github` require Node.js (installed by the prerequisite installer).
- `fetch` requires uv / `uvx` (installed by the prerequisite installer).
- `github` requires `GITHUB_TOKEN` set as an environment variable (a classic PAT with `repo` scope). Without it, `@github` queries return auth errors.
- `searxng-search` points at SearXNG specifically, so `@web` needs the opt-in SearXNG service running (`bob services searxng start`). If SearXNG is not running, `@web` queries return nothing silently. (This is distinct from the agent and CLI `web` tool, which uses in-process `ddgs` and needs no service.)

If a server fails to load, Continue shows a warning badge on its name; click it to see the error. Most failures are a missing `node`, `uvx`, or `GITHUB_TOKEN`.

### VS Code: Cline (agentic)

Cline is a more autonomous agent that reads and writes files, runs commands, and works across many turns. It is not auto-wired; configure it once in its settings panel.

Install the **Cline** extension, start the endpoint, then set the API provider to `OpenAI Compatible`:

| Field | Value |
|---|---|
| Base URL | `http://localhost:8081/v1` (replace `8081` if you changed `ports.litellmPort`) |
| API Key | `sk-local` (any non-empty string; the server ignores it) |
| Model ID | `coder` |

Set the context window to `16384` to match the server's limit. Leave image support off; these models are not multimodal in Cline. To split planning and editing, enable **Use different models for Plan and Act** and set Plan = `ponder`, Act = `coder` (switching evicts the other model from VRAM, so expect a brief load pause).

### Terminal: aider (plan and edit separately)

Aider has a genuine planning-versus-editing split: `ponder` (Qwen3-30B) drafts the change, `coder` (Qwen3-Coder-30B-A3B) turns it into file edits. You review the plan before any edit lands. Setup links the aider config (`config/aider/.aider.conf.yml`) into your home directory. Then:

```
cd <your-project>
bob aider
```

The config sets `architect: true` (request → `ponder` first) and `auto-accept-architect: false` (you see the plan and press Enter to apply, or refine first). Each turn triggers a VRAM swap between `ponder` and `coder`.

Useful in-session commands:

| Command | What it does |
|---|---|
| `/add <file>` | add a file to the editable context |
| `/read <file>` | add a file as read-only reference |
| `/ask <question>` | ask without triggering any edits |
| `/diff` | show pending changes |
| `/undo` | revert aider's last committed edit |
| `/drop` | remove files from context when it gets large |

aider auto-commits each accepted edit to git; work on a branch so `/undo` can roll back cleanly. Both models use a 16k context window. The `openai/` prefix in the config (`openai/ponder`, `openai/coder`) is required to route through a local endpoint and is already set.

## Shell AI Patterns: fabric

fabric transforms piped text through a named prompt pattern: a structured prompt with a specific output format baked in. Where `bob chat` is a blank canvas, fabric patterns encode the *format* of the answer (commit message, executive summary, code-review checklist). Patterns live in `~/.config/fabric/patterns/`, each a directory with a `system.md`.

It ships as a Go binary built from the `external/fabric` submodule. Run `bob fabric-setup` once to build and configure it (it builds the `fabric` binary from `external/fabric/cmd/fabric/` and copies the 254 patterns into `~/.config/fabric/patterns/`). Then pipe any text:

```
git diff --staged | fabric --pattern write_git_commit   # commit message from staged diff
cat notes.txt     | fabric --pattern summarize          # summarize a document or log
cat error.log     | fabric --pattern explain            # explain an error
cat myfile.py     | fabric --pattern code_review        # code review
cat meeting.txt   | fabric --pattern extract_wisdom     # action items from meeting notes
fabric -l                                               # list all 254 patterns
```

fabric uses the `coder` model by default; pass `--model ponder` for complex analysis. To update patterns after a submodule bump, re-run `bob fabric-setup` (patterns re-copied; the binary rebuilds only if missing, delete it first to force a rebuild).

## Ecosystem Services

### LiteLLM proxy

LiteLLM sits between clients and llama-swap, adding retry logic and structured request logging.

```
bob litellm          # start the proxy on :8081 in the background (PID tracked)
bob litellm status   # show PID and uptime
bob litellm stop     # stop the background proxy
```

All clients (Continue, aider, Cline, fabric, Open WebUI, `bob chat`) use `:8081` by default. The proxy exposes all local model names (`coder`, `ponder`, `chat`, `fim`, `embed`) plus the pro model names (`chat-pro`, `ponder-pro`, `coder-pro`, `vision-pro`) when API keys are set. Direct `:8080` access to llama-swap still works for local models but bypasses retry logic and Langfuse tracing.

`config/litellm.yaml` is generated automatically by `bob gen` and `bob serve`; do not edit it by hand.

### Opt-in services (Langfuse, SearXNG, n8n)

A default install is 100% Docker-free: setup starts none of these services. They are all opt-in, brought up on demand.

- **n8n** runs **native** on the Node toolchain, not in a container: `bob services n8n start`.
- **SearXNG** is a **Docker** opt-in: `bob services searxng start`. If Docker is missing, this runs a guided install through the system package manager first. Port 8888.
- **Langfuse** is a **Docker** opt-in: `bob services langfuse start`. Same guided Docker install if needed. Port 3001.

GPU tools (llama.cpp, Open WebUI) stay native for performance.

```
bob services start    # start the opt-in services group, prints state table
bob services stop     # stop the services (data is preserved)
bob services status   # names, state, and uptime
bob services logs     # tail all service logs (Ctrl-C to stop)
```

Docker must be running before starting a Docker service (SearXNG or Langfuse). Override ports or timezone in `config/user.json` under `defaults`:

```json
{ "defaults": { "langfusePort": 3001, "searxngPort": 8888, "n8nPort": 5678, "n8nTimezone": "America/New_York" } }
```

After changing any of these, re-run `bob services start` to regenerate `.env` and restart containers.

**Persistent data** lives in gitignored directories under `tools/`:
- `tools/langfuse-data/`: Postgres DB with all Langfuse traces, projects, and API keys
- `tools/n8n-data/`: n8n workflows, credentials, and execution history

These survive `bob services stop`/`start`. `docker system prune -af` deletes them: back them up if you have valuable history.

---

#### Observability: file traces and Langfuse

The default trace sink is a local **file sink** (Docker-free): traces write to `logs/traces/<trace_id>.jsonl` and you read them with `bob traces` (`bob traces list`, `bob traces show <id>`). `agent.tracing` gates tracing (default off); `agent.tracingSink` is `file` (default) or `otlp`, where `otlp` exports to `agent.otlpEndpoint`.

**Langfuse** is an **opt-in** upgrade over the built-in file traces: a full dashboard for the same data. Start it with `bob services langfuse start`, then open `http://localhost:3001`. Default login: `admin@local.dev` / `admin123`.

Langfuse records every bob request routed through LiteLLM: full prompt, response, model name, latency, token counts, and retry events. Use it to debug unexpected answers, compare quant levels (run `bob eval` before/after a profile switch), audit agentic tool calls, and track token burn.

**Enabling Langfuse tracing** (opt-in; Langfuse doesn't auto-capture, and only requests through LiteLLM are visible):

1. Start the Langfuse service: `bob services langfuse start`
2. Open `http://localhost:3001` → **Settings → API Keys** → create a key pair; copy the **Public** and **Secret** keys.
3. Set the keys as environment variables:

   Linux:
   ```bash
   export LANGFUSE_PUBLIC_KEY='pk-lf-...'
   export LANGFUSE_SECRET_KEY='sk-lf-...'
   ```
   Windows:
   ```bat
   setx LANGFUSE_PUBLIC_KEY "pk-lf-..."
   setx LANGFUSE_SECRET_KEY "sk-lf-..."
   ```
4. Enable Langfuse callbacks in `config/user.json`:
   ```json
   { "defaults": { "langfuseEnabled": true } }
   ```
5. Regenerate the proxy config and restart it:
   ```
   bob gen
   bob litellm stop
   bob litellm
   ```
6. Point your client at `:8081` (or use `bob chat`, which goes through LiteLLM automatically).
7. Requests appear in the Langfuse dashboard under **Traces** within a few seconds.

The steps above route LiteLLM request logs to Langfuse. To also export the **agent's** own traces (the file-sink data) to Langfuse instead of local files, set `agent.tracingSink` to `otlp`, point `agent.otlpEndpoint` at the Langfuse OTLP endpoint, then run `bob gen`.

> `config/litellm.yaml` is regenerated on every `bob gen` and `bob serve`; do not edit it directly. Use `config/user.json` for all persistent customization.

---

#### SearXNG: private web search

Open `http://localhost:8888` for a search UI. Queries fan out to Google, Bing, DuckDuckGo, and others; SearXNG aggregates the results. Your IP talks to SearXNG locally, and SearXNG talks to providers on your behalf.

**Using `@web` in Continue.dev:** with the SearXNG service running (`bob services searxng start`), the `searxng-search` MCP server is active. In any Continue chat, prefix a query:

```
@web what is the latest llama.cpp release?
@web python asyncio best practices 2025
```

If SearXNG is not running, `@web` returns nothing silently; start it first. (The agent and CLI `web` tool does not depend on SearXNG: it uses in-process `ddgs`.)

**As a browser search engine:** browser settings → Search engines → Add: Name `local`, URL `http://localhost:8888/search?q=%s`, shortcut `s`. Then type `s <query>` in the address bar.

Config lives at `config/searxng/settings.yml` (committed; edit to enable/disable engines or change safe-search level).

---

#### n8n: workflow automation

Open `http://localhost:5678`. No login required on first run; set up an account on first visit (credentials stay local in `tools/n8n-data/`).

n8n is a visual workflow builder: each workflow is a graph of trigger nodes (webhook, schedule, file watch) connected to action nodes (HTTP request, email, code).

**Connecting to the local LLM:** n8n runs native, so the host LLM is reachable at `http://localhost:8081`. Add an **HTTP Request** node:
- Method: `POST`
- URL: `http://localhost:8081/v1/chat/completions`
- Header: `Authorization: Bearer sk-local` (any non-empty string)
- Body (JSON):
  ```json
  { "model": "coder", "messages": [{ "role": "user", "content": "{{ $json.text }}" }] }
  ```

The response is `choices[0].message.content`; wire that to whatever you want. Prefer the LiteLLM proxy at `:8081` over the direct endpoint `:8080`: it adds automatic retry while a model is mid-swap. (If you instead run n8n in Docker yourself, use `http://host.docker.internal:8081` for the host from inside the container.)

**Example workflows:** PR summarizer (GitHub webhook → fetch diff → `coder` → comment); daily digest (schedule → RSS → `ponder` → email); commit-message generator (git hook webhook → staged diff → message). n8n schedules run in UTC by default; set `n8nTimezone` in `config/user.json` and re-run `bob services start` for local time.

**Starter workflow:** a ready-to-import workflow lives at `tools/n8n-workflows/daily-research-digest.json` (see `tools/n8n-workflows/README.md`). Import it (top-right menu → **Import from file**), edit the **Config** node (`discord_url`, `rss_feed_url`, `keywords_csv`), Save, then toggle **Active**.

---

#### Troubleshooting Docker

| Symptom | Cause | Fix |
|---------|-------|-----|
| `exec /bin/sh: exec format error` on a container | Image layers corrupted by an interrupted download | `docker system prune -af` then `bob services start`; re-downloads clean copies (~3 GB) |
| `langfuse-postgres unhealthy`, `dependency failed to start` | Postgres failed to start; usually the corrupted-layer issue | Same: `docker system prune -af` + `bob services start` |
| `500 Internal Server Error` on all `docker` commands | Docker engine / WSL2 backend not started | Restart Docker and wait for it to come up (60 to 90 s) |
| `@web` in Continue returns nothing | SearXNG service not running | `bob services status`; if SearXNG isn't `Up`, run `bob services searxng start` |
| Langfuse dashboard shows no traces | Tracing not enabled or LiteLLM not running | Follow "Enabling Langfuse tracing"; confirm `bob litellm status` shows running |
| Port already in use | Another process on 3001 / 8888 / 5678 | Override the port in `config/user.json`, re-run `bob services start` |
| Lost n8n workflows or Langfuse history | `docker system prune -af` deleted `tools/*-data` | Not recoverable without a backup; back them up before prune |

#### Updating Docker service images

Bump the image tag in `tools/compose/docker-compose.yml`, then:

```
docker compose -f tools/compose/docker-compose.yml pull
bob services stop
bob services start
```

Persistent data in `tools/langfuse-data/` and `tools/n8n-data/` is preserved across image updates. Back it up before a major version upgrade in case the new container runs a non-backwards-compatible migration.

## Model quality benchmarks

`bob eval` uses [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) to run standardized tasks against the endpoint and return a reproducible accuracy score. This is separate from `bob bench`, which measures throughput (tokens/sec); `bob eval` measures *answer quality*.

**Why:** VRAM savings from lower quant levels come at an accuracy cost. Speed and VRAM are easy to measure; `bob eval` closes the loop on whether a model or quant change degraded the answers.

Requires the endpoint running first (or lets it auto-start). The eval venv is created by setup.

```
bob eval coder gsm8k --limit 100   # quick smoke test (~8 min); math word problems
bob eval coder gsm8k               # full (~90 min)
bob eval coder humaneval           # code generation (~3 hr)
bob eval ponder mmlu              # general knowledge (~90 min)
bob eval coder gsm8k --shots 5     # 5-shot variant (slightly higher scores, longer)
```

Results are saved as JSON under `results/eval-<role>-<task>-<timestamp>/`. The primary metric is `exact_match,flexible-extract` (0.0 to 1.0). Reference points for 14B Q4 quant models:

| Task | Measures | Expected (5-shot) | Expected (0-shot) |
|------|---------|-------------------|-------------------|
| `gsm8k` | math word problems | 0.72 to 0.82 | 0.60 to 0.72 |
| `humaneval` | code generation pass@1 | 0.60 to 0.72 | 0.50 to 0.65 |
| `mmlu` | general knowledge | 0.62 to 0.70 | 0.55 to 0.65 |

Scores well below these ranges usually mean the chat template wasn't applied correctly. Run the same task before and after a quant change to measure the quality delta.

## Browser chat and RAG: Open WebUI

Open WebUI is opt-in; install it at setup with `--with-webui` (Linux `./setup.sh --with-webui`, Windows `setup.bat --with-webui`). Once installed, `bob up` starts it on port 3000 (pre-wired to the local endpoint and embedding model), or `bob webui` launches it alone.

Open WebUI uses the `embed` model for document search automatically. Add documents through the workspace panel; they are indexed locally and available in any chat via the RAG interface. Create model presets in Workspace → Models (e.g. a low-temperature preset for careful, deliberate answers).

> **Agent model in WebUI:** Selecting the `agent` model in Open WebUI runs raw inference, tool schemas are not injected and `<tool_call>` blocks appear as plain text. For full tool use, run `bob agent "goal"` in the terminal, or start `bob agent serve` and call `http://localhost:8084/v1/agent/completions` from n8n or any HTTP client.

## Customizing your setup: config/user.json

Configuration is all JSON. Three files:

- `config/defaults.json`: the neutral single source of truth: `ports`, `roleTable`, and `runtime.*` defaults (persona, memory, vision, voice, agent). Both languages read it. Committed; don't edit for per-machine changes.
- `config/models.json`: the model registry: profiles, roles, files, VRAM, SHA256, launch flags, peers. Committed.
- `config/user.json`: **your** per-machine override (gitignored). The whole file is deep-merged (top-level keys) over both `models.json` (registry keys like `defaults`, `peers`, `profiles`) and the `defaults.json` runtime defaults (`persona`, `memory`, `agent`, `voice`, `vision`). No `bob` wrapper, the runtime keys sit at the top level. This is the file you edit. (Onboarding also writes an empty `{"bob": {}}` marker; that key is not config.)

`config/user.json.example` documents the shape. A minimal override:

```json
{
  "defaults": { "n8nTimezone": "America/New_York" },
  "memory": { "autoRecall": true },
  "voice":  { "sttModel": "medium" }
}
```

Config resolves the same way on every OS: live from `defaults.json` deep-merged with `user.json`. No generated `data/config.json` is written or read.

After changing config, run `bob gen` to regenerate the runtime configs (`config/llama-swap.yaml`, `config/litellm.yaml`, and Open WebUI system prompts) from the registry, no server restart needed for the next `bob serve`:

```
bob gen             # regenerate runtime configs
bob gen 12gb        # regenerate for a specific profile
```

## Managing model profiles

`config/models.json` defines all models grouped into profiles; `activeProfile` selects which one is used.

```
bob profiles             # list all profiles with VRAM footprints and current selection
bob profile 12gb         # switch profiles and regenerate the server config
bob profile auto         # detect GPU VRAM and switch to the best-fit profile
bob fetch --list 12gb    # preview what the 12gb profile would download, without downloading
bob fetch                # download any models the current profile is missing
bob show coder           # file path, size, SHA256, and disk status for one role
bob models               # list all models with backing names and load state
```

Switching profiles does not delete models from previous profiles; they stay in `models/`. Run `bob fetch` after switching to pull any files the new profile needs. To add or change a model, edit its entry in `config/models.json` (or override in `config/user.json`), then `bob fetch` to download and `bob gen`/`bob serve` to pick it up. Never edit the generated server configs (`config/llama-swap.yaml`, `config/litellm.yaml`) by hand.

## Keeping the stack current

**Update everything:**
```
bob update            # pull code + configs, sync submodules, reinstall the venv,
                      #   rebuild llama.cpp only if it moved, relock, fetch any new
                      #   models, then doctor
bob update --tag <ref> # update to a specific release tag/commit
```
`bob update` is the one command to get the latest: it fast-forwards the repo, then **downloads any models a release just added** (resume + checksum-verify; already-present GGUFs are skipped, so a code-only update downloads nothing). New default-off features arrive ready to enable, flip the flag in `config/user.json`. See [TUNING.md](TUNING.md#bumping-the-llamacpp-submodule) for verifying performance didn't regress. `bob build [--cpu] [--force]` rebuilds without bumping the submodule; `bob version` shows binary versions and submodule commits; `bob lock --check` verifies the pinned, checksum-verified build in `versions.lock`.

**Docker services (Langfuse, SearXNG, n8n):** bump image tags in `tools/compose/docker-compose.yml` and re-pull (see [Updating Docker service images](#updating-docker-service-images)).

**Python venv dependencies:** delete the relevant `tools/venv-*` directory and re-run setup (Linux `./setup.sh`, Windows `setup.bat`); it recreates missing venvs automatically.

**Fabric patterns:** re-run `bob fabric-setup` after bumping the `external/fabric` submodule; it re-copies the pattern directory.
