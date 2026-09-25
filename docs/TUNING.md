# TUNING

Server configuration internals and tuning: the launch flags the models run with, how to estimate VRAM requirements, how to verify you're on the fast hardware path, and how to update the inference engine safely.

## How the server config is generated

The runtime config files (and the Open WebUI model table) are generated from [config/models.json](../config/models.json) every time you run `bob serve` or `bob gen`. Don't edit them by hand; edit `config/models.json` (or your `config/user.json` override) instead. The generators are Python functions in [scripts/tools/generate.py](../scripts/tools/generate.py).

- `config/llama-swap.yaml` (`gen_llama_swap`): local model routing, swap groups, KV cache flags
- `config/litellm.yaml` (`gen_litellm`): LiteLLM proxy model list, including pro models (API-backed peers)
- `config/continue/config.yaml` (`gen_continue`): Continue.dev model list plus per-role `systemMessage`
- Open WebUI model table (`gen_webui`): syncs system prompts from `config/models.json` into the Open WebUI SQLite database (`tools/webui-data/webui.db`); skipped if that db doesn't exist yet

The generated `llama-swap.yaml` has this structure:

- `macros:` reusable strings, mainly the shared `llama-server` command (`srv`) and KV cache flags (`kv`), referenced as `${name}` in model entries
- `models:` one named entry per model; `cmd` is the only required field; `${PORT}` is assigned automatically by the proxy
- `ttl: 0` on pinned models like `fim` and `embed`, which disables automatic unloading
- `filters.setParams` (and `setParamsByID` for aliases) enforces each role's sampling (temperature, top_p, top_k, min_p) server-side on every profile, so a client cannot override it. `config/models.json` sets sampling only in `setParams`; a `--temp`-style flag in a model's `flags` would be a client-overridable default, and `bob gen` warns on one
- `groups:` with `swap: true`, models in the same group evict each other, so only one large model is resident at a time

## Tunable defaults and personal overrides

The `defaults` block in `config/models.json` controls the server launch flags and port numbers. Both `gen_llama_swap` and `gen_litellm` read it whenever `bob gen` regenerates their configs.

| Key | Default | Effect |
|-----|---------|--------|
| `ngl` | `99` | GPU layers. Lower (e.g. `80`) to CPU-offload if a model overflows VRAM. |
| `flashAttn` | `true` | Enable Flash Attention. Required for KV cache quantization. |
| `kvQuantK` | `"q8_0"` | Key cache type (`--cache-type-k`). Safe across all GPU generations with flash-attn. Pre-Blackwell (sm_75 to 89, RTX 20/30/40): q5_1 saves ~30% more VRAM. Blackwell (sm_120+, RTX 50): sub-q8_0 types regress with flash-attn. |
| `kvQuantV` | `"q8_0"` | Value cache type (`--cache-type-v`). ~50% VRAM savings vs f16. Pre-Blackwell: q4_0 saves ~75% vs f16. |
| `kvQuant` | `""` | Legacy single-axis override. When non-empty, overrides both `kvQuantK` and `kvQuantV`. To disable KV quant entirely (Gemma models): set `kvQuantK = ""` and `kvQuantV = ""` instead. |
| `batch` | `512` | Logical batch size (`-b`). **Note:** value `512` emits no flag; llama.cpp then uses its default of 2048. Set a value > 512 to override explicitly. |
| `ubatch` | `512` | Physical GPU batch size (`-ub`). Raise to `1024` or `2048` to reduce kernel-launch overhead on long prompts. Must be ≤ effective batch size. Uses more peak VRAM at higher values. |
| `parallel` | `1` | Parallel request slots (`-np`). Set `>1` for multi-user or shared setups. |
| `threads` | `-1` | CPU threads for BLAS. `-1` = auto. Tune if you're offloading layers to CPU. |
| `noMmap` | `false` | Load model into heap RAM at startup (`--no-mmap`). Eliminates page faults on CPU-offloaded layers. Startup is slower; inference is smoother. Also supported per-model. |
| `mlockBig` | `false` | Apply `--mlock` to swap-group models (ponder/coder/chat). Pins CPU-resident pages in RAM. Windows: needs `SeLockMemoryPrivilege`. |
| `numa` | `""` | NUMA strategy (`--numa`). Options: `""` (off), `"isolate"`, `"distribute"`, `"numactl"`. On 7950X3D: try `"isolate"` first. |

Ports are **not** in this block; they live once in [config/defaults.json](../config/defaults.json) under `ports` (llama-swap `8080`, LiteLLM proxy `8081`, faster-whisper STT `8082`, piper `8083`, agent server `8084`, Open WebUI `3000`). Override a port with a top-level key of the same name in `config/user.json` (for example `"litellmPort": 8091`); `agentPort` and `mcpPort` go under `agent`.

Four runtime keys sit at the **top level** of `config/user.json` rather than in this block:

| Key | Default | Effect |
|-----|---------|--------|
| `bindHost` | `"127.0.0.1"` | Address every service binds: LiteLLM, Open WebUI, n8n, and the Docker services' published ports. llama-swap always stays on loopback (LiteLLM fronts it), and the voice servers bind `voiceBindHost`. Set `"0.0.0.0"` for LAN access; the LiteLLM key is then the only guard on the proxy. |
| `voiceBindHost` | `"127.0.0.1"` | Address faster-whisper (STT) and piper (TTS) bind. They have no authentication, so they stay on loopback even when `bindHost` opens the rest; set `"0.0.0.0"` only when another machine needs speech and the network is trusted. |
| `litellmKey` | `""` | Empty means generated: a random `sk-bob-...` key created on first use and kept in `data/secrets.json` (the environment or the OS keychain win if they hold one). Set it only to pin a key of your own. Run `bob gen` after a change so every client config carries it. |
| `langfuseEnabled` | `false` | Send LiteLLM traces to a local Langfuse (`bob services langfuse start`). |
| `n8nTimezone` | `"UTC"` | Timezone for n8n schedules. |

Every other service secret (Open WebUI, n8n encryption key, SearXNG, all Langfuse keys and passwords) is generated on first use the same way and has no config key.

**KV cache type options** (valid for both `kvQuantK` and `kvQuantV`): `f16`, `bf16`, `q8_0`, `q5_1`, `q5_0`, `q4_1`, `q4_0`, `iq4_nl`.

**Personal overrides without touching git**: create `config/user.json` (gitignored, copy from [config/user.json.example](../config/user.json.example)) to shadow any of these values without modifying the tracked file. It deep-merges over `config/models.json`:

```json
{
  "defaults": {
    "ngl": 80,
    "kvQuantK": "q8_0",
    "kvQuantV": "q8_0"
  }
}
```

- `ngl: 80` leaves VRAM headroom for other GPU apps.
- `kvQuantK`/`kvQuantV`: `q8_0` is the default; pre-Blackwell (RTX 20/30/40) try `q5_1`/`q4_0` for more VRAM savings.
- `kvQuant` is the legacy single-axis form that overrides both K and V.

Run `bob gen` after editing. Changes take effect on the next `bob serve`.

> Config resolves the same way on every OS: live from `config/defaults.json` deep-merged with `config/user.json`, and `config/user.json` is the documented override surface everywhere. There is no per-OS authoring source and no generated `data/config.json`.

### Agent behavior

The agent runtime defaults live in `config/defaults.json` under `runtime.agent`. Tools are **auto-discovered** from
`scripts/tools/*.py` and `plugins/*/tool.py`: there is no allowlist, and creating the file is the only
registration step. To exclude a tool without deleting it, add its name to `agent.disabledTools`
(a denylist). See [plugins/AUTHORING.md](../plugins/AUTHORING.md).

| Key | Default | Effect |
|-----|---------|--------|
| `agent.toolFormat` | `"hermes"` | Tool calling protocol. `"hermes"` = inject tool schemas in the system prompt and parse `<tool_call>` XML from the content (the Hermes-style format the Qwen chat templates use). `"openai"` = use the OpenAI `tools` parameter. |
| `agent.agency` | `"show"` | `"silent"` = run without output. `"show"` = print tool calls and results to stderr. `"confirm"` = prompt before each tool execution. |
| `agent.maxSteps` | `10` | Maximum tool-call iterations before stopping. |
| `agent.maxHistoryMsgs` | `40` | Sliding window by **message count**, the first-pass overflow guard. |
| `agent.maxContextTokens` | `0` | Token budget for one request. `0` = auto: the per-slot context window of the role being served (a local role's `ctx` divided across its slots, or a peer's `contextWindow`). An explicit value is capped at that window. The history gets what is left after the output reservation and the tool schemas; the oldest non-system turns drop first. The same bound applies to summaries, plan and verify turns, and plugin calls (`bob_core.complete`). When the window is small (4096 on cpu `chat` and 16gb `vision`), the tool schemas are compacted first, then non-core tools are left out largest first (core is `file_*`, `shell_*`, `memory_*`, `web_*`, `todo_*`) with one notice naming them; `max_tokens` shrinks to fit, and when fewer than 256 output tokens fit the run stops with a `context_overflow` error that says what to trim. |
| `agent.outputReserveTokens` | `1024` | Tokens reserved for a local role's reply and always sent as `max_tokens`, capped at half the window. A pro role requests its peer's `maxOutputTokens` instead (the role's override, else the peer's), with the same half-window cap. A reply cut off at this limit is marked as truncated, and a tool call in a truncated reply is never run. `--max` overrides it for one call. |
| `agent.maxDuplicateToolCalls` | `2` | How many times the loop runs the same tool call with the same arguments before it stops the model from repeating it. |
| `agent.maxToolResultTokens` | `1000` | Per-tool-result cap (~4 chars/token) applied before a result is appended to history, so one huge tool output can't blow the budget. |
| `agent.conversationPaging` | `false` | Persist the full transcript (incl. tool turns + one-shot CLI runs) to an owner-scoped store and offer the `conversation_search` tool to page dropped turns back. Grows `data/bob.db`. See [MEMORY.md](MEMORY.md#conversation-paging-recall-over-dropped-turns). |
| `agent.compactSchemasAfter` | `12` | Once more than this many tools are loaded, inject **compact** tool schemas (param descriptions dropped) so the fixed per-turn prompt doesn't grow unbounded with tool count. |
| `agent.requestTimeout` | `600` | Client-side LLM call timeout (s). Must be **≥** the litellm proxy's `request_timeout` (600) so thinking models (ponder) aren't cut off mid-response. |
| `agent.llmRetries` | `2` | Retries for a transient LLM error (5xx/timeout/conn) per step: total tries = this + 1. Covers the llama-swap model-swap race (a 500 "upstream command exited prematurely" on the first request after an idle-unload). Retried only before the first token surfaces. |
| `agent.llmRetryBackoffSec` | `2.0` | Base backoff (s) before a retry; escalates per attempt (2s, 4s, …) to give a restarting backend time to come up. |
| `agent.allowPrivateFetch` | `false` | When `false`, `web_fetch` blocks `file://`/non-http schemes and loopback/RFC-1918/link-local hosts (SSRF guard). Set `true` only if you deliberately need the agent to reach private hosts. |
| `agent.disabledTools` | `[]` | Tool names (stem/dir) to **exclude** from discovery. Denylist, not allowlist. |
| `agent.allowedReadPaths` | (repo root) | Paths `file_read` may access. Defaults to the repo root at runtime. Add more in `config/user.json`. **Secrets denylist:** `config.json`, `*.psd1`, `*.db`, `logs/`, `.env*` are refused even inside an allowed root. |
| `agent.allowedWritePaths` | `[]` | Paths `file_write` may access. Empty = write disabled. Opt in via `config/user.json`. The secrets denylist applies here too. |
| `agent.subAgentAllowPro` | `false` | Let `spawn_agent` send a sub-run to a cloud (`*-pro`) role. Off, a sub-agent can only use local roles. |
| `agent.computerUse.display` | `"virtual"` | Where computer use acts. `"virtual"` is enforced and Linux only: it needs `BOB_VIRTUAL_DISPLAY` pointing at a virtual X display, plus `xdotool` and `scrot` or ImageMagick. On Windows and macOS set `"host"`. See [SECURITY.md](SECURITY.md#computer-use-scriptstoolscomputerpy). |

Override any of these in `config/user.json` under the top-level `agent` key:

```json
{
  "agent": {
    "agency": "confirm",
    "allowedReadPaths": ["/home/you/projects", "/srv/code"],
    "disabledTools": ["shell", "fabric"]
  }
}
```

(On Windows, use Windows paths, e.g. `"allowedReadPaths": ["C:\\local-llm", "D:\\projects"]`.)

**Agent HTTP server (`bob agent serve`).** Exposes the agent loop as REST + SSE on `:8084`. Every
endpoint requires `Authorization: Bearer <token>`.

| Key | Default | Effect |
|-----|---------|--------|
| `agent.serveHost` | `"127.0.0.1"` | Bind address. Set `"0.0.0.0"` to expose on the LAN; **harden `allowPrivateFetch` first** (keep it `false`). |
| `agent.agentPort` | `8084` | Server port. |
| `agent.apiTokens` | `[]` | Per-client Bearer tokens, each `{"token": "sk-…", "owner": "alice"}`. Sessions are owner-scoped: a token only sees sessions its owner created (others 404). Bare strings still work (token = owner). |
| `agent.defaultOwner` | `"local"` | Owner id the `litellmKey` (and any unlabelled session) maps to. |
| `agent.acceptLitellmKey` | `true` | Accept the LiteLLM key as a bearer token on the agent API and the MCP HTTP transport. Set `false` so only `agent.apiTokens` entries and store-issued tokens open them. With it on, anyone holding the key (every generated client config, n8n, fabric, Open WebUI) gets an unscoped `agent.defaultOwner` identity; the hardened setup is scoped `agent.apiTokens` with this set to `false`. |
| `agent.sessionDbPath` | `"data/sessions.db"` | SQLite store for multi-turn sessions (WAL, created on first server start). |
| `agent.maxSessionTokens` | `0` | Per-session token budget; `0` = unlimited. Once reached, that session's completions return HTTP 402. |
| `agent.gitAllowedRoots` | `[]` | Extra repos `git_*` may read; the Bob repo root is always allowed. |
| `agent.logMaxBytes` / `logBackupCount` | `5000000` / `3` | Rotation for `logs/bob-agent.log`. |
| `agent.mcpEnabled` | `false` | Enable `bob agent mcp` (expose tools over MCP). Gates both transports. |
| `agent.mcpAllowTools` | `[]` | An MCP client has no one to approve a call, so approval-required and state-changing tools (`shell_run`, `file_write`, `music_play`, ...) are refused over MCP unless listed here. `spawn_agent` must be listed too, and a sub-run it starts is held to the same list. |
| `agent.mcpTransport` | `"stdio"` | `stdio` (the client spawns Bob) or `http` (Streamable HTTP, so a client on another machine can reach a running Bob). `bob agent mcp --http` overrides it for one run. |
| `agent.mcpPort` / `agent.mcpHost` | `8085` / `"127.0.0.1"` | Where the HTTP transport binds. It uses the agent API's token auth (scopes, rate limit, revocable store tokens). `0.0.0.0` exposes it to the LAN: give remote clients a dedicated `agent.apiTokens` entry rather than the litellm key. |
| `agent.mcpAllowedHosts` | `[]` | Extra `Host` header values the HTTP transport answers to (DNS-rebinding protection). Loopback and the bind host are always allowed; add the LAN address or DNS name a remote client dials. |
| `agent.mcpAllowedOrigins` | `[]` | Browser `Origin` values allowed against the HTTP transport. |
| `agent.mcpUrl` | `""` | The URL `bob gen` writes into the dsh drop-in, when the harness reaches Bob at something other than the local bind address. |

See [AGENT-SERVER.md](AGENT-SERVER.md) for the endpoint contract.

**Switching the agent model:** the `agent` role is an alias of the profile's chat model on 16gb, 24gb, 32gb, 12gb and cpu (with its own low temperature), and a Qwen3.5-4B on 8gb. If you point it at a model that expects OpenAI-format tool calling, set `agent.toolFormat = "openai"` in `config/user.json` and run `bob gen`.

### Memory (`memory.*`)

Bob's typed, owner/project-scoped memory store (SQLite + Qwen3-Embedding-0.6B). On by default. Keys live in
`config/defaults.json` under `runtime.memory`; override in `config/user.json` under the top-level
`memory` key. Full engine reference: [MEMORY.md](MEMORY.md).

| Key | Default | Effect |
|-----|---------|--------|
| `memory.enabled` | `true` | Master switch: CLI, agent tools, injection, consolidation. |
| `memory.autoRecall` | `false` | Recall + inject relevant memories **every turn**. Off = the agent recalls only via the `memory_recall` tool. |
| `memory.injectProfileAtStart` | `true` | Inject the stable profile block once at session start. |
| `memory.profileMaxTokens` | `200` | Cap on the profile block. |
| `memory.maxInjectedTokens` | `1200` | Total budget for injected memory (profile + autoRecall + `BOB.md`); over budget trims autoRecall → profile → `BOB.md`. |
| `memory.dbPath` | `data/bob.db` | Memory database (gitignored). |
| `memory.embedModel` | `embed` | Embedding role (Qwen3-Embedding-0.6B at `:8081`). A profile with no embed role (cpu) runs keyword-only memory. |
| `memory.recallK` | `5` | Max results per recall. |
| `memory.recallThreshold` | `0.35` | The recall gate: a memory is returned only with a raw cosine at or above this (in hybrid or rerank mode, a strong keyword match or a passing reranker score also counts). The blended rank only orders what passes. |
| `memory.dedupThreshold` | `0.92` | Cosine at/above which a store is a duplicate. |
| `memory.retrieval` | `"dense"` | `"dense"` (cosine) or `"hybrid"` (fuse BM25/FTS5 via RRF). |
| `memory.rrfK` | `60` | Reciprocal Rank Fusion constant (hybrid). |
| `memory.rerank` | `false` | Cross-encoder rerank of the fused candidates; needs a `reranking` model in the stack, loud-fails to hybrid if absent. |
| `memory.rerankThreshold` | (unset) | Optional gate on the cross-encoder score; unset, the reranker gates at `recallThreshold`. |
| `memory.rerankTopN` | `20` | Fused candidates re-scored by the reranker. |
| `memory.rerankBaseUrl` | `""` | Reranker endpoint override; empty = the local llama-swap `/v1/rerank`. |
| `memory.coreBlocks` | `{}` | `name → char cap` for agent-editable, always-injected core-memory blocks (the `memory_block` tool). Empty = off. |
| `memory.ranking.{wSemantic,wRecency,wType,wUsage,wSalience}` | `1.0/0.3/0.2/0.1/0.3` | Blended-rank term weights. |
| `memory.ranking.halfLifeDays` | `{profile/preference 36500, project 90, fact 365, episodic 30}` | Per-type recency half-lives. |
| `memory.typeWeights` | `{profile 1.0 … episodic 0.5}` | Per-type rank weights. |
| `memory.maxSummaryTokens` | `512` | Token budget for the consolidation LLM call: must clear a reasoning model's hidden-reasoning budget (a tight cap yields an empty completion). |
| `memory.autoConsolidate` | `true` | Consolidate durable facts when a session ends. |
| `memory.consolidateTimeout` | `30` | Seconds bounding the end-of-session consolidation call. |
| `memory.reconcileTopK` | `20` | Existing facts shown to the extractor for supersede decisions. |
| `memory.maxRows` | `2000` | Per-owner soft cap; excess lowest-salience/oldest rows pruned. |
| `memory.forgetAfterDays` | `{episodic: 180}` | Per-type TTL (profile/preference exempt). |
| `memory.transcriptMaxRows` / `transcriptMaxDays` | `20000` / `90` | Retention for the conversation-paging transcript: oldest rows past either limit are pruned. |
| `memory.scopeByProject` | `true` | Scope `project`-type facts per repo; `false` = one global pool. |
| `memory.projectFiles` | `true` | Read `BOB.md`/`AGENTS.md` project files at session start. |
| `memory.bobMdMaxTokens` | `4000` | Cap on the concatenated project files. |

Recall and injection are best-effort: a memory-server error is logged and skipped, never fatal to a run.

### Plugins

Plugins live in `plugins/<name>/` as an `invoke.py` (CLI + core logic) plus an optional `tool.py`
(agent-facing wrapper). They are discovered and dispatched automatically, with nothing to register.
Plugins are written in Python.

Python plugins read config through `bob_core.load_config()` (which resolves `config/defaults.json` +
`config/user.json` live on every OS) and can use any routing
role. To override the model a plugin uses for a specific invocation, most accept a `--role` flag (check
`--help` on each plugin, or run one directly with `bob --run <name> '{json}'`).

To disable a plugin without deleting it, add its directory name (for example `play`) to
`agent.disabledTools` in `config/user.json`. That is the only switch; renaming `invoke.py` does not
disable it. Plugins are Python only, and `bob <plugin> ...` runs `main(argv)` in its `invoke.py`.

## Per-model launch flags (Blackwell / 16GB)

These flags are assembled from the `defaults` block above. The resulting command for a 16 GB profile with default settings is:

```
-ngl 99 -c 16384 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0
```

`-ngl 99` loads all layers onto the GPU. Lower it only if a model plus its context spills past 16 GB; some layers then fall back to CPU, which is slower but functional.

`--flash-attn on` enables Flash Attention, required for KV cache quantization. This build needs the explicit `on`/`off`/`auto` value; a bare `--flash-attn` errors out.

`--cache-type-k q8_0 --cache-type-v q8_0` applies KV cache quantization (MODULE J): ~50% VRAM savings versus unquantized f16, with near-zero performance overhead across all GPU generations. On pre-Blackwell GPUs (RTX 20/30/40, sm_75 to 89), overriding to q5_1 keys and q4_0 values saves ~75% vs f16 (see [config/user.json.example](../config/user.json.example)). On Blackwell (RTX 50, sm_120+), sub-q8_0 types cause a significant prompt-processing regression with flash attention; q8_0 is the correct choice. Exception: Gemma models regress in quality with any KV quantization; set `kvQuantK = ""` and `kvQuantV = ""` in `config/user.json`.

## VRAM math

To decide whether a model will fit, estimate weight memory plus KV cache.

Weight memory is approximately the parameter count multiplied by the bytes per weight for that quantization level. Common values: Q4_K_M is about 0.56 GB per billion parameters, Q5 about 0.70, Q8 about 1.0.

KV cache adds roughly 1 to 2 GB at a 4k context window, or 3 to 5 GB at 32k. The default q8_0/q8_0 quantization cuts these figures by roughly 50% versus unquantized f16. On pre-Blackwell GPUs, q5_1 keys and q4_0 values cut by ~75% at the cost of minor quality loss.

For the models in this repo on 16 GB VRAM: the Qwen3.8-27B IQ3_XXS that serves chat, coder, ponder, writer and agent is 10.09 GB of weights, measured at 10950 MiB with its 40960-token q4_0 KV cache, so it sits entirely on the card. On 12 GB the MoE coder and ponder (Qwen3-Coder-30B-A3B, Qwen3.6-35B-A3B) exceed the card and keep some experts in system RAM (see [MoE expert offloading](#moe-expert-offloading-ncpumoe)); because only 3B parameters are active per token, generation is still fast.

For mixture-of-experts models generally, VRAM requirements are based on the total parameter count, not the active count. Active parameters affect compute speed but not the memory needed to load the model. An 80B model with 3B active parameters at Q4 still needs roughly 45 GB for the weight matrix.

## Verifying the fast path

The key health check is whether the engine uses Blackwell's optimized matrix multiplication (MMQ) rather than the slower cuBLAS fallback. The fallback is roughly five to six times slower on prefill.

```
bob bench
```

Expected numbers on an RTX 5080 with the default coder model: **pp512 ≈ 4400 t/s, tg128 ≈ 85 t/s**.

If prefill is around 1000 t/s, the engine is on the CPU / slow path. `bob diagnose` reports the engine tier and flags a GPU that is idle. Re-provision the engine:

```
bob build --force
```

The `--force` flag re-provisions even when a binary already exists. `bob build` uses the prebuilt, driver-only engine by default (no CUDA toolkit needed). `--from-source` builds the engine instead, auto-detecting your GPU architecture and the newest compatible CUDA toolkit (Blackwell requires 12.8+) and wiping the `build/` directory first. Add `--cpu` for a no-GPU build.

## Quality benchmarking (lm-eval)

`bob bench` measures *speed*: tokens per second. It says nothing about whether the model's answers are correct. `bob eval` fills that gap using [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness), an open-source harness that runs standardized tasks against any OpenAI-compatible endpoint and returns a reproducible accuracy score. lm-eval and its heavy deps live in their own lazily-provisioned virtualenv (`tools/venv-eval`); the first `bob eval` run creates it automatically.

**When to run:** after changing a model, switching quant levels (e.g. Q8_0 → Q4_K_M to reclaim VRAM), or bumping llama.cpp. A score drop reveals whether the change hurt answer quality, not just speed.

```
bob serve                        # endpoint must be running

# Quick smoke test first (~8 min, 100 samples)
bob eval coder gsm8k --limit 100

# Full benchmarks (sequential, parallel=1 server default)
bob eval coder gsm8k             # ~90 min, 1319 samples
bob eval coder humaneval         # ~3 hr
```

The syntax is `bob eval <role> [task] [--shots N] [--limit N]`. Results land in `results/eval-<role>-<task>-<timestamp>/`. Look for `exact_match,flexible-extract` (0.0 to 1.0); the flexible extractor finds the final number in the response, the right metric for generative math tasks.

**Reference scores for a 14B-class Q4_K_M coder** (a yardstick for spotting a regression, not a measurement of the current profiles):

| Task | What it measures | 5-shot | 0-shot |
|------|-----------------|--------|--------|
| `gsm8k` | math word problems | 0.72 to 0.82 | 0.60 to 0.72 |
| `humaneval` | code generation pass@1 | 0.60 to 0.72 | 0.50 to 0.65 |
| `mmlu` | general knowledge | 0.62 to 0.70 | 0.55 to 0.65 |

**Quant tradeoffs for the coder role:**

| Quant | Disk / VRAM | Accuracy vs Q8_0 | Notes |
|-------|------------|-----------------|-------|
| Q8_0 | 15 GB | baseline | Best quality; only fits on 24 GB+ cards |
| Q6_K | 11 GB | ~0.5% drop | Good balance for 24 GB cards |
| Q4_K_M | 8.4 GB | ~1 to 2% drop | Default for 16 GB; good quality/VRAM tradeoff |
| Q4_0 | 7.9 GB | ~4 to 6% drop | Not recommended, savings small, quality loss noticeable |

`bob eval` always passes `--apply_chat_template` to lm-eval, so the model sees its expected prompt format; without it, generative scores like gsm8k collapse.

**Shot count:** `--shots 0` (zero-shot) is the default and runs faster. `--shots 5` gives 3 to 5% higher scores but takes longer, and only makes sense when comparing against published 5-shot benchmarks.

## Updating the llama.cpp engine

New llama.cpp versions can add model support, fix bugs, or improve performance. Blackwell MMQ support can regress between commits, so always re-run the benchmark after a bump to confirm performance before committing the new pin.

The easiest path is the built-in update command, which fast-forwards the current branch, syncs submodules, reinstalls the venvs from their locks, updates the engine **only if llama.cpp actually moved** (a `bin/` snapshot, an atomic file swap, a binary verify, and a rollback on any error), and finishes with `bob doctor`. It does not rewrite `versions.lock`; run `bob lock` yourself after a deliberate bump:

```
bob update
```

To pin a specific release tag instead of the latest commit, pass `--tag`:

```
bob update --tag <release-ref>
```

Re-run the benchmark after any bump:

```
bob bench
```

If the numbers look good, commit the updated submodule pointer:

Linux:
```bash
git add external/llama.cpp
git commit -m "bump llama.cpp to <commit>"
```

Windows:
```bat
git add external\llama.cpp
git commit -m "bump llama.cpp to <commit>"
```

To check out a specific commit or tag by hand instead of `bob update`:

Linux:
```bash
cd external/llama.cpp
git fetch origin
git checkout <new-commit-or-tag>
cd ../..
bob build --force
bob bench
```

Windows:
```bat
cd external\llama.cpp
git fetch origin
git checkout <new-commit-or-tag>
cd ..\..
bob build --force
bob bench
```

If performance regressed, check back to the previous known-good commit.

## Adding or swapping a model

All model configuration lives in `config/models.json`. To add a model or swap the backing GGUF for an existing role:

1. Edit the model entry under the profile: set `repo` and `path` (the HuggingFace source), `gguf` (the local filename), `ctx` (context size), and any optional flags (`kv`, `flags`, `setParams`, `ttl`, `pinned`, `embedding`).
2. Optionally add its name to `group.members` if it should swap with the other large models.
3. Run `bob fetch` to download the new GGUF, then `bob serve` to pick it up. Use `bob fetch --list` first to preview what will be downloaded.

To add an entirely new VRAM tier, add a new key under `profiles` in `config/models.json` and switch to it with `bob profile <name>`. See [USAGE.md](USAGE.md#managing-model-profiles).

## Voice response tuning

Voice-specific settings live in `config/defaults.json` under `runtime.voice`; override them per-machine in `config/user.json` under the top-level `voice` key. They only affect `bob voice` (the continuous voice loop); `bob chat` is unaffected.

| Key | Default | Effect |
|-----|---------|--------|
| `silenceSec` | `1.5` | Seconds of mic silence before recording stops. If Bob cuts you off while you're still speaking, raise to `2.0`. |
| `sttModel` | `"small"` | Whisper model size: `tiny.en`, `base.en`, `small`, `medium`. Larger is more accurate but slower. Re-run `bob setup-voice` after changing to download the new model file. |
| `ttsVoice` | `"en_GB-alan-medium"` | Piper voice (ONNX file). Re-run `bob setup-voice` after changing to fetch a new voice. |

Override in `config/user.json`:

```json
{
  "voice": {
    "sttModel": "medium",
    "silenceSec": 2.0
  }
}
```

The voice loop reuses the **same** agent turn (and persona) as text chat; there is no separate voice system prompt. To keep replies speech-friendly, it runs `bob_voice.format_for_speech()` ([scripts/bob_voice.py](../scripts/bob_voice.py)), a post-processor that strips markdown and typographic symbols before the text reaches piper. The chain is: model output → strip `<think>` blocks → `format_for_speech` → piper TTS.

**Piper HTTP server (for Open WebUI TTS):** `bob piper` starts a FastAPI wrapper around piper on `:8083` that accepts OpenAI-compatible `POST /v1/audio/speech` requests. The OpenAI `voice` parameter is accepted but ignored: piper always uses the configured `ttsVoice` ONNX file. Wire it in Open WebUI: Admin Panel → Audio → Text-to-Speech Engine → `http://localhost:8083`.

---

## System prompts

**Bob's persona (agent loop + interactive shell)**: the base system prompt lives in
the neutral runtime layer at `config/defaults.json` → `runtime.persona.systemPrompt` (read on every OS
by the Python resolver). The shipped default is
intentionally short and general: it introduces Bob and tells the model to save durable facts about you
with the memory tools.

Give Bob a specific role or personality by editing that value in `config/defaults.json`, or override it
per-machine without touching the tracked file by adding a top-level `persona` key to `config/user.json`:

```json
{
  "persona": {
    "systemPrompt": "You are Bob, a terse senior engineer. Skip pleasantries."
  }
}
```

Run `bob gen` (or any `bob` command) to pick up the change. Keep it to the persona itself: tool usage and
memory behaviour are guided by the tool descriptions, not the system prompt.

The remaining two surfaces are separate:

**Open WebUI**: driven from `config/models.json`. The top-level `prompts` key holds per-role prompts for local models:

```json
{
  "prompts": {
    "coder": "You are a skilled software engineer. Be direct. No preambles.",
    "chat": "Be concise. /no_think"
  }
}
```

`bob gen` calls `gen_webui`, which merges these into the Open WebUI SQLite database: it sets each Bob model's system prompt and leaves every other field and model you edited in WebUI alone. Override in `config/user.json` using the same key names. `ponder` has its own prompt (think it through, state assumptions, weigh alternatives, conclude).

A pro (cloud) role uses the same `prompts[role]` as its local role, so `coder-pro` gets the `coder` prompt. To give one pro role a different prompt, set `systemPrompt` on it in the object form:

```json
{
  "peers": {
    "deepseek": {
      "pro": {
        "coder":  { "model": "deepseek-v4-flash", "systemPrompt": "You are an expert software engineer. Be direct. No preambles." },
        "ponder": { "model": "deepseek-v4-pro", "maxOutputTokens": 65536 }
      }
    }
  }
}
```

The peer's `maxOutputTokens` (32768 for DeepSeek) is the output cap each `*-pro` route defaults to in `litellm.yaml`, and a role can override it, as `ponder` does above. It stops a runaway generation without cutting off a long answer or a large tool call, and a client that sends its own `max_tokens` still wins. Local roles have no cap in `litellm.yaml`: they stop when the model finishes or the window fills. Bob's own agent loop always sends `max_tokens`: a pro role asks for this same `maxOutputTokens`, a local role for `agent.outputReserveTokens`, and both are capped at half the window. A role can also be a bare model string; only the object form takes `systemPrompt` or an override.

**Continue.dev**: `config/continue/config.yaml` is **generated** by `gen_continue` from the same `prompts` in `config/models.json`, then linked into `~/.continue/` by setup. Change the role prompts in `config/models.json` (or `config/user.json`) and run `bob gen` rather than editing the generated file, which is overwritten on the next regenerate.

## KV cache quantization

`--cache-type-k` and `--cache-type-v` control how the key and value tensors of the attention cache are stored. Keys drive attention score computation and are more sensitivity-critical; values are weighted and summed, so they tolerate more aggressive quantization.

Profile default: `--cache-type-k q8_0 --cache-type-v q8_0`, ~50% KV VRAM savings versus unquantized f16, with near-zero performance overhead on all GPU generations.

A single model can override the profile-wide setting with `kvQuantK` / `kvQuantV` in
`config/models.json`. The `16gb` profile does exactly that: its 27B is held entirely in VRAM at a long
context, which is only affordable at `q4_0`, while every other model on the tier keeps `q8_0`.

**VRAM impact at ctx=16384, Qwen3.5-9B (40 layers, 8 KV heads, d_head=128):**

| K type | V type | KV VRAM estimate | Notes |
|--------|--------|-----------------|-------|
| f16 | f16 | ~2.7 GB | llama.cpp default (no quant) |
| q8_0 | q8_0 | ~1.3 GB | **Current default. Safe on all GPUs.** |
| q5_1 | q4_0 | ~0.75 GB | Pre-Blackwell only (RTX 20/30/40). Regresses on Blackwell + flash-attn. |
| q4_0 | q4_0 | ~0.65 GB | Maximum savings; same pre-Blackwell caveat. |

Pre-Blackwell users (RTX 20/30/40, sm_75 to 89) can override for more VRAM savings:

```json
{
  "defaults": { "kvQuantK": "q5_1", "kvQuantV": "q4_0" }
}
```

**Exception:** Gemma models regress in quality with any KV quantization. Set `kvQuantK = ""` and `kvQuantV = ""` for any Gemma model entry.

## RAM preloading (--no-mmap)

`noMmap: true` on a model entry (or in `defaults` to apply globally) forces llama.cpp to read the full model file into heap memory at startup instead of using memory-mapped I/O (the default).

Under mmap, CPU-offloaded layer weights are read from disk on demand (page faults). For the 12gb tier's MoE coder and ponder, whose offloaded experts live in system RAM, every access to those layers is a potential disk seek during inference. With `--no-mmap`, all weights sit in heap RAM after startup: zero disk I/O during inference.

**Trade-off:** startup takes roughly 1 s per GB of model size (the 17 GB 12gb ponder = ~17 s on first load after a cold start). After that, the heap pages can be retained in RAM across swaps if `--mlock` is also set.

Set per-model in `config/models.json` (already done for the 12gb coder and ponder, which have `"noMmap": true`), or globally in `config/user.json`:

```json
{
  "defaults": { "noMmap": true }
}
```

Verified: `--no-mmap` is fully supported on Windows (via `SetFilePointerEx` + `ReadFile`) and on Linux (native `mmap`/`read`).

## Memory locking (--mlock)

`--mlock` is already applied to the always-resident models: `embed` on every GPU profile, and `fim` where it is pinned (8gb, 12gb, 24gb, 32gb; on 16gb `fim` joins the swap group and is not locked). Setting `mlockBig: true` in defaults extends locking to swap-group models (ponder, coder, chat).

Combined with `--no-mmap`, mlock fully pins model weights in physical RAM: no disk seeks, no OS eviction to the pagefile. Inference latency for CPU-offloaded layers becomes consistent rather than occasionally spiky.

**Windows requirement:** `SeLockMemoryPrivilege`. Without it, llama-server logs a warning and continues without locking. Grant it (one-time UAC prompt):

```
bob mlock --grant
```

After the UAC prompt completes, **restart your terminal**, then `bob serve`. `bob diagnose` confirms the privilege is active. Manual fallback: `secpol.msc` → Local Policies → User Rights Assignment → "Lock pages in memory" → add your user account → restart terminal.

**Linux requirement:** a sufficient memlock limit (`RLIMIT_MEMLOCK`). Raise it with `ulimit -l unlimited` for the session, or a `memlock` entry in `/etc/security/limits.conf` for persistence. There is no Windows-style privilege to grant, so `bob mlock --grant` only prints guidance on Linux.

Enable in `config/user.json`:

```json
{
  "defaults": { "mlockBig": true }
}
```

**RAM budget:** locking the 17 GB 12gb ponder on a 64 GB system leaves ~45 GB free (safe). Do not enable on systems with 16 GB RAM.

## NUMA strategy (7950X3D and multi-CCD CPUs)

`--numa` controls how llama.cpp allocates threads and memory when CPU layer offloading is active. The Ryzen 9 7950X3D exposes two CCDs as two NUMA nodes: CCD0 has the 96 MB V-Cache; CCD1 has 32 MB L3.

| Strategy | Effect |
|----------|--------|
| `""` (off, default) | OS decides thread placement |
| `"isolate"` | Confine all inference threads to the starting NUMA node (usually CCD0 = V-Cache) |
| `"distribute"` | Spread threads across all NUMA nodes: more parallelism, more cross-CCD traffic |
| `"numactl"` | Use numactl CPU map (Linux-specific; no-op on Windows) |

**Recommendation for 7950X3D:** start with `"isolate"`. The 96 MB V-Cache on CCD0 improves data locality for CPU-offloaded layer computation. If prefill throughput matters more than latency, try `"distribute"` and compare with `bob bench`.

Enable in `config/user.json`:

```json
{
  "defaults": { "numa": "isolate" }
}
```

## Where a profile's VRAM actually goes

Three llama-server defaults cost more VRAM than any quantization choice, and all three are now pinned
explicitly by the generator. Measured on a 16 GB RTX 5080:

| Default | What it does | Cost |
|---|---|---|
| no `-c` | reserves the model's full trained context window | 0.6B embedder: **4.8 GB** (1.4 GB at `-c 2048`); 0.6B reranker: **5.7 GB** |
| `--parallel 4` | four request slots, each with its own KV *and*, on a hybrid attention/SSM model, its own recurrent-state cache | **~450 MiB** on the 27B; more on a dense model |
| `-ub 512` | compute buffer sized for a 512-token micro-batch | **~226 MiB** per 0.6B helper |

So every model in `config/models.json` sets `ctx`, embedders and rerankers included; the `srv` macro
always emits `-np 1`; and the embedder passes `-ub 128`. The reranker runs at `-c 1024 -ub 1024 -b 1024`
instead, because a rank-pooling reranker rejects any query plus document pair longer than its ubatch.
Its smaller KV partly offsets the larger compute buffer, but that net figure has not been measured on a
card yet; the 1198 MiB in the profile notes is from the older `-c 2048 -ub 128` setting.

The other trap is llama-swap's grouping. Any model Bob does not list as a swap member lands in
llama-swap's implicit default group, which defaults to `exclusive: true` — so a single embedding call
for a memory lookup would unload the chat model. Bob emits a named `resident` group
(`swap: false, exclusive: false, persistent: true`) for `embed` and `rerank` so they coexist instead.

## MoE expert offloading (`nCpuMoe`)

The 12gb ponder model (Qwen3.6-35B-A3B: 36B total, 3B active per token) exceeds that card at Q4_K_M. Bob keeps
it on the card with **`--n-cpu-moe N`**, which keeps the Mixture-of-Experts weights of the first N layers
in system RAM while everything else stays on the GPU at `-ngl 99`. Because only ~3B experts activate per
token, an A3B model streams those from RAM with little speed loss. The `16gb` tier no longer needs this:
its 27B fits the card whole. `12gb` still uses it.

Set it per profile in `config/models.json`:

```json
{
  "profiles": {
    "12gb": { "ponder": { "nCpuMoe": 34 } }
  }
}
```

**Tuning:** lower `nCpuMoe` keeps more experts on the GPU (faster, more VRAM); raise it to fit a tighter
card. `--cpu-moe` (all experts in RAM) is the safest floor for very small VRAM. Benchmark with `bob bench`
after a change and watch `tg128` (generation tokens/s) plus VRAM headroom.

> **Why this is needed:** older llama.cpp auto-spilled excess layers to CPU at `-ngl 99`. Current
> llama.cpp does not: with an explicit `-ngl 99` it refuses to auto-fit and aborts with an out-of-memory
> error for a model larger than VRAM. `nCpuMoe` is the deterministic replacement (and the path to running
> even larger MoE models, e.g. an 80B-A3B, on a single small card).

Run `bob gen && bob serve && bob bench` after editing.

**Effect of --no-mmap + --mlock on CPU-offloaded layers:**

| State | CPU layer access latency |
|-------|--------------------------|
| Default (mmap, no mlock) | Disk seek on first access per page; OS may evict to pagefile |
| `noMmap: true` | Heap RAM from startup; no disk seeks during inference |
| `noMmap + mlockBig` | Heap RAM, pinned: OS cannot evict; consistent low latency |

The 7950X3D's V-Cache (96 MB L3) reduces main-memory pressure for CPU-resident layer data. Pair it with `numa: "isolate"` to keep those accesses on the V-Cache CCD.

## Speculative decoding

Two kinds ship. On **24gb and 32gb** the 27B chat model runs its own multi-token-prediction head as the draft (`--spec-type draft-mtp --spec-draft-n-max 2` in its `flags`), which needs no second model. On **8gb**, `draftRole: "fim"` on the `coder` model uses the always-resident `fim` model as a draft (16gb, 12gb and cpu use no draft): `fim` proposes N tokens, and `coder` verifies them in a single forward pass. When the draft is correct (roughly 70 to 80% of the time on coding tasks), the large model accepts all N tokens without spending compute on each one, effectively multiplying generation throughput.

Expected speedup: **20 to 40% on generation-heavy tasks** (autocomplete, inline edits, code generation). No quality change: the large model rejects incorrect draft tokens and falls back to its own output.

**Tokenizer constraint:** only the `coder` → `fim` pairing is safe for a draft model. The Qwen3.5 `chat` and `ponder` models have a different tokenizer vocabulary from Qwen2.5 (used by the 8gb `coder` and `fim`). Mismatched vocabularies cause the large model to reject nearly all draft tokens, eliminating the speedup and potentially producing garbled output. Do not add `draftRole` to `chat` or `ponder`.

**Disable:** Remove `draftRole` from the 8gb `coder` entry (or the `--spec-type` flags from a 24gb/32gb `chat`) in `config/models.json`, then run `bob gen`.

**Verify it's active:** run `bob gen`, then grep the generated config for the draft-model flag.

Linux:
```bash
grep -- '-md ' config/llama-swap.yaml
```

Windows:
```bat
findstr "-md " config\llama-swap.yaml
```

On 8gb the `coder` cmd should contain `-md ${env.LLAMA_LOCAL_ROOT}/models/qwen-coder-1.5b-q8_0.gguf -ngld 99`. The `ponder` and `chat` cmds must not contain `-md`.
