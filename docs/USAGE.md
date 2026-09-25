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
- **Plugins** are drop-in capabilities in `plugins/<name>/`: **summarise, draft, search, play**. Each has a CLI, `bob <plugin> ...`, which runs `main(argv)` in `plugins/<name>/invoke.py`, and (with a `tool.py`) agent tools the loop can call. List them with `bob plugins list`.

> A plugin's CLI name is its directory name; its agent tools have their own names. `bob summarise README.md --length short` runs the CLI. Through the agent it is `bob agent "summarise README.md"`, and deterministically it is the tool name: `bob --run summarise_text '{"content": "text to summarise"}'`. The other plugin tools are `draft_text`, `search_code` and `music_play` / `music_stop`.

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

Type `/` to filter the command list. Gated tools (e.g. `shell_run`, or any tool under `/agency confirm`) show an inline approval: **y** runs it once, **N** (the default) refuses, **a** approves this exact call (same tool, same arguments) for the rest of the session, and **t** approves the tool for any arguments; **Ctrl-C** cancels the in-flight turn and returns to the prompt. Inference auto-starts on your first turn if the stack isn't already up.

**Two surfaces, one core.** The shell's `/commands` and the terminal's `bob <verb>` are not competing menus: use `bob <verb>` for scripting, cron, and SSH one-shots; use `/command` to drive the same thing from inside the cockpit. The lifecycle/cockpit commands live on **both**: `bob up`/`/up`, `bob stop`/`/stop`, `bob status`/`/status`, plus `restart`, `services`, `webui`, `logs`. Each is a thin front door over one shared core (e.g. [`scripts/tools/stack.py`](../scripts/tools/stack.py)), never a second implementation. Session-only state (`/model`, `/agency`, `/session`, `/theme`, `/clear`) is shell-only by design; provisioning and one-shot conversation (`chat`, `fetch`, `build`, `setup`, …) are terminal-only. From the terminal, `bob help` prints the same generated command catalog and, at the end, lists which commands are also `/commands` in the shell.

## One-shot chat: `chat`, `think`, `code`

For scripting, piping, and quick questions without entering the shell:

```
bob chat          # opens the routed REPL, multi-turn, empty line to exit
bob think         # same, with reasoning turned ON (the chat model thinks before answering)
bob code          # same but uses the coder role, code focus
```

`think` is a reasoning **mode**, not a model swap: `bob think` and `bob chat --think` keep the chat
model and turn its thinking on. For the `ponder` role, pick it explicitly with `/model ponder` in the
shell (and `/think on`). On 16gb and up `ponder` is the same 27B as `chat` at a lower temperature with its
own reasoning prompt; on 8gb and 12gb it is a separate, larger model. Reasoning runs in the model's reasoning channel and
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
bob stop      # stop Bob's own services and free VRAM (it never touches processes Bob did not start)
```

The endpoint logs go to `logs/llama-swap.log`; tail them live with `bob logs`. The server loads a model into VRAM on first request and unloads it after idle. The exceptions are `embed` (embeddings) and, on every profile but 16gb, `fim` (autocomplete), which are pinned and never unloaded. On 16gb `fim` joins the swap group, so autocomplete and chat take turns. Only one large model is resident at a time; switching between them takes a few seconds. On 16gb and up, `chat`, `coder`, `ponder`, `writer` and `agent` are one model under five names, so moving between them costs no swap.

**mlock:** the pinned models (`embed`, and `fim` where it is pinned) are locked in physical RAM with `--mlock`, preventing the OS from paging their weights to disk under memory pressure (e.g. simultaneous VS Code autocomplete, chat, and Open WebUI load). On systems with less than 32 GB of RAM, disable it by overriding the `fim`/`embed` entries in `config/user.json` and re-running `bob gen`. Setting `mlockBig` on the swap-group models (ponder, coder, chat) extends mlock to their CPU-offloaded pages; on Windows this needs `SeLockMemoryPrivilege` (`bob mlock --grant` checks and grants it), on Linux you raise the memlock limit instead (`ulimit -l unlimited` or `/etc/security/limits.conf`).

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
| `chat` | general conversation | Qwen3.8-27B GSQ-RCO IQ3_XXS |
| `coder` | coding chat and agentic edits | *alias of* `chat` |
| `ponder` | heavy reasoning and architecture | *alias of* `chat` |
| `writer` | long-form prose and drafting | *alias of* `chat` |
| `agent` | local tool use and autonomous tasks | *alias of* `chat` |
| `vision` | image description and visual Q&A | Qwen3-VL-8B Q4_K_M + mmproj |
| `fim` | autocomplete | Qwen-Coder-1.5B Q4_K_M |
| `embed` | RAG embeddings (resident) | Qwen3-Embedding-0.6B Q8 |
| `rerank` | recall reranking (resident) | Qwen3-Reranker-0.6B Q8 |

Five of those roles are **one loaded model under five names**. Qwen3.8-27B in the GSQ-RCO
IQ3_XXS packing is 10 GB, fits a 16 GB card whole, and scores at its full-precision level on
reasoning and code, so there is nothing left for a separate reasoner or writer to do. Each name
keeps its own sampling (`writer` runs warmer, `agent` near-deterministic) through llama-swap's
per-alias parameters, and the swap group no longer thrashes between four big GGUFs.

`chat` is a *reasoning* model: it thinks before it answers, and that reasoning is routed to
`reasoning_content` rather than the reply. Give it room. A very small `--max` (say `--max 70`) can
be consumed entirely by the thinking pass and return an empty answer; the default (uncapped) path
is fine.

On the `16gb` profile `fim` shares the swap group with `chat`, because a resident autocomplete
model does not fit beside a 10 GB one: inline completion and chat take turns. `24gb` and up keep
`fim` resident, fold `vision` into the same 27B (it ships a vision projector), and run the
`-mtp` build, whose built-in draft head is worth about +40% tokens/sec.

Every model's GGUF file, HuggingFace source, context size, and launch flags are defined once in [config/models.json](../config/models.json). The downloader and the runtime config both read from it. Clients reference the role names above (`coder`, `ponder`, etc.), so swapping the backing model for a role never requires touching any client configuration.

The `12gb` profile keeps the MoE pair (Qwen3-Coder-30B-A3B + Qwen3.6-35B-A3B with expert offload): once Bob's own resident models are counted, the 27B only fits there at a packing that costs more code quality than the offload does. The `8gb` profile targets cards like the RTX 3070 and 4060 and is marked unvalidated. The `24gb` and `32gb` profiles ship near-lossless quants for bigger cards. Switch with `bob profile 12gb`, `bob profile auto` to detect from VRAM, or pass `--profile <name>` to setup before the first model download.

A `cpu` profile (a single tiny ~0.8 GB Qwen3.5-0.8B serving `chat`, with `writer` and `agent` as aliases) targets **no-GPU** boxes such as CI runners and dev laptops. It proves the serve → agent path works without a GPU (correctness and wiring, not performance); `bob profile auto` selects it when no GPU is detected, and `bob build --cpu` produces a CUDA-off engine to run it. It has no `coder`, `ponder`, `vision`, `embed` or `rerank`: a request for `coder` or `ponder` falls back to `chat` with a notice, an image request is refused with a clear message, and memory runs keyword-only.

### Pro models (API-backed, no platform fee)

Additional model names are available via the LiteLLM proxy (`:8081`) when the corresponding API keys are set. They route **litellm → API provider directly**: no llama-swap hop, no OpenRouter markup.

| Name | Role | Provider | Backing model | Approx. cost |
|---|---|---|---|---|
| `chat-pro` | general conversation | DeepSeek | deepseek-v4-flash | ~$0.27/M in |
| `ponder-pro` | heavy reasoning | DeepSeek | deepseek-v4-pro | ~$0.55/M in |
| `coder-pro` | coding | DeepSeek | deepseek-v4-flash | ~$0.27/M in |
| `writer-pro` | long-form prose | DeepSeek | deepseek-v4-pro | ~$0.55/M in |

DeepSeek V4 takes no images, so no cloud vision role ships. `--pro` on an image request uses `vision.visionProRole`, which defaults to the local `vision` model; point it at a pro role whose peer is marked `supportsVision` to send images to the cloud. A pro role without `supportsVision` refuses image input with a clear message. A pro role uses the same per-role system prompt as its local role (`prompts` in `config/models.json`) unless its peer entry sets its own `systemPrompt`. Each peer declares `contextWindow` and `maxOutputTokens` (DeepSeek V4: 1000000 and 32768, with `ponder` raised to 65536), which the generated client configs use.

**API keys**: all four pro roles route through DeepSeek by default, so only one key is needed. Set it in the environment:

Linux:
```bash
export DEEPSEEK_API_KEY='sk-...'   # platform.deepseek.com -> API keys
```

Windows:
```bat
setx DEEPSEEK_API_KEY "sk-..."     :: platform.deepseek.com -> API keys
```

Or store it via onboarding (it writes the key to `peers.deepseek.apiKey` in `config/user.json`, gitignored). Pro models are only available through `:8081` (LiteLLM). Direct `:8080` requests return "model not found" because llama-swap only serves local models.

**Other coding peers (opt-in).** Two alternative cloud coders ship defined but disabled in `config/models.json`: **GLM-5.3** (z.ai, key `ZHIPU_API_KEY`) and **Kimi K3** (Moonshot, key `MOONSHOT_API_KEY`). Enable one at a time (set its `enabled: true`, export its key, run `bob gen`); each provides `coder-pro` (GLM also provides `chat-pro`, `ponder-pro` and `writer-pro`), so run a single coding peer to avoid a name clash. DeepSeek stays the enabled default.

**Override providers or models** in `config/user.json` under a `peers` block (see `config/user.json.example`). You can disable individual peers, change which model a role uses, or add OpenRouter as a fallback (5.5% platform fee applies). Run `bob gen` after any change.

**Bill control:** set a per-key spending limit on the provider dashboard as a hard stop independent of local config. Optionally add `budget`/`budgetPeriod` to the deepseek peer block in `user.json` for a LiteLLM-side cap. Run `bob gen` after any change.

## Calling the API directly

The endpoint speaks the OpenAI chat completions API, so any HTTP client works. Point any tool already configured for OpenAI at `http://localhost:8081/v1` with Bob's LiteLLM key as the API key.

**The LiteLLM key** is generated on first use (`sk-bob-...`) and kept in `data/secrets.json` under `litellmKey` (mode 0600). A key in the environment (`BOB_LITELLMKEY`), the OS keychain, or an explicit `litellmKey` in `config/user.json` wins. Bob's own clients and every config `bob gen` writes already carry it; for your own scripts, read it from that file:

Linux:
```bash
export BOB_KEY=$(python3 -c "import json; print(json.load(open('data/secrets.json'))['litellmKey'])")
curl http://localhost:8081/v1/chat/completions \
  -H "Authorization: Bearer $BOB_KEY" -H "Content-Type: application/json" \
  -d '{"model":"coder","messages":[{"role":"user","content":"write fizzbuzz in rust"}]}'
```

Windows (Command Prompt, with `BOB_KEY` set to the `litellmKey` value from `data\secrets.json`):
```bat
curl http://localhost:8081/v1/chat/completions -H "Authorization: Bearer %BOB_KEY%" -H "Content-Type: application/json" -d "{\"model\":\"coder\",\"messages\":[{\"role\":\"user\",\"content\":\"write fizzbuzz in rust\"}]}"
```

Or use the built-in streaming CLI (identical on every OS):
```
bob chat --code "write fizzbuzz in rust"
bob chat --think "design a caching layer" --sys "Be concise." --max 1024
```

The LiteLLM proxy port defaults to `8081` (`ports.litellmPort` in `config/defaults.json`). The underlying llama-swap engine is on `8080` (`ports.port`), but clients should use `8081` for retry logic, Langfuse tracing, and pro-model access.

### Embeddings API

The `embed` model (Qwen3-Embedding-0.6B) exposes an embeddings endpoint:

Linux:
```bash
curl http://localhost:8081/v1/embeddings \
  -H "Authorization: Bearer $BOB_KEY" -H "Content-Type: application/json" \
  -d '{"model":"embed","input":"The quick brown fox"}'
```

Windows (Command Prompt):
```bat
curl http://localhost:8081/v1/embeddings -H "Authorization: Bearer %BOB_KEY%" -H "Content-Type: application/json" -d "{\"model\":\"embed\",\"input\":\"The quick brown fox\"}"
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
import os
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key=os.environ["BOB_KEY"])   # the litellmKey from data/secrets.json
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

Bob stores and retrieves facts using SQLite + Qwen3-Embedding-0.6B embeddings (the same embedder on every GPU profile). The `embed` model is already pinned in VRAM, so memory costs 0 extra VRAM and one embed call per store/recall. A profile with no embed model (cpu) runs keyword-only memory. **Memory is on by default** (`runtime.memory.enabled = true`); disable it in `config/user.json` with `{"memory":{"enabled":false}}`.

This is a summary; the full reference (typed store, ranking, scoping, sessions, every config key) is in **[MEMORY.md](MEMORY.md)**.

**From the terminal:**
```
bob remember "I prefer dark mode in all editors"
bob recall  "editor preferences"    # blended-rank search (not plain semantic), prints JSON
bob memory list                     # browse what Bob knows
bob memory show 42                   # one row incl. its provenance (which session taught it)
bob memory pin 42                    # protect a fact from pruning
bob memory forget --query "vim"     # show the matches, confirm, then forget them (--yes skips the prompt)
bob memory forget --session <id>    # retract everything a session taught Bob, its transcript included
bob memory status                   # DB path, size, per-type counts
bob memory clear --yes              # wipe memories, core blocks, the transcript and the search index
bob memory --db /path/to/other.db list   # any subcommand against another database
```

**Automatic in the `bob` shell.** You don't manage memory by hand:

- At **session start** your stable profile (and any project [`BOB.md`](MEMORY.md#project-instruction-files)) is injected once.
- At **session end** (`/exit`, `/session new`, …) durable facts are **consolidated**: the model extracts typed facts and *supersedes* contradictions instead of accumulating them ("I use vim" → later "I switched to vscode" leaves only vscode).
- Set `memory.autoRecall = true` to also inject relevant memories on **every turn** (off by default; otherwise the agent recalls on demand via the `memory_recall` tool).

Injected memory is capped at `memory.maxInjectedTokens` so it cannot overflow the context window. Memory is always local: even with `--pro`, recall and embedding stay on the local `embed` model at `:8081`. A forgotten fact also leaves the injected profile, and it can be stored again later. Memory DB defaults to `data/bob.db` (gitignored); override with `memory.dbPath`.

**Optional context-engineering upgrades** (all off by default, see [MEMORY.md](MEMORY.md)):

- **Cross-encoder rerank** (`memory.rerank`): a second-stage reranker sharpens recall relevance and filters noise (needs a local `reranking` model in the stack).
- **Core-memory blocks** (`memory.coreBlocks`): named, always-injected notes the agent curates for itself with the `memory_block` tool (MemGPT/Letta style).
- **Conversation paging** (`agent.conversationPaging`): persists the full transcript so the agent can `conversation_search` and page back turns that compaction dropped.

### First run: onboarding

Setup runs an interactive onboarding flow at the end when memory holds no profile yet (a bare `bob` on a terminal offers the same once):

```
Bob: Hi. What's your name?
> Siva
Bob: What kind of work do you do most?
> Game dev and AI tooling
Bob: Got a DeepSeek API key? (Enter to skip)
> sk-...
Bob: Ready. Type 'bob' to start.
```

This writes your name and work context to `data/bob.db` (profile table) and your API key to `peers.deepseek.apiKey` in `config/user.json` (gitignored).

### Budget tracking

```
bob budget    # shows LiteLLM spend (if proxy is running) + configured caps
```

Shows the configured `max_budget`/`budget_duration`, queries the LiteLLM proxy for spend data if it's running, and reports memory DB size at $0 cost (fully local). For a detailed per-request cost breakdown, enable Langfuse tracing (see the [observability section](#observability-file-traces-and-langfuse)).

## Voice

Voice adds two-way audio to the terminal using faster-whisper (STT) and piper (TTS). All processing is local: no cloud, no microphone data leaving the machine. Voice is **enabled by default** (`runtime.voice.enabled = true`); you only download the models once. With `voice.enabled = false`, `bob voice` and the shell's `/voice` refuse with a clear message.

**One-time model download:**
```
bob setup-voice
```
Downloads the faster-whisper STT model, the piper binary and voice, and installs the audio and STT Python deps. `bob up` starts the STT server on port 8082 on first voice use, not at boot, and its model is freed again after `voice.sttIdleSeconds` (default 900) so it does not hold ~1 GB of VRAM while nobody is talking. Set `voice.preload = true` for a warm first utterance instead. On an NVIDIA GPU, setup also installs the CUDA-12 runtime libs (cuBLAS/cuDNN) so STT runs on the GPU; otherwise, or if those libs are missing at runtime, the server falls back to CPU int8 automatically (fast enough for single-utterance voice).

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
bob whisper start|stop|status       # faster-whisper STT server (:8082; /inference and OpenAI /v1/audio/transcriptions)
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
- The default STT model handles accented English and non-English languages. For higher accuracy set `"voice": {"sttModel": "medium"}` at the top level of `config/user.json` and re-run `bob setup-voice`.

**Voice response tuning** (all under the top-level `voice` key in `config/user.json`):

| Key | Default | Effect |
|-----|---------|--------|
| `silenceSec` | `1.5` | Seconds of mic silence before recording stops. Raise if Bob cuts off while you're still speaking. |
| `sttModel` | `'small'` | STT model size (faster-whisper CT2): `tiny`, `base`, `small`, `medium`, `large-v3`. Larger = more accurate, slower. Re-run `bob setup-voice` after changing. |
| `sttComputeType` | `'auto'` | faster-whisper compute type: `auto` (float16 on GPU, int8 on CPU), or a CT2 type. |

The voice loop has no system prompt of its own: it reuses Bob's persona and the same agent turn as text chat. It sanitises text before sending it to piper, stripping markdown symbols so stray markdown from the model never reaches the TTS engine and Bob does not read punctuation aloud.

## Vision

On the 16gb profile vision uses Qwen3-VL-8B (a ~5 GB GGUF + a ~1.2 GB mmproj) to describe images and answer visual questions; it loads on demand and unloads after 30 s idle to free VRAM. On 24gb and 32gb the 27B chat model reads images itself through its own projector. 8gb, 12gb and cpu serve no vision model, and image input there is refused with a message that says so. Vision is **enabled by default** (`runtime.vision.enabled = true`); with `vision.enabled = false` every image input is refused.

**Setup:** the vision GGUF and its mmproj download with the profile's models (`bob fetch`).

**Commands:**
```
bob describe path/to/image.png
bob describe path/to/image.png "What text is visible?"
bob screenshot
bob screenshot "What application is open and what does it show?"
```

`--pro` uses `vision.visionProRole`, which defaults to the local `vision` model, because DeepSeek V4 takes no images. To send images to the cloud, point `visionProRole` at a pro role whose peer is marked `supportsVision`; a pro role without it refuses image input rather than failing at the provider. The same goes for a local role: an image sent with a pinned text-only role (such as `coder`) is refused, since a local model reads images only when its entry sets `supportsVision` or an `mmproj`.

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

Override for a single run with `--agency confirm`; set the default with `"agent": {"agency": "confirm"}` at the top level of `config/user.json`. `agent.maxSteps` (default 10) caps the tool iterations per goal.

Every front door (this loop, the shell, skill steps, `bob --run`, and MCP clients) goes through one approval gate. A tool that needs approval asks on a terminal and is refused when there is no one to ask (piped, scheduled, served). The context budget is automatic: `agent.maxContextTokens = 0` uses the per-slot window of the model serving the role, and the reply is always capped by `agent.outputReserveTokens` (default 1024) sent as `max_tokens`. A reply that hits that cap is marked as truncated, and a tool call in a truncated reply is never run. On a profile that lacks the requested role (cpu has no `coder` or `ponder`), the run falls back to `chat` and says so.

### Available tools

| Tool | What it does | Needs |
|------|-------------|-------|
| `memory` | Store and recall facts from Bob's memory DB | `embed` model running |
| `web` | Search the web, fetch URLs | nothing (in-process `ddgs`; optional providers below) |
| `git` | `git_status`, `git_log`, `git_diff` on any repo | git on PATH |
| `file` | `file_read` (within allowed paths), `file_write` (state-changing; disabled until `allowedWritePaths` is set) | `allowedReadPaths` set |
| `shell` | Run shell commands (always asks for approval, whatever the agency mode) | Interactive terminal |
| `fabric` | Run any fabric pattern on text input | fabric on PATH |

The `web` tool searches through the in-process `ddgs` metasearch provider by default (pure-Python, aggregates DuckDuckGo, Bing, and others). It needs no service, no daemon, and no Docker, and behaves identically on every OS. Optional providers: Brave or Tavily via an API key (`agent.searchProvider`), or the opt-in `searxng` Docker service. All providers fall back to `ddgs`, then to a last-ditch DuckDuckGo HTML scrape. Config keys: `agent.searchProvider` (default `ddgs`) and `agent.webSearchFallback` (default true).

The drop-in **plugins** (summarise, draft, search, play) are also callable by the agent: `summarise_text`, `draft_text`, `search_code` (ripgrep, obeying the same allow and deny paths as `file_read`), and `music_play` / `music_stop` (state-changing). Tools and plugins are **auto-discovered** from `scripts/tools/*.py` and `plugins/*/tool.py`; dropping in a file is the only registration step, there is no allowlist. Tool names are global: a module that declares a name another module already registered is refused and reported in the startup summary. To exclude a tool or plugin without deleting it, add its file stem or plugin directory name to `agent.disabledTools` (a denylist) in config.

**Sub-agents** (`spawn_agent`) run on local roles only unless `agent.subAgentAllowPro` is on, and a role's tool scopes carry into the sub-run.

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

The default `agent.toolFormat = 'hermes'` injects the tool schemas into the system prompt, and the model responds with `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` XML, the format the Qwen chat templates Bob ships are trained on. Bob's agent loop handles this transparently. Models that expect the OpenAI `tools` parameter work with `agent.toolFormat = 'openai'`.

### HTTP server (REST + SSE)

```
bob agent serve            # binds agent.serveHost:agent.agentPort (default 127.0.0.1:8084)
```

Exposes the agent loop over HTTP for n8n / WebUI / other clients. Every endpoint except `/health` requires `Authorization: Bearer <token>` (the litellm key or an `agent.apiTokens` entry). Each token maps to an owner, and sessions are owner-scoped: a token sees only sessions its owner created. Supports one-shot `POST /v1/agent/completions`, token-streaming `POST /v1/agent/completions/stream` (SSE; cancels on client disconnect), and multi-turn `POST/GET/DELETE /v1/sessions`. An unreachable model backend returns 503 and a failing one 502, so a client can tell "retry later" from a bad request. Set `agent.acceptLitellmKey = false` so only issued tokens open the API. Full endpoint contract, event schema, and n8n wiring: [AGENT-SERVER.md](AGENT-SERVER.md).

### Expose Bob's tools over MCP

```
bob agent mcp              # stdio: the client spawns Bob as a child process
bob agent mcp --http       # Streamable HTTP on 127.0.0.1:8085/mcp
```

Both need `agent.mcpEnabled = true` in `config/user.json`, and both expose the same registry: memory,
web, git, file, shell, fabric, code search, and any plugin.

Use stdio when the client sits on this machine; it needs no port and no token. Use `--http` when the
client is somewhere else, or when several clients share one Bob: the HTTP transport keeps sessions, so
it serves many clients from one process, which stdio cannot do.

An MCP client has no one to answer an approval prompt, so approval-required and state-changing tools
(`shell_run`, `file_write`, `memory_store`, `music_play`, ...) are refused over MCP unless you list them in
`agent.mcpAllowTools`. `spawn_agent` must be listed as well, and a sub-run it starts is held to the same
list.

The HTTP transport uses the agent server's token auth: the litellm key (unless
`agent.acceptLitellmKey = false`), `agent.apiTokens` entries, and, with `agent.authStore` on, scoped,
rate-limited, revocable store tokens. Only `/health` is open. It binds loopback by default. To reach it from
another machine, set `agent.mcpHost` to `0.0.0.0`, list the address that machine dials in
`agent.mcpAllowedHosts`, and issue that client its own token:

```jsonc
{ "agent": { "mcpEnabled": true, "mcpTransport": "http", "mcpHost": "0.0.0.0",
             "mcpAllowedHosts": ["bob.lan:8085"],
             "apiTokens": [{ "token": "sk-harness", "owner": "laptop" }] } }
```

Anything reachable over a LAN deserves the same care as the agent server: every Bob tool is available
to a token holder, so keep `agent.allowPrivateFetch` off and hand out per-client tokens you can revoke.

### Check agent health

```
bob setup check     # dependency + registration checks  (alias of `bob doctor --quick`)
bob doctor          # the above, plus runtime: endpoint reachable, GPU/VRAM, writable dirs, config parses
bob diagnose        # GPU, VRAM, CUDA, and model-file health check
```

`bob setup check` (equivalently `bob doctor --quick`) verifies agent dependencies in order (venv, Python packages, config, tools directory, schedules file, fabric, SearXNG, n8n, LiteLLM proxy, BobAgent task, agent model file, tool loading honoring `agent.disabledTools`, memory vectors against the embed model, the reranker batch) and prints a fix command for each failure. `bob doctor` runs all of those plus a runtime pre-flight; run it first when something's off. Both are one core (`health.health_check`) behind a depth flag.

## Deterministic invocation: `bob --run`

For scripts and CI, `bob --run <tool> '{json}'` runs exactly one capability through the real agent dispatch, no model, no reasoning loop, just the tool:

```
bob --run summarise_text '{"content": "text to summarise", "length": "short"}'
bob --run search_code '{"query": "TODO", "path": "src/"}'
bob --run git_status '{}'
```

The first argument is the tool's function name (as `bob tools list` prints it), not the plugin or file name. `--run` goes through the same approval gate as the loop: a gated tool asks on a terminal and is refused when stdin is piped, and the command exits non-zero when the tool errors or did not run. This is the plumbing consumers (and outside-terminal clients) use to invoke a single tool deterministically. List available tools with `bob tools list` and plugins with `bob plugins list`.

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

For the dedicated reasoning role, switch to it explicitly with `/model ponder` (and turn `/think on`).

**External clients (Continue.dev, aider):** they call the proxy directly and bypass Bob's `/think`, so
they get the model's native default (Qwen3 reasons by default). To suppress reasoning there, append
`/no_think` to a message; the Qwen3 chat template honors it.

## Function Calling (Tool Use)

The `coder` model supports OpenAI-style function calling. Define functions the model can request, then execute them in your app:

```python
import os
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8081/v1", api_key=os.environ["BOB_KEY"])   # the litellmKey from data/secrets.json

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

**Supported:** `coder` (Qwen3.8-27B, strong at agentic tool use). On the 16gb profile `ponder`, `chat` and `writer` are the same loaded model, so any of them works; the names differ only in sampling. In aider, tool use is handled internally.

## Clients

The Continue config is linked into your home directory during setup, so it works with no in-app configuration. Without symlink privileges, setup copies the file instead; re-run setup after editing the repo configs to sync the copy. aider is opt-in and runs with its generated config passed explicitly (`bob aider`), so nothing is written to your home directory for it. The DeepSeek Harness owns its own settings document, so Bob merges into that one rather than linking over it. Every generated config that carries the LiteLLM key is written mode 0600, and `bob gen` rewrites them all when the key changes.

### VS Code: Continue.dev (autocomplete and chat)

Continue.dev provides inline autocomplete and a chat panel inside VS Code. Setup links the repo's config into `~/.continue/config.yaml`, so all models are wired with no in-editor setup.

Install the **Continue** extension from the VS Code Marketplace, then start the endpoint (`bob up`, or talk to Bob and it auto-starts). Open the Continue panel (`Ctrl+L`) and the `coder` and `ponder` models appear immediately.

**How models map to Continue roles:**

| Continue role | Model | Purpose |
|---|---|---|
| Chat, edit, apply | `coder` (Qwen3.8-27B) | default coding chat and inline edits |
| Chat, edit | `ponder` (Qwen3.8-27B) | architecture discussion and heavy reasoning |
| Chat | `chat` (Qwen3.8-27B) | general conversation |
| Chat | `writer` (Qwen3.8-27B) | long-form prose and drafting |
| Chat, edit | `chat-pro` (DeepSeek V4, API) | general conversation via API |
| Chat, edit, apply | `coder-pro` (DeepSeek V4, API) | coding via API |
| Chat | `ponder-pro` (DeepSeek V4 Pro, API) | heavy reasoning via API |
| Chat | `writer-pro` (DeepSeek V4 Pro, API) | long-form prose via API |
| Chat | `vision` (Qwen3-VL-8B, local) | image description and visual Q&A |
| Autocomplete | `autocomplete` (the `fim` role, Qwen-Coder-1.5B) | as-you-type ghost text completions |
| Embed | `embeddings` (the `embed` role, Qwen3-Embedding-0.6B, pinned) | `@codebase` and `@docs` RAG indexing |

System prompts are set per-model and synced to clients by `bob gen`. `Ctrl+L` opens a new chat with any selected code attached; `Ctrl+I` opens an inline edit and shows a diff to accept or reject. Autocomplete fires as ghost text; `Tab` accepts. Use the model dropdown to switch roles.

Each local model's `contextLength` is its per-slot window on the active profile, the `ctx` divided across its `--parallel` slots:

| Profile | chat / coder / ponder / writer | vision | autocomplete |
|---|---|---|---|
| `16gb` | 40960 | 4096 | 8192 |
| `12gb` | chat, coder 16384; ponder, writer 8192 | (none) | 4096 |
| `8gb` | 4096 | (none) | 2048 |
| `24gb` | 98304 | 98304 | 8192 |
| `32gb` | 196608 (393216 split across two slots) | 196608 | 8192 |

Pro models carry no `contextLength`, so Continue uses its own default for them. The first message to a large model is slower while it loads into VRAM; `embed` stays pinned, and so does `fim` on every profile but 16gb, where autocomplete and chat take turns.

#### Continue.dev MCP Servers

Up to five MCP servers are wired into Continue, activating as context providers in the Continue chat panel. `searxng-search` is included only when `agent.searchProvider` is `searxng`; `bob` (Bob's own tool registry over stdio) only when `agent.mcpEnabled` is on; and the npx-launched ones (`filesystem`, `github`, `searxng-search`) only when `npx` is on PATH (`bob gen` names any it left out):

| Server | How to invoke | What it does |
|--------|--------------|-------------|
| `filesystem` | `@filesystem` then a path | Read files within a configured whitelist (paths outside return permission denied) |
| `fetch` | `@url https://...` | Fetch any URL and include its text as context |
| `github` | `@github` then a query | Search GitHub issues, PRs, and code |
| `searxng-search` | `@web` then a query | Private web search via the opt-in SearXNG service |
| `bob` | tool calls from the chat panel | Bob's tools over MCP, acting on the open workspace; approval-required and state-changing tools are refused unless listed in `agent.mcpAllowTools` |

**Prerequisites:**
- `filesystem`, `github` require Node.js (`./install_prereqs.sh --with-node`, then `bob gen`).
- `fetch` requires uv / `uvx`.
- `github` requires `GITHUB_TOKEN` set as an environment variable (a classic PAT with `repo` scope). Without it, `@github` queries return auth errors.
- `searxng-search` points at SearXNG specifically, so `@web` needs the opt-in SearXNG service running (`bob services searxng start`) and `agent.searchProvider = "searxng"`. If SearXNG is not running, `@web` queries return nothing silently. (This is distinct from the agent and CLI `web` tool, which uses in-process `ddgs` and needs no service.)

If a server fails to load, Continue shows a warning badge on its name; click it to see the error. Most failures are a missing `node`, `uvx`, or `GITHUB_TOKEN`.

### VS Code: Cline (agentic)

Cline is a more autonomous agent that reads and writes files, runs commands, and works across many turns. It is not auto-wired; configure it once in its settings panel.

Install the **Cline** extension, start the endpoint, then set the API provider to `OpenAI Compatible`:

| Field | Value |
|---|---|
| Base URL | `http://localhost:8081/v1` (replace `8081` if you changed `ports.litellmPort`) |
| API Key | Bob's LiteLLM key: the `litellmKey` entry in `data/secrets.json` (see [Calling the API directly](#calling-the-api-directly)) |
| Model ID | `coder` |

Set the context window to the `coder` role's per-slot window on your profile (the Continue table above; 40960 on 16gb). Leave image support off; these models are not multimodal in Cline. To split planning and editing, enable **Use different models for Plan and Act** and set Plan = `ponder`, Act = `coder` (switching evicts the other model from VRAM, so expect a brief load pause).

### Terminal: aider (plan and edit separately)

Aider has a genuine planning-versus-editing split: `ponder` drafts the change, `coder` turns it into file edits (on the 16gb profile both names reach the same 27B, at different temperatures). You review the plan before any edit lands. aider is opt-in: install it with `bob aider-setup` (or `./setup.sh --with-aider`), which creates `tools/venv-aider` and generates `config/aider/.aider.conf.yml` plus `config/aider/model-metadata.json`. `bob aider` passes that config with `--config` and the key through `AIDER_OPENAI_API_KEY`, so nothing goes in `~/.aider.conf.yml`. Then:

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

aider auto-commits each accepted edit to git; work on a branch so `/undo` can roll back cleanly. `model-metadata.json` tells aider each model's per-slot window on the active profile (40960 for both on 16gb), and the repo map is sized to the smaller one. On a profile without `coder` or `ponder` the config falls back to `chat`. The `openai/` prefix in the config (`openai/ponder`, `openai/coder`) is required to route through a local endpoint and is already set.

### Browser and terminal: DeepSeek Harness (dsh)

[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) is a model-agnostic coding agent with
a browser UI and a headless mode. Bob serves it two ways at once: as the model backend, and as a tool
provider over MCP. Nothing about dsh runs inside Bob, and Bob needs nothing from it; the pairing is the
one Bob is built for, a private local brain behind somebody else's front end.

dsh is Node, so install it its own way (`npx @deepseek-ai/dsh web`), then run `bob gen`. Setup wires it
too, when it is already installed. Two drop-ins land in the harness home (`$DSH_HOME`, default `~/.dsh`):

| File | What Bob writes | How |
|---|---|---|
| `settings.yaml` | a `bob` provider route: every chat-capable role plus the enabled pro peers | merged, so your other providers and sections survive |
| `cordis.patch.yml` | Bob's tool registry as an MCP server (`bob agent mcp`), stdio or HTTP per `agent.mcpTransport` | appended once, only when `agent.mcpEnabled` is on |

**What each model advertises.** A local role declares the window one request really gets: its `-c`,
divided by its slots when `--parallel` splits the KV cache (so the 32gb tier's 393216 is 196608 per
request). A role under 16384 is left out, because dsh's prompt and pi-ai's fixed 4096-token output margin
would leave it no room to answer, and `bob gen` names what it left out. A pro role declares its peer's
`contextWindow` and `maxOutputTokens` (the model's own limits, the same output cap LiteLLM applies to
every client), and is image capable only when the peer says `supportsVision`.

Both are generated into `config/dsh/` first, from the same registry every other client config comes from,
so a model refresh reaches dsh with one `bob gen` and dsh re-reads the route on its next request.

**The key.** The route names its credential (`BOB_LITELLM_KEY`) instead of carrying it, and `bob gen`
stores Bob's `litellmKey` under that name in dsh's own credential store (`$DSH_HOME/.credentials.yaml`,
owner-only). dsh watches that file, so the route authenticates on its next request with nothing exported
and no restart. An exported `BOB_LITELLM_KEY` still wins for the run it was exported in.

**Why the route sets compatibility switches.** pi-ai, the dsh adapter this route uses, infers a request
shape from the endpoint URL and treats an address it does not recognize as OpenAI itself. Two of those
inferences are wrong for llama.cpp: a reasoning model's system prompt would travel as `role: developer`,
and the output cap as `max_completion_tokens`. The generated route sets `supportsDeveloperRole: false`
and `maxTokensField: max_tokens`, which is why models work rather than every request failing.

**Bob's tools inside dsh.** With `agent.mcpEnabled` set to `true` in `config/user.json`, dsh spawns
`bob agent mcp` over stdio and gets the whole registry: memory, web, git, file, shell, fabric, code
search, and any plugin. The entry runs the server in the harness's own working directory, so those tools
act on the project dsh has open, not on Bob's repo.

**A harness on another machine.** Set `agent.mcpTransport = "http"` and `bob gen` writes the same entry
as a Streamable HTTP connection instead of a spawn, so dsh dials a Bob that is already running
(`bob agent mcp --http`). Set `agent.mcpUrl` when dsh reaches Bob at something other than the local bind
address, and export `BOB_LITELLM_KEY` (or the token you issued that client) on the dsh side, since the
generated entry sends it as a Bearer header. When the transport changes, `bob gen` replaces the existing
Bob entry in `cordis.patch.yml` in place rather than adding a second one.

## Shell AI Patterns: fabric

fabric transforms piped text through a named prompt pattern: a structured prompt with a specific output format baked in. Where `bob chat` is a blank canvas, fabric patterns encode the *format* of the answer (commit message, executive summary, code-review checklist). Patterns live in `~/.config/fabric/patterns/`, each a directory with a `system.md`.

It ships as a Go binary built from the `external/fabric` submodule, and it is opt-in. Run `bob fabric-setup` (or `./setup.sh --with-fabric`) once to build and configure it: it builds the `fabric` binary from `external/fabric/cmd/fabric/`, copies the 254 patterns into `~/.config/fabric/patterns/`, and adds Bob's endpoint as fabric's LiteLLM vendor (the `LITELLM_*` keys) without overwriting anything else in your `~/.config/fabric/.env`. Then pipe any text:

```
git diff --staged | fabric --pattern write_git_commit   # commit message from staged diff
cat notes.txt     | fabric --pattern summarize          # summarize a document or log
cat error.log     | fabric --pattern explain            # explain an error
cat myfile.py     | fabric --pattern code_review        # code review
cat meeting.txt   | fabric --pattern extract_wisdom     # action items from meeting notes
fabric -l                                               # list all 254 patterns
```

fabric uses the `coder` model by default; pass `--model ponder` for complex analysis. The agent's `fabric_run` tool always calls fabric with `--vendor LiteLLM --model coder`, so it reaches Bob whatever your fabric defaults are. To update patterns after a submodule bump, re-run `bob fabric-setup` (patterns re-copied; the binary rebuilds only if missing, delete it first to force a rebuild).

## Ecosystem Services

### LiteLLM proxy

LiteLLM sits between clients and llama-swap, adding retry logic and structured request logging.

```
bob litellm          # start the proxy on :8081 in the background (PID tracked)
bob litellm status   # show PID and uptime
bob litellm stop     # stop the background proxy
```

All clients (Continue, aider, Cline, fabric, Open WebUI, `bob chat`) use `:8081` by default. The proxy exposes all local model names (`coder`, `ponder`, `chat`, `writer`, `agent`, `vision`, `fim`, `embed`, `rerank`, whichever the profile serves) plus the pro model names (`chat-pro`, `ponder-pro`, `coder-pro`, `writer-pro`) when API keys are set. It requires the LiteLLM key on every request and binds `bindHost` (loopback by default). Each local role's sampling is enforced server-side by llama-swap (`setParams`), so a client's `temperature` or `top_p` cannot override it. Direct `:8080` access to llama-swap still works for local models but bypasses retry logic and Langfuse tracing.

`config/litellm.yaml` is generated automatically by `bob gen` and `bob serve`; do not edit it by hand. It holds no key: it reads `master_key: os.environ/LITELLM_MASTER_KEY`, which Bob sets from `litellmKey` when it starts the proxy.

**When the key changes** (an upgrade, a rotated secret), every start, auto-start included, regenerates the generated configs that still carry the old key and restarts a Bob-started proxy that rejects the current one. Open WebUI's stored connection to Bob's proxy is updated before WebUI starts, and `bob gen` also updates fabric's LiteLLM key. `bob doctor` reports it on the "Generated configs carry the current LiteLLM key" row. Clients Bob does not configure (a phone, another machine, your own scripts) need the new key by hand.

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

Docker must be running before starting a Docker service (SearXNG or Langfuse). On Windows, the first start installs Docker Desktop. Override ports or the timezone with top-level keys in `config/user.json` (not under `defaults`):

```json
{ "langfusePort": 3001, "searxngPort": 8888, "n8nPort": 5678, "n8nTimezone": "America/New_York" }
```

Every service binds `bindHost` (default `127.0.0.1`): LiteLLM, Open WebUI, n8n (`N8N_LISTEN_ADDRESS`), and the Docker services' published ports. Set `"bindHost": "0.0.0.0"` for LAN access; llama-swap stays on loopback regardless. piper and faster-whisper have no authentication, so they bind `voiceBindHost` (default `127.0.0.1`) instead and stay on loopback unless you set it too. Each service's secret (the n8n encryption key, the SearXNG secret, the Langfuse keys and passwords) is generated on first start into `data/secrets.json`.

After changing any of these, re-run `bob services start` to regenerate `.env` and restart containers.

**Persistent data:**
- `tools/langfuse-data/` (gitignored): Langfuse's Postgres database (projects, users, API keys)
- Docker named volumes `langfuse-clickhouse-data` and `langfuse-minio-data`: Langfuse's traces and event blobs
- `tools/n8n-data/` (gitignored): n8n workflows, credentials, and execution history

These survive `bob services stop`/`start`. Removing the Docker volumes (`docker volume prune`, `docker compose down -v`) deletes the Langfuse trace history: back it up if you need it.

---

#### Observability: file traces and Langfuse

The default trace sink is a local **file sink** (Docker-free): traces write to `logs/traces/<trace_id>.jsonl` and you read them with `bob traces` (`bob traces list`, `bob traces show <id>`). `agent.tracing` gates tracing (default off); `agent.tracingSink` is `file` (default) or `otlp`, where `otlp` exports to `agent.otlpEndpoint`.

**Langfuse** is an **opt-in** upgrade over the built-in file traces: a full dashboard for the same data. It runs Langfuse v3 in Docker: the web app and a worker, with Postgres, ClickHouse, Redis and MinIO. Start it with `bob services langfuse start`, then open `http://localhost:3001`. Log in as `admin@local.dev` with the generated password, the `langfuseAdminPassword` entry in `data/secrets.json` (the start command prints the path).

Langfuse records every bob request routed through LiteLLM: full prompt, response, model name, latency, token counts, and retry events. Use it to debug unexpected answers, compare quant levels (run `bob eval` before/after a profile switch), audit agentic tool calls, and track token burn.

**Enabling Langfuse tracing** (opt-in; Langfuse doesn't auto-capture, and only requests through LiteLLM are visible):

1. Start the Langfuse service: `bob services langfuse start`. Its project is created with a generated key pair (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` in `data/secrets.json`), so there are no keys to copy. An exported `LANGFUSE_*` pair (a hosted Langfuse) wins.
2. Enable Langfuse callbacks with a top-level key in `config/user.json`:
   ```json
   { "langfuseEnabled": true }
   ```
3. Regenerate the proxy config and restart it:
   ```
   bob gen
   bob litellm stop
   bob litellm
   ```
4. Point your client at `:8081` (or use `bob chat`, which goes through LiteLLM automatically).
5. Requests appear in the Langfuse dashboard under **Traces** within a few seconds.

The steps above route LiteLLM request logs to Langfuse. To also export the **agent's** own traces (the file-sink data) to Langfuse instead of local files, set `agent.tracing` to `true` and `agent.tracingSink` to `otlp`. With `agent.otlpEndpoint` empty, Bob sends them to the local Langfuse OTLP route (`/api/public/otel/v1/traces` on the Langfuse port) with HTTP Basic auth built from the same generated key pair.

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
- Authentication: the **Bob LiteLLM** credential (Header Auth), which `bob services n8n start` creates and keeps in sync with Bob's key. Pick it in the node's Credential field rather than typing a header.
- Body (JSON):
  ```json
  { "model": "coder", "messages": [{ "role": "user", "content": "{{ $json.text }}" }] }
  ```

The response is `choices[0].message.content`; wire that to whatever you want. Prefer the LiteLLM proxy at `:8081` over the direct endpoint `:8080`: it adds automatic retry while a model is mid-swap. (If you instead run n8n in Docker yourself, use `http://host.docker.internal:8081` for the host from inside the container.)

**Example workflows:** PR summarizer (GitHub webhook → fetch diff → `coder` → comment); daily digest (schedule → RSS → `ponder` → email); commit-message generator (git hook webhook → staged diff → message). n8n schedules run in UTC by default; set the top-level `n8nTimezone` in `config/user.json` and restart n8n for local time.

**Starter workflows:** ready-to-import workflows live in `tools/n8n-workflows/` (daily research digest, vision describe, voice transcribe; see its README). Import one (top-right menu → **Import from file**); for the digest, edit the **Config** node (`discord_url`, `rss_feed_url`, `keywords_csv`), Save, then toggle **Active**.

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
| Lost Langfuse history | The Langfuse Docker volumes or `tools/langfuse-data/` were removed | Not recoverable without a backup; back them up before pruning volumes |

#### Updating Docker service images

Bump the image tag in `tools/compose/docker-compose.yml`, then:

```
docker compose -f tools/compose/docker-compose.yml pull
bob services stop
bob services start
```

Persistent data in `tools/langfuse-data/` is preserved across image updates. Back it up before a major version upgrade in case the new container runs a non-backwards-compatible migration.

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

Results are saved as JSON under `results/eval-<role>-<task>-<timestamp>/`. The primary metric is `exact_match,flexible-extract` (0.0 to 1.0). Reference points for a 14B-class Q4 coder (a yardstick for spotting a regression, not a measurement of the current profiles):

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
- `config/user.json`: **your** per-machine override (gitignored). The whole file is deep-merged (top-level keys) over both `models.json` (registry keys like `defaults`, `peers`, `profiles`, `prompts`) and the `defaults.json` runtime defaults (`persona`, `memory`, `agent`, `voice`, `vision`, plus the top-level `bindHost`, `litellmKey`, `langfuseEnabled`, `n8nTimezone` and port keys). No `bob` wrapper, the runtime keys sit at the top level. This is the file you edit. (Onboarding also writes a `bob` marker section; that key is not config.)

`config/user.json.example` documents the shape. A minimal override:

```json
{
  "n8nTimezone": "America/New_York",
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
bob update            # pull code + configs, sync submodules, reinstall the venvs from their
                      #   locks, update llama.cpp only if it moved, fetch any new models,
                      #   restart a running endpoint, then doctor
bob update --tag <ref> # update to a specific release tag/commit
bob update --no-restart # leave a running endpoint on the pre-update binaries
```
`bob update` is the one command to get the latest: it moves to the target for your channel (`stable` = the latest release, which carries the prebuilt engines; `latest` = `main`), swaps in the matching engine (a fast prebuilt download where available, else a source rebuild; the new files replace the old ones atomically, and any error restores the previous engine), and **downloads any models a release just added** (resume + checksum-verify; already-present GGUFs are skipped, so a code-only update downloads nothing). Pick the channel with `bob update --channel stable|latest`, or `--from-source` to build the engine. An endpoint that kept serving through the update is restarted at the end, because a running server holds the pre-update binaries and the generated config from before the pull (`config/llama-swap.yaml` is rebuilt from `config/models.json` on every start), so one `bob update` is the whole move; pass `--no-restart` to leave it serving and restart later with `bob restart`. Only an endpoint the stack started in the background is restarted: a foreground `bob serve` writes no pidfile, so the update reports it and leaves it alone rather than killing a process out from under your terminal. Ctrl+C and rerun `bob serve` to finish that one. The restart waits for the old processes to exit before starting new ones. `bob update` does not rewrite `versions.lock`; the release you move to carries its own. New default-off features arrive ready to enable, flip the flag in `config/user.json`. See [TUNING.md](TUNING.md#updating-the-llamacpp-engine) for verifying performance didn't regress. `bob build [--cpu] [--from-source] [--force]` re-provisions the engine without bumping the submodule; `bob version` shows binary versions and submodule commits; `bob lock --check` verifies the pinned, checksum-verified build in `versions.lock`.

**Docker services (Langfuse, SearXNG):** bump image tags in `tools/compose/docker-compose.yml` and re-pull (see [Updating Docker service images](#updating-docker-service-images)).

**Python venv dependencies:** delete the relevant `tools/venv-*` directory and re-run setup (Linux `./setup.sh`, Windows `setup.bat`); it recreates missing venvs from their `.lock` files automatically. (aider: `bob aider-setup --force`.)

**Fabric patterns:** re-run `bob fabric-setup` after bumping the `external/fabric` submodule; it re-copies the pattern directory.
