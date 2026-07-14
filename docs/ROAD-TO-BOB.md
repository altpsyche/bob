# Road to Bob

A roadmap for evolving `bob` into **Bob**: a personal, always-local AI assistant product.

---

## What Bob Is

**Bob is your private AI brain on your own hardware, Windows or Linux.**

Most AI assistants live in the cloud and know nothing about you. Bob runs entirely on your GPU, costs nothing per query, and builds a growing understanding of how you work: your projects, preferences, and context that persists across sessions.

Bob is not a chatbot you visit. Bob is infrastructure you run. When you open a terminal, Bob is already on. When you ask a question, it answers in under a second. When you forget what you decided last Tuesday, Bob remembers. When you need the strongest answer, Bob routes to cloud and tells you. When you want to hear it instead of read it, Bob speaks.

### The Bob Product Family

| Product | What it is |
|---------|-----------|
| **Bob** | Core personal assistant (this repo) |
| **BobReview** | Code review and report generation |
| **BobBot** | Game AI plugin |

---

## Current Architecture (Do Not Break)

```
All clients → LiteLLM :8081 (retry, budget cap, Langfuse tracing)
  ├── Local roles  → llama-swap :8080 → llama-server (GPU or CPU inference)
  └── Pro roles    → provider API directly (DeepSeek, etc.)
```

> **Cross-platform (Module NC, done).** The orchestration now runs on Windows **and** Linux under
> PowerShell 7 behind one seam (`scripts/_platform.ps1`), with a CPU / no-GPU build+serve tier for CI
> and GPU-less boxes. The binary is `llama-server.exe` on Windows, `llama-server` on Linux. See
> [PORTABILITY.md](PORTABILITY.md).

### 11 Model Roles Today

| Role | Backend | Notes |
|------|---------|-------|
| `ponder` | Qwen3-30B-A3B Q4 (local) | Thinking mode, reasoning |
| `coder` | Qwen2.5-Coder-14B Q4 (local) | Function calling, speculative decode |
| `chat` | Qwen3-14B Q4 (local) | General conversation |
| `fim` | Qwen2.5-Coder-3B (local, pinned) | Autocomplete, never unloads |
| `embed` | BGE-M3 (local, pinned, 600 MB VRAM) | Embeddings, always live |
| `vision` | Qwen2-VL-7B Q4 + mmproj (local, on-demand) | Image description and visual Q&A |
| `chat-pro` | DeepSeek API | Cloud, $0 platform fee |
| `coder-pro` | DeepSeek API | Cloud |
| `ponder-pro` | DeepSeek Reasoner API | Cloud, strongest reasoning |
| `vision-pro` | DeepSeek V4 API (vision-capable) | Cloud vision; uses existing DEEPSEEK_API_KEY |
| `agent` | Hermes-3-Llama-3.1-8B Q5 (local) | Hermes 3 XML tool-call format; agent loop |

**Pro peers are provider-agnostic.** `user.psd1` can swap any role to Zhipu, OpenRouter, Anthropic, or any OpenAI-compatible endpoint. The generators handle it transparently.

**Config chain:**
`config/models.psd1` (base, committed) ← merged with `config/user.psd1` (personal, gitignored)
→ `gen-llama-swap.ps1` → `config/llama-swap.yaml`
→ `gen-litellm.ps1` → `config/litellm.yaml`

**All API calls must go through LiteLLM :8081** (not directly to :8080): ensures retry logic, budget tracking, and Langfuse tracing apply to everything including Bob's own requests.

---

## Phase 0: Pure Rebrand (Done)

**Effort:** ~0.5 days  
**Risk:** Low: pure text changes, zero functionality change  
**Goal:** The product is named Bob. Every user-facing string reflects it.

### Files to create

| File | Action |
|------|--------|
| `scripts/bob.ps1` | Copy of `llm.ps1` with all `llm <cmd>` → `bob <cmd>` in user-facing strings *(historical: `llm.ps1` was later retired in Module M10; `bob` is now the single CLI)* |
| `config/bob.psd1` | Stub file reserving schema (committed, not gitignored) |

### Files to edit

| File | What changes |
|------|-------------|
| `scripts/install-cli.ps1` | Emit `bob.cmd` shim; emit deprecated `llm.cmd` that prints "use bob instead" |
| `scripts/up.ps1` | `llm stop/logs` → `bob stop/logs` in status output |
| `scripts/start.ps1` | `'llm stop'` → `'bob stop'` in free-port warning |
| `scripts/_models.ps1` | `$env:LLM_PROFILE` → `$env:BOB_PROFILE` in `Resolve-ProfileName` |
| `config/models.psd1` | `webuiSecret = 'bob-dev'` → `'bob-dev'`; inline comments |
| `config/continue/config.yaml` | `name: bob` → `name: bob` |
| `config/searxng/settings.yml` | `secret_key: "bob-searxng"` → `"bob-searxng"` |
| `tools/compose/docker-compose.yml` | All `bob` literals → `bob` (Langfuse org/project, encryption keys) |
| `setup.bat`, `install_prereqs.bat` | Comment headers only |
| All `docs/*.md` | `llm ` → `bob ` (space-suffixed to avoid hitting `llama`, `litellm`) |
| `README.md` | Title + all command examples |

> **Note, Docker volume migration:** Changing `N8N_ENCRYPTION_KEY` and Langfuse secrets breaks existing container data. Before running `bob services start` after the rename: stop all services, delete `tools/n8n-data/` and `tools/langfuse-data/`, then restart.

### Verification checklist

```powershell
bob help                     # bob-branded help text
bob status                   # running models shown
bob chat chat "hello"        # works (old syntax)
llm help                     # fails: shim removed, command not found
```

---

## Phase 1: Bob Gets Identity (Done)

**Effort:** 3 to 5 days  
**Risk:** Low to Medium  
**Goal:** `bob chat` feels like talking to an assistant, not a server. Persona is configured. Memory is available.

> **Note: Superseded by Modules MEM / MEM2. This Phase 1 memory spec is historical.** The shipped
> memory engine is far beyond this: a typed/owner/project-scoped SQLite store (schema v3), blended
> recall ranking, importance/salience, pinning, conflict-aware consolidation (supersede, not
> accumulate), provenance + forget-by-session, `BOB.md` project files, persisted `bob` shell
> sessions, and auto-injection at session start (the "explicit-only" note below is no longer true).
> The schema, CLI list, and config block below are **out of date**: see **[MEMORY.md](MEMORY.md)**
> for what actually ships.

**Implementation notes (deviations from spec):**
- Memory injection is **explicit-only** (`!recall <query>` in REPL): no auto-injection per turn. Prevents context window bloat.
- `autoSummarize` implemented: REPL `finally` block calls `bob_memory.py summarize-session` when `memory.enabled` and `memory.autoSummarize = $true`.
- `autoFallback` config exists (`$false` default) but fallback logic not implemented; deferred to Phase 1.x.

### 1.1 Persona config (`config/bob.psd1`)

New file, **committed** (persona is part of the product). Users who want to override it use `config/user.psd1`, consistent with the existing `models.psd1` + `user.psd1` pattern.

Load via a new `Get-BobConfig` function added to `scripts/_models.ps1`.

```powershell
@{
  persona = @{
    name         = 'Bob'
    systemPrompt = @'
You are Bob, a personal AI assistant running privately on this machine. You are direct, practical, and you remember what matters. You assist with software development, writing, planning, and daily work. Relevant memories from past sessions are provided in context when available. When you don't know something, say so.
'@
    style        = 'direct'    # direct | friendly | formal
  }

  routing = @{
    defaultRole  = 'chat'      # used by `bob chat` with no flags
    proRole      = 'chat-pro'  # used by `bob chat --pro`
    thinkRole    = 'ponder'   # used by `bob think` / `bob chat --think`
    codeRole     = 'coder'     # used by `bob code`
    autoFallback = $false      # $true = fall back to local if cloud fails
  }

  memory = @{
    enabled          = $false      # flip to $true to activate
    dbPath           = 'data\bob.db'
    embedModel       = 'embed'     # BGE-M3, already pinned at :8081
    recallK          = 5
    maxSummaryTokens = 256
    autoSummarize    = $true       # summarize session when REPL exits
  }

  voice     = @{ enabled = $false }    # Phase 2
  proactive = @{ enabled = $false }    # Phase 3
}
```

### 1.2 `bob chat` interactive REPL + smart routing

**Model selection logic:**

```
bob chat                       → defaultRole ('chat')
bob chat "question"            → defaultRole, single-shot with persona
bob chat --pro "question"      → proRole ('chat-pro'), cloud
bob chat --think "reason X"    → thinkRole ('ponder'), local deep think
bob chat --think --pro "..."   → 'ponder-pro', cloud strongest
bob chat --model coder "..."   → explicit override (backward compat)
bob chat chat "..."            → old syntax still works
```

**First-class aliases** (add to `bob.ps1` switch):

```
bob code "write me X"         → codeRole ('coder')
bob code --pro "..."          → 'coder-pro'
bob think "plan X"            → thinkRole ('ponder')
bob think --pro "..."         → 'ponder-pro'
```

**REPL mechanics** (in the `'chat'` case of `bob.ps1`):
1. Parse flags to determine target role name
2. Load `Get-BobConfig`, build initial `$messages` with system prompt
3. If `memory.enabled`: call `bob-memory recall <query>` → prepend `[Memory: ...]` system chunk
4. Enter `while` loop: read line → POST to `http://localhost:8081/v1/chat/completions` (streaming) → append to `$messages`
5. Ctrl+C / `exit` → if `autoSummarize`, call `bob-memory summarize-session`

**autoFallback:** When `$true` and the cloud API fails, retry against the local role with a visible notice:
```
[Bob] Cloud unreachable, falling back to local model
```

### 1.3 Memory: SQLite + BGE-M3

BGE-M3 is already pinned (0 extra VRAM). It's at `:8081/v1/embeddings`. Memory costs 0 VRAM + 1 HTTP call per recall.

**New: `scripts/bob_memory.py`** (~100 lines, thin):
- `store "text" [--source user|session]`: embed → cosine insert into SQLite
- `recall "query" [--top 5]`: embed → top-K by cosine similarity → JSON output
- `summarize-session --id N`: POST to `:8081/v1/chat/completions` (chat role) → store summary

**Python env:** Run in `tools/venv-litellm` (already has `requests`/`httpx`). No new venv.

**Wrapper: `scripts/bob-memory.ps1`**: activates `venv-litellm`, calls `bob_memory.py`.

**SQLite schema** (`data/bob.db`, add `data/` to `.gitignore`):
```sql
memories(id, content, embedding BLOB, source TEXT, created_at TEXT, last_used TEXT, use_count INT, tags TEXT)
sessions(id, started_at TEXT, ended_at TEXT, summary TEXT, topics TEXT)
profile(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)
```

> **Memory is always local**: even when `bob chat --pro` routes responses to DeepSeek, memory recall still uses BGE-M3 at `:8081`. Private by design.

### 1.4 Onboarding (extend `scripts/setup.ps1`)

Run once if `data/bob.db` doesn't exist yet:

```
Bob: Hi. What's your name?
> Siva

Bob: What kind of work do you do most?
> Game dev and AI tooling

Bob: Got a DeepSeek API key? (enables cloud-quality answers when you want them)
> sk-...

Bob: Ready.
```

Stores profile in SQLite. Writes key to `config/user.psd1` (gitignored). Runs `bob gen` to activate pro peers.

### 1.5 New CLI subcommands

```
bob remember "fact"       # manually store to memory
bob recall "query"        # debug: query memory, print results
bob memory status         # DB size, entry count, last session date
bob memory clear          # drop all entries (requires --yes)
bob budget                # show cloud spend vs cap (reads litellm.yaml + Langfuse if enabled)
```

**`bob status` enhancement:** Show local models (running/available) + which pro roles are configured (green = API key set, grey = not configured).

---

## Phase 2: Bob Gets Senses (Done)

**Effort:** 4 to 6 days  
**Risk:** Medium (audio capture is fiddly on Windows)  
**Goal:** Voice in + voice out + vision. All local. Also wired voice/vision into every client in the stack (Phase 2.5).

### 2.1 Voice input: whisper.cpp STT (done)

`whisper-server.exe` on `:8082`. Model: `ggml-small.en.bin` (~74 MB; `sttModel = 'small'` in `bob.psd1`).

```
bob listen                    # record mic → whisper :8082 → transcript printed
bob transcribe <file>         # transcribe audio file to stdout
bob whisper start|stop|status # server management
```

### 2.2 TTS: piper-tts + HTTP server (done)

`bin/piper.exe` + `bin/voices/en_GB-alan-medium.onnx`. ~200 ms latency. Piper reads text from stdin.

New: `scripts/piper_server.py`, a FastAPI server wrapping piper, exposes OpenAI-compatible `POST /v1/audio/speech` on `:8083`. Open WebUI wires to this for browser TTS.

```
bob speak "text"              # synthesize + play (piper TTS direct)
bob piper start|stop|status  # HTTP server management (:8083, for WebUI)
```

### 2.3 Continuous voice loop (done)

Voice loop calls `Invoke-BobStream` directly (not a subprocess) with the voice-specific system prompt, then strips markdown via `Format-ForSpeech` before sending to piper. Keeps replies short (`voice.maxTokens = 512`).

```
bob voice                     # continuous loop: listen → chat → speak (Ctrl+C to stop)
bob voice --pro               # same, routes to cloud (DeepSeek API)
```

**Voice loop latency (16 GB RTX):** ~300 ms STT + ~1 s TTFT + ~200 ms TTS = conversational.

### 2.4 Vision: Qwen2-VL-7B (done)

`llama-server.exe --mmproj qwen2-vl-mmproj.gguf` exposes OpenAI-compatible image input. Model loads on demand, TTL 30 s.

```
bob describe <image>          # describe image file
bob describe <image> --pro    # cloud vision via DeepSeek V4
bob screenshot [prompt]       # capture active display → describe
bob screenshot --pro          # cloud vision for complex screenshots
```

`vision-pro` routes to DeepSeek V4 (deepseek-chat supports vision input natively). Uses existing `DEEPSEEK_API_KEY`, no new credentials.

### 2.5 Client stack wiring (done)

- **Continue.dev**: `vision` model entry added to `config/continue/config.yaml`
- **LiteLLM**: `vision` entry has `supports_vision: true`; `vision-pro` entry routes to DeepSeek
- **n8n**: `vision-describe.json` and `voice-transcribe.json` workflows added to `tools/n8n-workflows/`
- **Open WebUI**: wire STT to whisper `:8082` and TTS to piper `:8083` in Admin Panel → Audio

### 2.6 `config/bob.psd1` Phase 2 additions (done)

```powershell
voice = @{
  enabled      = $true
  sttPort      = 8082
  sttModel     = 'small'
  ttsEngine    = 'piper'
  ttsVoice     = 'en_GB-alan-medium'
  ttsPort      = 8083
  silenceSec   = 1.5
  maxTokens    = 512
  systemPrompt = @'
You are Bob, a voice assistant. Reply in natural spoken sentences only.
Never use markdown: no asterisks, no bullet points, no pound signs, no backticks, no numbered lists, no dashes as bullets, no special symbols.
If you need to list things, say "first", "then", "finally" or similar spoken connectives.
Keep answers brief and direct. One to three sentences is ideal.
'@
}
vision = @{
  enabled       = $true
  visionRole    = 'vision'
  visionProRole = 'vision-pro'
}
```

---

## Phase 3: Bob Gets Agency (Done)

**Effort:** 1 to 2 weeks (delivered incrementally)  
**Risk:** Medium  
**Goal:** Bob does things without being asked.

### 3.1 Scheduled tasks (done)

Background process: `scripts/bob-agent.ps1` registered as a recurring task `BobAgent`, a Windows Scheduled Task, or a cron entry on Linux (NC4, via `Register-AgentTask`).  
Config: `data/schedules.json`. Fires every minute; checks which cron entries are due (5-field cron, 60 s double-fire guard).

```powershell
bob agent schedule add "morning-summary" --cron "0 9 * * *" --goal "check git log and summarise today's work"
bob agent schedule list
bob agent schedule run <name>      # fire immediately (ignores cron)
bob agent schedule remove <name>
bob agent install                  # register the recurring BobAgent task (Windows Scheduled Task / Linux cron)
bob agent status                   # show task state
```

Results stored in `data/schedules.json` under `lastRunResult`. `notify = true` on an entry fires a desktop notification (a Windows toast, or `notify-send` on Linux; `Send-Notification` in the NC1 seam). Scheduler always runs with `agency = 'silent'`.

### 3.2 Toast notifications (done)

`scripts/bob-toast.ps1`: PowerShell `Windows.UI.Notifications` API.  
Used by: scheduled task results (`notify = true`), long-running agent goals. `toastAppId` in `config/bob.psd1` controls the sender identity.

### 3.3 Plugin system (done)

Plugin = `plugins/<name>/` with `invoke.ps1` or `invoke.py` (plus optional `description.txt`).

```powershell
# bob.ps1 default case: plugin fallback runs before showing help
$pluginDir = Join-Path $repo "plugins\$cmd"
if (Test-Path "$pluginDir\invoke.ps1") { & "$pluginDir\invoke.ps1" @rest }
elseif (Test-Path "$pluginDir\invoke.py") { & $venvPy "$pluginDir\invoke.py" @rest }
```

Any language participates: a plugin just needs to accept args and write to stdout. Python plugins run in `venv-litellm` (has `openai`, `requests`). PowerShell plugins run directly.

```powershell
bob plugins list    # show all installed plugins with type and description
```

**Built-in Phase 3 plugins:**
- `summarise` (`bob summarise <file>` or `cat file | bob summarise`): feeds text to local LLM, streams a summary. `--length short|medium|long`
- `draft` (`bob draft "<prompt>" --type email|pr|slack|doc`): drafts text from a one-liner using the ponder/chat role; output is clean, ready to paste
- `search` (`bob search "<query>" [--path dir]`): ripgrep files then LLM synthesises the results; `--raw` skips LLM
- `play` (`bob play <search query>`): opens Spotify URI if installed, falls back to YouTube Music in browser

---

## Phase 4: Cohesive Plugin and Tool Architecture (Done)

**Effort:** ~0.5 days
**Risk:** Low: additive only, no existing behaviour changed
**Goal:** Every plugin callable by both humans (`bob <name>`) and the agent (`bob agent "..."`). No capability duplication between layers.

### The problem

Phases 0 to 3 created two parallel capability systems that didn't talk to each other:
- **Agent tools** (`scripts/tools/*.py`): the LLM calls these autonomously
- **Plugins** (`plugins/<name>/invoke.py`): humans call from the terminal

`tool_loader.py` already scanned `plugins/<name>/tool.py` for agent tools, but nothing used it. Result: `summarise`, `draft`, `search`, and `play` were invisible to the agent. `music.py` was placed in `scripts/tools/` (wrong layer) because no rule existed to guide placement.

### Three-layer model

```
Layer 1  scripts/tools/<name>.py        infrastructure / pure plumbing (git, file, memory, web, shell)
Layer 2  plugins/<name>/tool.py         agent-facing interface for a CLI plugin
Layer 3  plugins/<name>/invoke.py       human-facing CLI wrapper
```

**Core logic rule:** Logic lives in `invoke.py` as an importable function. `tool.py` imports and calls it. The CLI calls it too. One function, two callers, no duplication.

**Registration rule:** *(superseded by Module M1, see [plugins/AUTHORING.md](../plugins/AUTHORING.md))* Tools now **auto-discover**; there is no `agent.tools` allowlist. Creating the file is the only step; exclude one via the `agent.disabledTools` denylist. The historical text below described the original allowlist design.

### What changed

| File | Action |
|------|--------|
| `scripts/tools/music.py` | Deleted: wrong layer |
| `plugins/play/tool.py` | Created: music moved to correct layer |
| `plugins/summarise/invoke.py` | `summarise()` core function extracted |
| `plugins/summarise/tool.py` | Created: exposes `summarise_text` to agent |
| `plugins/draft/invoke.py` | `draft()` core function extracted |
| `plugins/draft/tool.py` | Created: exposes `draft_text` to agent |
| `plugins/search/invoke.py` | `synthesise()` core function extracted |
| `plugins/search/tool.py` | Created: exposes `search_code` to agent |
| `config/bob.psd1` | `agent.tools`: `'music'` → `'play', 'summarise', 'draft', 'search'` |
| `plugins/AUTHORING.md` | Created: authoritative human guide for adding plugins and tools |
| `.claude/CLAUDE.md` | Created: Claude Code rules for placement and authoring |

### Agent tools after Phase 4

```
memory  web  git  file  shell  fabric   ← Layer 1 (infrastructure)
play    summarise  draft  search        ← Layer 2 (plugin tools)
```

The agent can now: summarise a file it just read, draft a message as part of a task, search the codebase while reasoning, and play music, all without leaving the agent loop.

---

## Known Gaps and Future Work

These are real integration limits discovered after Phase 4. Not bugs in the current features: things that don't yet exist.

### Voice + Agent are separate loops

`bob voice` is a standalone chat loop. It does not load agent tools. The agent cannot speak its output. The two share the same LLM stack but have no runtime bridge.

**Future:** A `bob voice --agent` mode that runs the full agent loop and pipes the final answer through piper TTS. Requires capping agent output length and stripping markdown before TTS.

### WebUI audio wiring is manual

Whisper (:8082) and piper (:8083) are running after `bob up`, but connecting them to Open WebUI requires a manual step: Admin Panel → Audio → set STT to `http://localhost:8082` and TTS to `http://localhost:8083`. This is not automated.

**Future:** A `bob setup-webui` command (or extend `bob up`) that POSTs the audio config to the Open WebUI settings API, so the wiring is automatic on first run.

### n8n → agent path is stubbed

`bob_agent_server.py` exposes the agent as a REST API on :8084. The README documents it. No n8n workflow actually calls it: the three existing workflows call LiteLLM and whisper directly. The bidirectional n8n → agent integration is available but unused.

**Future:** An n8n workflow that accepts a goal via webhook and calls `POST http://host.docker.internal:8084/v1/agent/completions`. Enables full agentic automation from n8n triggers (schedule, webhook, Discord message, etc).

---

## Architecture Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Memory backend | SQLite + BGE-M3 | BGE-M3 already pinned (0 extra VRAM), zero new infra. Qdrant at 50K+ entries. |
| Memory privacy | Always local BGE-M3 | Even `bob chat --pro` uses local embed for memory recall |
| TTS | piper (67 MB) | Zero network, ~200 ms. llama-tts (`bin/llama-tts.exe`) is quality upgrade already compiled |
| Pro routing | Through LiteLLM :8081 | Retry, budget cap, Langfuse tracing apply to all requests |
| Provider lock-in | None | `user.psd1` peers block supports any OpenAI-compatible endpoint |
| Plugin runtime | PowerShell scripts | No new dependencies for core. Plugins shell to Python when needed |

---

## Phase Summary

| Phase | What | Effort | Risk | Status |
|-------|------|--------|------|--------|
| 0 | Rebrand: `llm`→`bob` everywhere | 0.5 days | Low | Done |
| 1 | Identity: persona, REPL, smart routing, memory | 3 to 5 days | Low to Medium | Done |
| 2 | Senses: whisper STT, piper TTS, vision + full stack wiring | 4 to 6 days | Medium | Done |
| 3 | Agency: agent loop, Hermes 3 tool use, schedules, notifications, plugins | 1 to 2 weeks | Medium | Done |
| 4 | Cohesion: plugin and tool bridge, all plugins available to agent | 0.5 days | Low | Done |

Each phase is fully backward-compatible with the previous. The inference stack (llama-swap, LiteLLM, Open WebUI, Continue.dev, aider) is never modified; all integrations remain on their existing API endpoints.

---

## Critical Files by Phase

| File | Phase | Action |
|------|-------|--------|
| `scripts/bob.ps1` | 0 | New: copy of `llm.ps1` with renamed strings *(`llm.ps1` retired in M10)* |
| `scripts/install-cli.ps1` | 0 | Edit: emit `bob.cmd` (later: removes the retired `llm.cmd` shim) |
| `scripts/_models.ps1` | 0+1 | Edit: rename env var; add `Get-BobConfig` |
| `config/bob.psd1` | 0 stub → 1 full | New: persona, routing, memory, voice config |
| `tools/compose/docker-compose.yml` | 0 | Edit: rename `bob` literals |
| `scripts/bob.ps1` (chat case) | 1 | Edit: REPL + flag routing + memory injection |
| `scripts/bob_memory.py` | 1 | New: embed/store/recall/summarize |
| `scripts/bob-memory.ps1` | 1 | New: PowerShell wrapper (runs in `venv-litellm`) |
| `scripts/setup.ps1` | 1 | Edit: add onboarding flow |
| `scripts/build-whisper.ps1` | 2 | New: build whisper.cpp |
| `scripts/start-whisper.ps1` | 2 | New: start whisper-server on :8082 |
| `scripts/setup-voice.ps1` | 2 | New: download piper + voice model |
| `scripts/bob_core.py` | 3 | New: shared Python core: config, LLM client, memory access |
| `scripts/bob_loop.py` | 3 | New: agent loop (Hermes 3 XML tool-call format + OpenAI fallback) |
| `scripts/bob_clip.py` | 3 | New: fast web clip: fetch → summarise → store |
| `scripts/bob-agent.ps1` | 3 | New: background scheduler (BobAgent Windows Task) |
| `scripts/bob-toast.ps1` | 3 | New: Windows toast notification sender |
| `scripts/tools/tool_loader.py` | 3 | New: auto-discover tools from scripts/tools/ and plugins/ |
| `scripts/tools/memory.py` | 3 | New: memory_recall / memory_store tool |
| `scripts/tools/web.py` | 3 | New: web_search (SearXNG) / web_fetch tool |
| `scripts/tools/git.py` | 3 | New: git_status / git_log / git_diff tool |
| `scripts/tools/file.py` | 3 | New: file_read / file_write tool (path allowlist) |
| `scripts/tools/shell.py` | 3 | New: shell_run tool (always prompts) |
| `scripts/tools/fabric.py` | 3 | New: fabric_run tool (which-check) |
| `plugins/summarise/invoke.py` | 3 | New: `bob summarise` plugin |
| `plugins/draft/invoke.py` | 3 | New: `bob draft` plugin |
| `plugins/search/invoke.py` | 3 | New: `bob search` plugin |
| `plugins/play/invoke.ps1` | 3 | New: `bob play` plugin |
