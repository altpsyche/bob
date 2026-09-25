# Changelog

All notable changes to Bob are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions carry a `versions.lock`
(pinned submodules + deps + toolchain + model manifest) so a release is reproducible.
`bob version` reports the running release; `bob update` moves between releases lockfile-to-lockfile,
rebuilds only what changed, verifies, and rolls back on failure.

## [Unreleased]

## [2.0.1] (2026-09-25)

### Fixed
- **Prebuilt engines run on any x86-64 CPU from the last decade, not just ones like the build runner's.**
  A distribution build (`bob build --dist`, what the release publishes) compiled its CPU code for the
  GitHub runner's own instruction set, so a downloaded engine could crash with an illegal instruction on
  an older CPU. Distribution builds now target a fixed baseline (`-DGGML_NATIVE=OFF`: AVX2, FMA, F16C,
  no AVX-512); a local source build stays native. Published rows carry a build `recipe`, and a release
  rebuilds rather than reuses engines whose recipe differs. [scripts/tools/build.py](scripts/tools/build.py),
  [.github/scripts/pack_engine.py](.github/scripts/pack_engine.py).
- **`bob agent <goal>` exits 1 when the run fails.** It printed the error and exited 0 with no answer, so a
  script or CI job could not tell a failed run from a quiet one.
- **Secrets are stored reliably on Windows.** Windows refuses to rename over `data/secrets.json` while
  another process has it open, so a first-use write could fail; it is now retried briefly.
- **Pushing a release tag creates its GitHub release.** The engine publish jobs upload into a GitHub release
  but nothing created one, so a tag without a hand-made release page failed every upload. A `release-page`
  job now creates it from the tag's CHANGELOG section when it is missing and leaves an existing one alone.
  [.github/workflows/ci.yml](.github/workflows/ci.yml).

## [2.0.0] (2026-09-25)

### Security
- **Every service now binds loopback unless you say otherwise.** One top-level `bindHost` (default
  `127.0.0.1`) is the address LiteLLM (`--host`), Open WebUI and n8n (`N8N_LISTEN_ADDRESS`) listen on,
  and the Docker services publish their ports on `${BIND_HOST}`. llama-swap always stays on loopback,
  since LiteLLM fronts it. faster-whisper and piper have no authentication, so they bind their own
  top-level `voiceBindHost` (default `127.0.0.1`), and opening `bindHost` never exposes them. **Action:** LAN access now needs
  `"bindHost": "0.0.0.0"` in `config/user.json`. [scripts/tools/stack.py](scripts/tools/stack.py),
  [tools/compose/docker-compose.yml](tools/compose/docker-compose.yml)
- **No more well-known keys.** The LiteLLM key is generated per machine on first use (`sk-bob-...`) by
  `osenv.ensure_secret`, which keeps it in `data/secrets.json` (mode 0600) unless the environment or the
  OS keychain already holds one; an explicit `litellmKey` in `config/user.json` still wins, and `sk-local`
  is neither the default nor accepted. The Open WebUI session secret, the n8n encryption key, the SearXNG
  secret and every Langfuse secret (project keys, database passwords, and the admin password,
  `langfuseAdminPassword`) are generated the same way; an existing Postgres or n8n data dir keeps the
  value it was created with. Generated configs that carry the key (`litellm.yaml`, Continue, aider,
  dsh) are written 0600. `litellm.yaml` itself no longer holds the key: it reads
  `master_key: os.environ/LITELLM_MASTER_KEY`, and Bob passes the key in that variable when it starts the
  proxy. Secrets are written under an inter-process lock (`osenv.file_lock`) through 0600 temp files, so
  two processes generating at once agree on one value. **Action:** clients Bob starts or configures pick
  up the new key on their own (see the upgrade self-heal under Fixed); clients Bob does not configure (a
  phone, another machine, your own scripts) need the new key from `data/secrets.json` or the keychain.
  [scripts/osenv.py](scripts/osenv.py), [scripts/bob_core.py](scripts/bob_core.py),
  [scripts/tools/generate.py](scripts/tools/generate.py), [scripts/tools/stack.py](scripts/tools/stack.py)
- **The file tools cannot read a generated key.** `file_read` and `search_code` refuse the configs that
  embed or wire the LiteLLM key (`litellm.yaml`, `continue/config.yaml`, `aider/*`, `dsh/*`, listed once in
  `bob_fsguard.KEY_BEARING`), any `config` under `tools/n8n-data/`, `.webui_secret_key`, dsh's
  `.credentials.yaml` and `secrets.json`. [scripts/bob_fsguard.py](scripts/bob_fsguard.py)
- **One approval gate for every front door.** The agent loop, the shell, skill steps, `bob --run` and
  MCP all dispatch through `bob_permissions.dispatch_with_approval`. `bob --run` asks on a terminal and
  fails closed when piped; a skill's tool steps can no longer run a gated tool unasked. In the shell,
  **a** now approves only that exact call and **t** approves the tool for any arguments.
  [scripts/bob_permissions.py](scripts/bob_permissions.py), [scripts/bob/shell.py](scripts/bob/shell.py)
- **MCP refuses what it cannot ask about.** Approval-required and state-changing tools are refused over
  MCP unless listed in `agent.mcpAllowTools`, and the HTTP transport uses the agent API's full token auth
  (scopes, per-owner rate limit, revocable store tokens). `spawn_agent` must be listed too, and a sub-run
  it starts is held to the same list (`RunContext.unattended_allow`). `agent.acceptLitellmKey = false`
  stops the LiteLLM key from opening the agent API and MCP. With it on, anyone holding that key (every
  client config, n8n, fabric, Open WebUI) gets an unscoped `agent.defaultOwner` identity; the hardened
  setup is scoped `agent.apiTokens` with `acceptLitellmKey` false. [scripts/bob_mcp_server.py](scripts/bob_mcp_server.py),
  [scripts/bob_authstore.py](scripts/bob_authstore.py)
- **Tool arguments can no longer become options.** `git_diff` puts its file after `--` and runs with
  `--no-ext-diff --no-textconv`, a repo path starting with `-` is refused, and `git_log`'s count must be
  an integer; `search_code` keeps the query behind `-e` or `/c:` and obeys the same read allowlist and
  secrets denylist as `file_read`. [scripts/tools/git.py](scripts/tools/git.py),
  [plugins/search/invoke.py](plugins/search/invoke.py)
- **State-changing tools are declared as such.** `file_write`, `music_play` and `music_stop` are
  mutating, so the permission policy, checkpointing and MCP's refusal apply. A tool whose name another
  module already registered is refused rather than shadowing it, and `spawn_agent` can reach a cloud
  role only with `agent.subAgentAllowPro`, with a role's tool scopes carried into the sub-run.
  [scripts/tools/tool_registry.py](scripts/tools/tool_registry.py),
  [scripts/tools/spawn_agent.py](scripts/tools/spawn_agent.py)
- **Computer use's virtual display is enforced.** `agent.computerUse.display: "virtual"` now drives only
  the display in `BOB_VIRTUAL_DISPLAY` (Linux, with `xdotool` and `scrot` or ImageMagick), and refuses
  every action without one instead of touching the real desktop. **Action:** on Windows and macOS set
  `display: "host"`. [scripts/tools/computer.py](scripts/tools/computer.py)
- **n8n workflows no longer see Bob's key.** The LiteLLM nodes use a **Bob LiteLLM** Header Auth
  credential that `bob services n8n start` imports (and re-imports only when the key or n8n's encryption
  key changes), instead of reading `$env.BOB_LITELLM_KEY`; n8n's environment is not exposed to
  workflows. [scripts/tools/stack.py](scripts/tools/stack.py),
  [tools/n8n-workflows/README.md](tools/n8n-workflows/README.md)
- **CI actions are pinned by commit SHA**, with a skip guard and release gates on the publish jobs.
  [.github/workflows/ci.yml](.github/workflows/ci.yml)
- **The key-file denylist ignores case.** On a case-insensitive filesystem (APFS, NTFS)
  `CONFIG/Continue/config.yaml` opens the real file, so `bob_fsguard` now compares the `KEY_BEARING`
  paths and n8n's `config` casefolded. [scripts/bob_fsguard.py](scripts/bob_fsguard.py)
- **A stale LiteLLM pidfile can no longer kill another process.** Before restarting a proxy that rejects
  Bob's key, the pid in `logs/litellm.pid` must name Bob's own LiteLLM (`osenv.find_managed_processes`);
  after a reboot it can name anything, and that process is now reported as a foreign proxy and left
  running. [scripts/tools/stack.py](scripts/tools/stack.py)
- **A malformed `secrets.json` is never overwritten.** It used to read as empty, so the next generated
  secret rewrote the file with only itself, wiping the n8n, Open WebUI and Langfuse secrets (a new n8n
  key cannot decrypt n8n's stored credentials). It is now moved aside to `secrets.json.corrupt-<timestamp>`
  (0600) and `osenv.SecretsFileCorrupt` says how to restore it. On Windows a read that races the atomic
  replace is retried. [scripts/osenv.py](scripts/osenv.py)
- **`schedule_run` is held to the calling surface's allow list.** Run as a tool, the scheduled loop now
  inherits the caller's approver, owner, role scopes and `agent.mcpAllowTools` set, the way `spawn_agent`
  does, and it is gated on MCP like `spawn_agent` (`UNATTENDED_GATED`).
  [scripts/tools/schedule.py](scripts/tools/schedule.py), [scripts/bob_permissions.py](scripts/bob_permissions.py)
- **LiteLLM never starts without its master key.** Without `LITELLM_MASTER_KEY` LiteLLM only logs a
  warning and serves unauthenticated, so Bob's launcher refuses to start it with no key, and the generated
  `litellm.yaml` header warns that a hand run (`litellm --config config/litellm.yaml`) needs the variable
  exported. [scripts/tools/stack.py](scripts/tools/stack.py), [scripts/tools/generate.py](scripts/tools/generate.py),
  [docs/SECURITY.md](docs/SECURITY.md)

### Added
- **Budgets follow the model that serves the request.** `agent.maxContextTokens = 0` (the new default)
  uses the per-slot window of the role being served, minus the output reservation and the tool schemas;
  an explicit value is capped at that window, and bounds summaries, plan and verify turns and plugin
  calls as well (`bob_core.complete`). `max_tokens` is always sent: a pro role asks for its peer's
  `maxOutputTokens` (the role's override, else the peer's), a local role for `agent.outputReserveTokens`,
  both capped at half the window. A reply cut off at it is marked as truncated, and a tool call in a
  truncated reply is never run. Session budgets charge the run's real token usage. A small window (4096
  on cpu `chat` and 16gb `vision`) compacts the tool schemas, then leaves out non-core tools largest
  first (core is `file_`, `shell_`, `memory_`, `web_`, `todo_`) with one notice naming them and pointing
  to `agent.disabledTools`; `max_tokens` shrinks to fit, and when under 256 output tokens fit the run
  ends with a `context_overflow` error that says what to trim.
  [scripts/bob_loop.py](scripts/bob_loop.py), [scripts/bob_core.py](scripts/bob_core.py)
- **The CPU profile degrades clearly.** `coder`, `ponder`, `writer` and `agent` fall back to `chat` with
  a notice, image input is refused with a message when the profile has no vision model, when
  `vision.enabled` is false, when a pinned local role is text-only (neither `supportsVision` nor an
  `mmproj`), or when `--pro` routes to a peer without `supportsVision`, and memory runs
  keyword-only without an embed role. `voice.enabled = false` now gates `bob voice` and `/voice`.
  [scripts/bob_core.py](scripts/bob_core.py)
- **The STT server speaks OpenAI.** `POST /v1/audio/transcriptions` sits alongside `/inference`, so Open
  WebUI and the n8n voice workflow reach it directly. [scripts/faster_whisper_server.py](scripts/faster_whisper_server.py)
- **Memory commands do what they say.** `bob memory clear` wipes memories, core blocks, the transcript
  and their FTS indexes; `forget --query` shows the match and asks (`--yes` skips); `forget --session`
  covers the transcript; a forgotten fact leaves the profile and can be stored again. The transcript is
  bounded (`memory.transcriptMaxRows` 20000, `memory.transcriptMaxDays` 90), the DB runs in WAL mode,
  and `bob memory --db PATH` and `bob code index --rebuild` are new. [scripts/bob_memory.py](scripts/bob_memory.py),
  [docs/MEMORY.md](docs/MEMORY.md)
- **aider and fabric are opt-in tools.** `./setup.sh --with-aider` or `bob aider-setup` creates
  `tools/venv-aider` and generates `config/aider/.aider.conf.yml` plus a model-metadata file with each
  role's per-slot window; `bob aider` passes it with `--config`, and a `~/.aider.conf.yml` symlink into
  the repo is removed. `--with-fabric` or `bob fabric-setup` builds fabric and adds Bob as its LiteLLM
  vendor without overwriting the rest of `~/.config/fabric/.env`, and `fabric_run` always passes
  `--vendor LiteLLM --model coder`. **Action:** run one of them if you use aider or fabric.
  [scripts/bob/kernel.py](scripts/bob/kernel.py), [scripts/tools/build.py](scripts/tools/build.py)
- **`bob <plugin> ...` runs a plugin's CLI**, `main(argv)` in `plugins/<name>/invoke.py`. Plugins are
  Python only, and `agent.disabledTools` (by directory name) is the one way to disable one.
  [scripts/bob/cli.py](scripts/bob/cli.py), [plugins/AUTHORING.md](plugins/AUTHORING.md)
- **Langfuse runs v3 with nothing to copy.** Web and worker with Postgres, ClickHouse, Redis and MinIO;
  the project is created with the generated key pair, LiteLLM gets the same pair, and the agent's OTLP
  export defaults to the local `/api/public/otel/v1/traces` with Basic auth from those keys.
  [tools/compose/docker-compose.yml](tools/compose/docker-compose.yml), [scripts/bob_tracing.py](scripts/bob_tracing.py)
- **One 27B model now serves five roles, and it fits a 16 GB card whole.** `chat` on every GPU tier is
  Qwen3.8-27B in IST-DASLab's GSQ-RCO packing, and `coder`, `ponder`, `writer` and `agent` are
  `aliasOf: "chat"` — one download, one loaded llama-server, five names. The packing is why: GSQ
  quantizes each tensor at its own bit depth and RCO assigns those depths under a size budget, so the
  IQ3_S build matches the BF16 base exactly on AIME25 (100.00) and LiveCodeBench v6 (85.71) at 12 GB,
  and the 10 GB IQ3_XXS still holds 100.00 / 84.57 — both ahead of the uniform Unsloth quants Bob shipped
  before, at a smaller size. What that replaces on the 16gb tier: a 18.6 GB MoE coder with 24 layers of
  experts in system RAM, a 22 GB MoE reasoner with 32, and a 23 GB *dense* writer that llama.cpp had to
  fit around the card. All three are gone; the tier now downloads 18.6 GB total instead of ~90 GB, holds
  its model entirely on the GPU, and runs 2.5x the context. Each alias keeps its own sampling through
  llama-swap `setParamsByID`, so `writer` is still warmer than `agent`.
  [config/models.json](config/models.json), [scripts/bob_models.py](scripts/bob_models.py),
  [scripts/tools/generate.py](scripts/tools/generate.py), [docs/USAGE.md](docs/USAGE.md)
- **`bob` no longer holds the speech model hostage.** `bob up` and `bob serve` used to start
  faster-whisper eagerly whenever voice was enabled, and its GPU model sat on ~1 GB of VRAM whether or
  not anyone ever spoke. The server now loads its model on the first transcription and frees it again
  after `voice.sttIdleSeconds` (default 900); the port stays open throughout, so the `/voice` preflight,
  the health probe and the pidfile lifecycle are unchanged. `voice.preload = true` restores the old warm
  start. [scripts/faster_whisper_server.py](scripts/faster_whisper_server.py),
  [scripts/tools/stack.py](scripts/tools/stack.py), [config/defaults.json](config/defaults.json)

### Fixed
- **An upgrade no longer strands clients on the old key.** Every start, auto-start included, regenerates
  the generated configs whose embedded LiteLLM key is stale (`generate.refresh_stale_key_files`) and
  restarts a Bob-started proxy that rejects Bob's key (`bob_core.litellm_key_rejected`: `GET /v1/models`
  answers 401 or 403); a proxy Bob did not start is reported, not touched. Open WebUI's stored connection
  to Bob's LiteLLM port gets the current key before WebUI starts (`generate.webui_sync_key`; connections
  to anything else keep theirs), and `bob gen` updates fabric's LiteLLM key (`refresh_fabric_env`).
  `bob doctor` adds a "Generated configs carry the current LiteLLM key" row.
  [scripts/tools/stack.py](scripts/tools/stack.py), [scripts/tools/generate.py](scripts/tools/generate.py),
  [scripts/tools/health.py](scripts/tools/health.py)
- **Per-role sampling could be overridden by any client.** Sampling moved out of `--temp`-style flags
  into `setParams` on every tier (applied server-side, so a client's `temperature` cannot change it),
  with the same values per role everywhere; `bob gen` warns on a sampling flag left in `flags`. `ponder`
  gets its own prompt, pro roles inherit `prompts[role]`, and the cpu `writer` and `agent` are aliases
  of `chat`. [config/models.json](config/models.json), [scripts/tools/generate.py](scripts/tools/generate.py)
- **The reranker rejected long pairs.** A rank-pooling reranker refuses any query plus document longer
  than its ubatch, so `rerank` runs at `-c 1024 -ub 1024 -b 1024` and `bob doctor` flags a batch smaller
  than the context. The VRAM of the new setting still needs an on-card measurement.
  [config/models.json](config/models.json), [scripts/tools/health.py](scripts/tools/health.py)
- **Recall returned loosely related rows.** Recall now gates on the raw semantic score
  (`memory.recallThreshold`, with an optional `memory.rerankThreshold`) before recency and type reorder
  anything, and embed and rerank inputs are fitted to the model's context. `memory.embedModel` is now
  read. [scripts/bob_memory.py](scripts/bob_memory.py)
- **Client configs overstated the window.** Continue, dsh and aider state each role's per-slot window;
  Continue ships the SearXNG MCP server only when `agent.searchProvider` is `searxng`, adds Bob's MCP
  server when `agent.mcpEnabled` is on, and leaves out the npx servers when `npx` is absent. The Open
  WebUI prompt sync merges instead of clobbering, and the dsh MCP entry is replaced in place when the
  transport changes. [scripts/tools/generate.py](scripts/tools/generate.py)
- **The agent API reported an upstream outage as a server fault.** An unreachable model backend returns
  503 and a failing one 502; the task worker exits 1 on an error and 2 when it stops at max steps.
  [scripts/bob_agent_server.py](scripts/bob_agent_server.py), [scripts/bob_task_runner.py](scripts/bob_task_runner.py)
- **Update and stop were rougher than they looked.** `bob update` no longer relocks `versions.lock`, the
  restart waits for the old processes to exit, the engine update swaps files atomically, and a rollback
  restores on any error. `bob stop` stops only Bob's own processes. Setup never overrides a profile you
  chose; it only suggests one. [scripts/bob/cli.py](scripts/bob/cli.py), [scripts/tools/stack.py](scripts/tools/stack.py),
  [scripts/bob/kernel.py](scripts/bob/kernel.py)
- **Bob's models in DeepSeek Harness failed with "no credential for provider route bob".** The route
  referenced `BOB_LITELLM_KEY`, but nothing set it, so a fresh install could not connect until the user
  exported it by hand and restarted dsh. `bob gen` (and setup) now stores Bob's `litellmKey` under that
  name in dsh's credential store, `$DSH_HOME/.credentials.yaml`, editing only that line so the user's
  other keys and comments survive. dsh watches the file, so a running harness connects on its next
  request. [scripts/tools/generate.py](scripts/tools/generate.py).
- **DeepSeek Harness was told the wrong window for several models.** The 32gb tier advertised all 393216
  tokens of `-c`, but `--parallel 2 --no-kv-unified` gives each request 196608, so dsh overran the slot
  long before it compacted. 4096- and 8192-token roles (16gb vision, all of 8gb and cpu, 12gb
  ponder/writer) were offered even though pi-ai caps output at the window minus the prompt minus 4096,
  which left them one token to answer in; roles under 16384 are now left out and `bob gen` names them.
  Pro roles carried Bob's short chat cap (2048 to 8192 output tokens), which truncated an agent's file
  writes mid tool call, and no window at all; they now take the peer's real `contextWindow` and
  `maxOutputTokens` (DeepSeek V4: 1000000 in, 32768 out). `vision-pro` was marked image capable while
  routing to a model that takes no images; a pro role is image capable only with `supportsVision`, and
  a `vision` role without it is left out. [scripts/tools/generate.py](scripts/tools/generate.py),
  [config/models.json](config/models.json).
- **Cloud models cut off long answers and large tool calls.** Each pro role carried its own short output
  cap (2048 to 8192 tokens), which `litellm.yaml` applied to every client that sends no `max_tokens`:
  `bob chat --pro`, the agent loop's pro fallback, Continue, aider and Open WebUI. A local role has no
  cap, so the same file write that worked on `coder` broke mid tool call on `coder-pro`. The cap now
  comes from the peer's `maxOutputTokens` (DeepSeek 32768, with `ponder` raised to 65536 because its
  thinking spends the same budget; GLM 32768), overridable per role, and the per-role `maxTokens` is
  gone. A client's own `max_tokens` still wins. [scripts/tools/generate.py](scripts/tools/generate.py),
  [config/models.json](config/models.json).
- **Two `maxTokens` settings did nothing.** `defaults.maxTokens` was documented as `bob chat`'s default
  and `voice.maxTokens` as the voice reply cap, but no code read either. Both are removed rather than
  wired up: a hard cap cuts a spoken reply mid-sentence, and a reasoning model can spend all of it
  thinking. `bob chat --max N` still caps a single call.
- **A memory lookup was unloading the chat model.** llama-swap puts any model Bob does not list as a
  swap member into an implicit default group whose `exclusive` defaults to true, so loading `embed` or
  `rerank` evicted everything else — every semantic recall paid a full model reload. `bob gen` now emits
  a named `resident` group (`swap: false, exclusive: false, persistent: true`) for them.
  [scripts/tools/generate.py](scripts/tools/generate.py)
- **Three llama-server defaults were reserving VRAM nothing used.** With no `-c`, llama-server reserves
  the model's entire trained context window: measured at 4.8 GB for the 0.6B embedder and 5.7 GB for the
  0.6B reranker, against 1.4 GB each at `-c 2048`. `--parallel` defaults to four slots, each with its own
  KV and, on a hybrid attention/SSM model, its own recurrent-state cache (~450 MiB). `-ub 512` sizes a
  compute buffer neither 0.6B helper needs (~226 MiB each). Every model in the registry now sets `ctx`,
  the `srv` macro always emits `-np 1`, and the helpers pass `-ub 128`.
  [config/models.json](config/models.json), [scripts/tools/generate.py](scripts/tools/generate.py),
  [docs/TUNING.md](docs/TUNING.md)
- **A vision model no longer loses flash-attention and reasoning extraction.** The generator dropped
  `--flash-attn` (and, accidentally, `--reasoning-format deepseek`) from any model with an `mmproj`,
  on the grounds that the two were incompatible. `mtmd` detects flash-attention support per backend and
  falls back on its own, so the exclusion only cost VRAM and speed.
  [scripts/tools/generate.py](scripts/tools/generate.py)
- **`bob profiles` was multiplying a profile's size by its role count.** The total and the on-disk count
  are now per file, so roles sharing one GGUF are counted once; `bob model` marks an alias and reads its
  loaded state from the model that actually loads. [scripts/tools/models.py](scripts/tools/models.py)
- **A tool image on a small vision window no longer overflows it.** Switching to the vision role mid-run
  (a tool returned an image) now refits the tools to the vision model's window, counts each image at its
  flat token cost, and shrinks the reply to what is left. When even that does not fit, the images are not
  sent: the run stays on its role and the transcript says why, instead of a `max_tokens=1` request with an
  oversized prompt. [scripts/bob_loop.py](scripts/bob_loop.py)
- **fabric's pre-LiteLLM `.env` is migrated.** `bob gen` now also rewrites a `.env` that still holds the
  `OPENAI_API_KEY=sk-local` pair an earlier setup wrote, so `fabric_run`'s `--vendor LiteLLM` works.
  [scripts/tools/generate.py](scripts/tools/generate.py), [scripts/tools/build.py](scripts/tools/build.py)
- **A rotated key reaches dsh and a running Open WebUI.** Every start now refreshes the key in dsh's
  credential store (when `$DSH_HOME/.credentials.yaml` exists), not only `bob gen`, and when the key sync
  updates a running Open WebUI's stored rows it says WebUI needs a restart, since it keeps the key it
  read at start. [scripts/tools/stack.py](scripts/tools/stack.py), [scripts/tools/generate.py](scripts/tools/generate.py)

### Changed
- **A default install downloads and compiles far less.** llama-swap installs as the pinned,
  SHA-verified release binary (Go only for `--from-source`, including a pinned arm64 build), Node is
  optional (`--with-node`), and the compiler, cmake and ninja are installed only for a source build or a
  platform with no prebuilt (arm64 Linux compiles the engine automatically). On rpm-ostree the prebuilt
  path usually layers nothing and needs no reboot. On Windows, Docker Desktop is installed only when a
  Docker service is first started, and VS2022 only for `--from-source`. Venvs install from their `.lock`
  on every OS. [scripts/bob/install_prereqs.py](scripts/bob/install_prereqs.py),
  [scripts/tools/build.py](scripts/tools/build.py), [scripts/osenv.py](scripts/osenv.py)
- **whisper.cpp is gone.** faster-whisper is the only STT backend (GPU, with a CPU int8 fallback), so the
  submodule, its build and `voice.sttEngine` are removed. [scripts/tools/provision.py](scripts/tools/provision.py)
- **Config keys that did nothing are removed.** `memory.autoSummarize`, `voice.sttEngine`,
  `voice.ttsEngine`, and the `webuiSecret`, `port`, `langfusePort` and `n8nTimezone` entries in
  `config/models.json` `defaults`. `bindHost`, `voiceBindHost`, `langfuseEnabled`, `n8nTimezone` and `litellmKey` are
  top-level runtime keys, and every `config/user.json` override sits at the top level (no `bob`
  wrapper); `config/user.json.example` lists real keys at their defaults. `vision-pro` is removed (no
  enabled peer takes images) and `vision.visionProRole` points at the local `vision` model.
  [config/defaults.json](config/defaults.json), [config/models.json](config/models.json),
  [config/user.json.example](config/user.json.example)
- **One code path for each concern.** Session recording, approval, registry building, secret handling
  and token estimation each have a single implementation that every surface calls. Internal only.
- **Per-model KV quantization.** `kvQuantK` / `kvQuantV` on a single model override the profile-wide
  macro — the 16gb 27B is held entirely in VRAM at 40960 context, which is only affordable at `q4_0`,
  while everything else on the tier keeps `q8_0`. [config/models.json](config/models.json)
- **`fim` shares the swap group on the 16gb tier** (`swap: true`), because a resident autocomplete model
  does not fit beside a 10 GB chat model on a 16 GB card. Inline completion and chat take turns there;
  24gb and up keep `fim` resident. The tier's FIM model also drops to Q4_K_M.
  [config/models.json](config/models.json)

### Added
- **Bob's MCP server speaks Streamable HTTP, so a harness can reach it from another machine.**
  `bob agent mcp --http` serves the same tool registry as the stdio transport on
  `agent.mcpHost:agent.mcpPort` (loopback `:8085` by default) at `/mcp`, and `agent.mcpTransport = "http"`
  makes it the default for a bare `bob agent mcp`. stdio is one process per client and must be co-located,
  which is exactly the constraint that kept a laptop from borrowing a desktop's Bob; the HTTP transport
  keeps sessions, so several clients share one running Bob. It is authenticated with the **same** static
  bearer tokens as the agent API (the litellm key or an `agent.apiTokens` entry, now resolved by one map
  in [scripts/bob_authstore.py](scripts/bob_authstore.py) that both servers read), only `/health` is open,
  and an anonymous call gets a 401 rather than a mount redirect because the gate wraps the whole app.
  DNS-rebinding protection is on: loopback and the bind host are accepted, and the name a remote client
  dials goes in `agent.mcpAllowedHosts`. `bob gen` writes the dsh drop-in for whichever transport is
  configured (a spawn entry for stdio, a URL + Bearer header for HTTP, overridable with `agent.mcpUrl`),
  and `bob services` lists `mcp-http` alongside the agent API.
  [scripts/bob_mcp_server.py](scripts/bob_mcp_server.py), [scripts/tools/generate.py](scripts/tools/generate.py),
  [docs/USAGE.md](docs/USAGE.md), [docs/TUNING.md](docs/TUNING.md)
- **Install says what this machine will not get, before the work starts.** `lifecycle.unbuilt_target_notice`
  names the two honest gaps: a CPU architecture with no published prebuilt (arm64 Linux, which therefore
  compiles llama.cpp) and an AMD or Intel GPU, which gets no acceleration because the GPU tier is NVIDIA
  CUDA only. Every entry point that provisions an engine routes through `ensure_engine`, so setup, `bob
  build` and `bob update` are equally honest, and `bob diagnose` repeats it on a `Target` row. The probe
  (`osenv.other_gpu_vendors`) reads PCI vendor ids from `/sys/class/drm` on Linux and the display-class
  driver descriptions from the registry on Windows, and never raises.
  [scripts/bob/lifecycle.py](scripts/bob/lifecycle.py), [scripts/osenv.py](scripts/osenv.py)

### Changed
- **The prebuilt engine download is substantially smaller.** Three changes, no capability lost on the
  machines the prebuilt exists for: the published CUDA build sets `-DGGML_CUDA_NCCL=OFF`, dropping a
  ~350 MB multi-GPU collectives library that a single-GPU box never calls (llama.cpp keeps working
  without it, and a multi-GPU owner builds `--from-source`, where NCCL stays on); every asset is now
  `.tar.xz` instead of gzip or Windows zip, worth roughly 25% on these binaries and read by the client
  with stdlib `tarfile`; and both publish jobs share one packer
  ([.github/scripts/pack_engine.py](.github/scripts/pack_engine.py)) so the two platforms cannot drift in
  format, layout or reported size. Manifest rows now carry `bytes`, and the installer announces the
  download size up front instead of pausing silently for minutes. Staging keeps a SONAME symlink a
  symlink, so a half-gigabyte CUDA lib is no longer copied twice into `bin/`.
  [.github/workflows/ci.yml](.github/workflows/ci.yml), [scripts/tools/build.py](scripts/tools/build.py),
  [scripts/bob/lifecycle.py](scripts/bob/lifecycle.py)

- **DeepSeek Harness (dsh) is a wired client.** `bob gen` now generates `config/dsh/settings.yaml`, a
  pi-ai provider route pointing every chat-capable role and enabled pro peer at Bob's LiteLLM proxy, and
  `config/dsh/cordis.patch.yml`, which mounts Bob's tool registry in dsh as an MCP server over stdio
  (`bob agent mcp`, gated on `agent.mcpEnabled`). Both are installed into the harness home (`$DSH_HOME`,
  default `~/.dsh`) by `bob gen` and by setup's client-wiring step, and skipped cleanly when dsh is not
  installed. Unlike the Continue and aider configs, `settings.yaml` is merged rather than symlinked,
  because dsh's own Settings UI writes that document: only the `bob` provider route is touched, and the
  MCP entry is appended to `cordis.patch.yml` textually so a hand-written patch file keeps its comments
  and `!!js` expressions. The route ships `supportsDeveloperRole: false` and `maxTokensField: max_tokens`,
  without which pi-ai, which infers a request shape from the endpoint URL and reads an unrecognized
  address as OpenAI itself, would send every reasoning model's system prompt as `role: developer` and cap
  output with `max_completion_tokens`, neither of which llama.cpp accepts.
  [scripts/tools/generate.py](scripts/tools/generate.py), [scripts/bob/kernel.py](scripts/bob/kernel.py),
  [docs/USAGE.md](docs/USAGE.md)

## [1.3.0] (2026-09-08)

### Added
- **A `writer` role for long-form prose**, served by DeepSeek-R1-Distill-Qwen-32B. Reachable as
  `bob write [--pro]`, `bob chat --write`, and `/model writer` in the shell; `writer-pro` routes to
  DeepSeek V4 Pro (or GLM-5.3 when the zhipu peer is on). It joins the `ondemand` swap group, so it
  loads on demand and unloads like the other big models. The 8gb profile serves the role from
  Qwen3.5-9B instead: a 32B does not fit an 8 GB card at any usable quant. It is a reasoning model, so
  its thinking pass is routed to `reasoning_content`; a very small `--max` can be spent entirely on
  that pass and return an empty answer.
- **`ngl: "auto"` in a profile entry** drops `-ngl` from that model's command so llama.cpp fits the
  GPU offload to whatever VRAM is actually free (`common_fit_params`). Any explicit `-ngl` aborts that
  fit, so the shared `srv` macro cannot ride along and the model gets its own expansion. This is what
  lets a DENSE model larger than the card run at all: `--n-cpu-moe` only helps a MoE, and a hand-tuned
  layer count is wrong on every card but the one it was measured on. Used by `writer`.
  [scripts/tools/generate.py](scripts/tools/generate.py)
  [config/models.json](config/models.json), [config/defaults.json](config/defaults.json),
  [scripts/bob/registry.py](scripts/bob/registry.py)
- **`bob memory migrate --reembed`** rebuilds vectors left stale by an embed-model swap, and plain
  `bob memory migrate` now reports how many stale rows it can see.
  [scripts/bob_memory.py](scripts/bob_memory.py)

### Changed
- **Local models refreshed a generation.** `chat` and `agent` move to Qwen3.5-9B (retiring
  Hermes-3-Llama-3.1-8B, whose Llama-3.1 base dated to Aug 2024; Qwen3.x emits the same
  `<tool_call>` format the agent loop already parses, so the tool path is unchanged); `ponder` moves
  to Qwen3.6-35B-A3B (same 3B active params as the Qwen3-30B-A3B it replaces); `vision` moves to the
  first-party Qwen3-VL-8B GGUF; `embed` and `rerank` move to Qwen3-Embedding-0.6B and
  Qwen3-Reranker-0.6B. `coder` stays on Qwen3-Coder-30B-A3B and `fim` on Qwen2.5-Coder, since neither has a
  newer first-party replacement. Cloud peers move to `glm-5.3` and `kimi-k3`.
  [config/models.json](config/models.json)
- **Vendored submodules refreshed to their latest stable tags.** llama.cpp b9993 -> b10853, llama-swap
  v239 -> v255, whisper.cpp 0ae02cdb (v1.9.1+75) -> v1.9.3, fabric v1.4.458 -> v1.4.478. whisper.cpp is
  back on a release tag rather than parked ahead of one. Per-project details in
  [docs/VENDOR-CHANGELOG.md](docs/VENDOR-CHANGELOG.md); pins in [versions.lock](versions.lock).
- **One `bob update` is the whole move.** An endpoint that kept serving through an update was left running
  the pre-update binaries and the generated config from before the pull (`config/llama-swap.yaml` is
  rebuilt from `config/models.json` on a stack start), so a registry change like the one above needed a
  second `bob restart` nobody knew to run. `bob update` now restarts a running endpoint at the end, before
  the closing doctor; a stack that was already down stays down, and `--no-restart` opts out. The restart
  covers only an endpoint the stack started in the background (it wrote a pidfile). A foreground `bob serve`
  writes none, so it is reported and left serving rather than killed out from under its terminal, which is
  also what an orphan from a crashed start now gets instead of a silent name-kill.
  [scripts/tools/build.py](scripts/tools/build.py), [scripts/tools/stack.py](scripts/tools/stack.py)

### Fixed
- **`bob release <v> --tag` no longer stacks a duplicate changelog section.** The documented flow is
  cut, review, commit, then tag, and that second call re-entered the changelog cut and wrote a second
  empty `## [v]` heading above the real one. `cut_changelog` is now idempotent: a changelog that already
  carries the version's section is returned untouched, and the CLI says it was already cut.
  [scripts/bob/versions.py](scripts/bob/versions.py)
- **`bob build` no longer keeps a stale engine after a submodule bump.** It skipped whenever a
  `bin/llama-server` merely existed, so a bumped llama.cpp pin left the OLD binary in place while
  reporting success. Worse, the run said so out of both sides of its mouth: the prebuilt path correctly
  refused a mismatched asset and announced it was "building from source to stay in sync", then the
  source path skipped anyway. The build-tier marker now records the commit the engine was built from,
  and the skip compares it against the pinned revision. An unknown commit (a marker written before this
  field, or a hand-placed binary) counts as current, so an existing install never gets a surprise
  rebuild. `bob update` was never affected: it passes `force=True` to every moved component.
  [scripts/tools/build.py](scripts/tools/build.py), [scripts/osenv.py](scripts/osenv.py)
- **Memory vectors are now stamped with the embed model that produced them** (schema v4, backfilled).
  Two embedding models of the same width are not comparable but do not fail either: bge-m3 and
  Qwen3-Embedding-0.6B are both 1024-dim, so `cosine()` would zip a stale vector against a fresh query
  and return a meaningless score, silently degrading recall after an embed-model change. A row whose
  stamp does not match the active embed model is now treated as "no vector yet": it still reaches
  keyword/FTS recall and is skipped by near-dedup, until `bob memory migrate --reembed` rebuilds it.
  The semantic code index reuses this store, so it inherits the same protection. `bob doctor` (and so
  the closing doctor of every `bob update`) reports any rows still on an older embed model and names
  the command that fixes them, so an update that swaps the embed model cannot quietly leave recall
  degraded to keyword-only.
  [scripts/bob_memory.py](scripts/bob_memory.py), [scripts/tools/health.py](scripts/tools/health.py)
- **`bob agent` no longer overflows the model's context on a long prompt.** A goal carrying a pasted
  reference document was sent whole: the history window kept the newest message whatever its size, and
  the tool schemas that ride on the request (OpenAI tool mode, and the grammar-constraint payload in
  hermes mode) were never charged against `agent.maxContextTokens`, so a run could assemble a prompt
  well past the backend's window and fail the step with a 400. The budget now reserves the request-borne
  schemas plus the generation (`agent.outputReserveTokens`, default 1024, or `--max` when set), an
  oversized lone message is clamped in the middle with head and tail kept rather than sent whole, and a
  system prompt that has eaten the whole budget says so in `logs/bob-agent.log` instead of overflowing
  silently. The `agent` role's context also goes 8192 -> 32768 on the 16gb profile (Hermes-3-Llama-3.1-8B
  handles far more; the KV cache at q8_0 costs about 2 GB).
  [scripts/bob_loop.py](scripts/bob_loop.py), [config/models.json](config/models.json)

## [1.2.3] (2026-07-16)

### Changed
- **Smarter CI: do only the work a change requires, and never twice** (internal, no user-facing surface). The
  `acceptance-cpu` cache is split into a build cache keyed on the submodule commits + toolchain and a model
  cache keyed on the model manifest, so a release or version-only lockfile change reuses both fully warm
  instead of forcing a from-scratch recompile. A release tag skips the redundant heavy build+eval+distro tiers
  (that commit already passed on `main`) while keeping the fast `lint`/`core` sanity. Docs-only changes skip
  the heavy `acceptance-cpu`/`eval` tiers, `prereqs-distro` runs only when the install/package surface changes
  (plus weekly), and superseded PR runs are cancelled. [.github/workflows/ci.yml](.github/workflows/ci.yml).
- **Release publishing is faster and can't hang** (internal, no user-facing surface). When a release pins the
  same llama.cpp commit as the previous one, its engine binaries are byte-equivalent, so a new `engine-plan`
  CI job skips the engine build + the multi-GB asset upload entirely and `publish-manifest` copies the prior
  release's `engines.json` verbatim (its rows point at the prior assets and carry the matching
  `builtFromCommit`, which the resolver's commit-match guard accepts). Every `gh release upload` is now wrapped
  in a timeout + retry, so a stuck upload is killed and retried instead of stalling the job for hours.
  [.github/workflows/ci.yml](.github/workflows/ci.yml).

### Fixed
- **Installing or updating during a release's publish window no longer forces a source build.** A release tag
  becomes visible before its engine assets finish uploading (~45 min for an engine-changing release). Install,
  `bob update`, and the engine resolver now select the newest release whose `engines.json` is actually published,
  falling back to the previous complete release during that window; the commit-match guard keeps a borrowed
  manifest safe (a prebuilt is used only when its `builtFromCommit` matches the checkout's pinned llama.cpp).
  [scripts/bob/lifecycle.py](scripts/bob/lifecycle.py), [scripts/tools/build.py](scripts/tools/build.py),
  [install/install.sh](install/install.sh), [install/install.ps1](install/install.ps1).

## [1.2.2] (2026-07-16)

### Added
- **`bob release <x.y.z>` cuts a release without drift.** One command moves `VERSION`, the `versions.lock`
  `release` field, and `CHANGELOG.md` ([Unreleased] into a dated section) together so they cannot fall out of
  sync, with an opt-in `--tag` and a `--dry-run` preview. It regenerates the lock manifest-free, so cutting a
  release on a dev box never bakes that machine's model shas. This prevents the class of drift that forced a
  1.2.1 re-cut (bumping `VERSION` alone left the lock stale and failed the gates).
  [scripts/bob/versions.py](scripts/bob/versions.py), [scripts/bob/cli.py](scripts/bob/cli.py),
  [scripts/bob/registry.py](scripts/bob/registry.py).
- **Engine-manifest resolution contract test.** A hermetic test mirrors the exact row shape the release
  publishes and asserts the resolver selects every row, that the internal `gpu` tier matches the published
  `cuda` tier, and that a wrong tier value or renamed key yields no match (the drift that shipped an engine
  every GPU box declined). A dedicated `manifest-contract-live` CI job (schedule + manual) fetches the actual
  published `engines.json` and re-checks it against the live resolver so a shipped release cannot silently rot.
  [tests/test_release_manifest.py](tests/test_release_manifest.py), [.github/workflows/ci.yml](.github/workflows/ci.yml).
- **GPU acceptance: real inference on the published prebuilt.** `scripts/smoke.py` gained `--require-gpu` and
  `--expect-source`, which assert the staged engine ran on the GPU (not a silent CPU fallback) with the expected
  provenance (`prebuilt` vs `source`). Run at release time via the documented runbook
  ([docs/GPU-ACCEPTANCE.md](docs/GPU-ACCEPTANCE.md)) on a tag checkout: download the published driver-only
  asset, serve it, and verify it serves tokens on the GPU, so a broken resolver or engine is caught before the
  release is trusted. GPU acceptance runs locally, not in CI, because a self-hosted GPU runner on a public repo
  is an unacceptable standing risk. Windows CUDA on a real Windows GPU stays a known residual.

### Fixed
- **No more false "stale lockfile" on a clean working tree.** `bob lock --check` (and `bob doctor`) regenerate
  the lock ignoring the gitignored per-machine `models/manifest.json`, so a real on-disk sha for a model the
  committed lock pins as null no longer reports STALE. The check is now deterministic across a clean checkout,
  a dev box, and CI; sha integrity is still enforced at fetch time. [scripts/bob/versions.py](scripts/bob/versions.py).

## [1.2.1] (2026-07-16)

### Changed
- **Windows source builds use the Ninja generator with the MSVC environment auto-activated.** `bob build`
  on Windows now activates the Visual Studio toolchain itself (via `vswhere` + `vcvars64`), so a source build
  works from any shell with no "Developer Command Prompt" needed. Both OSes share one Ninja build recipe.
  No change for the common case (users download the prebuilt engine, which is byte-identical to 1.2.0).
- **Faster release builds (internal).** The engine compile is cached (ccache), warmed on the default branch
  and restored on release tags, so a release re-cut is a fast incremental build instead of a from-scratch
  recompile. Windows CUDA engines build on a pinned `windows-2022` image (a toolchain CUDA 12.8 supports).
- **One install/update lifecycle seam; a GPU box can no longer silently run CPU.** The four entry points that
  used to decide the build tier independently (`bob setup`, `bob build`, `bob update`, and the internal build)
  now route through one seam ([scripts/bob/lifecycle.py](scripts/bob/lifecycle.py): `resolve_build_tier` +
  `ensure_engine`). A GPU box with no reachable CUDA toolkit and no `--cpu` consent is BLOCKED with a
  one-command route rather than quietly building the CPU tier; `bob update` warns and keeps a running box
  alive instead of hard-failing. This was the drift that let a great GPU sit idle on CPU inference.
- **Prebuilt, driver-only engines (Bob stops being the outlier).** Bob installs a prebuilt `llama-server`
  that bundles the CUDA runtime libs, so a target box needs only the NVIDIA driver, never the CUDA Toolkit,
  and never compiles. Each release publishes an `engines.json` manifest **as a release asset**; the engine is
  downloaded, SHA256-verified against that manifest, and its CI build provenance is attested (verifiable with
  `gh attestation verify`). Source build stays the reproducible fallback via `--from-source`, and the default
  install no longer pulls the multi-GB CUDA Toolkit. Atomic hosts (Bazzite/Silverblue) work driver-only on the
  default path, no distrobox needed.
- **Broad, safe coverage.** One fat multi-arch CUDA binary covers every supported NVIDIA generation
  (Turing through Blackwell); the Linux binaries are built in an old-glibc container so they run on
  essentially every current distro, and a runtime self-check falls back to a source build if a binary can
  not launch (e.g. an unusually old glibc), so no machine is ever left with a non-starting engine. Windows
  engines are built by Bob too (bundled DLLs beside the .exe). arm64 Linux and AMD/Intel GPUs have no
  prebuilt yet and fall back to a source build / the CPU tier. A tagged release builds + uploads the engines
  and their manifest as release assets automatically, so no repo write-back or merge is needed to activate it.
- **`bob diagnose` / `bob status` flag an idle GPU.** A new `bin/.build-tier.json` marker records the tier
  bin/ was built at; diagnose emits a loud, actionable line when an NVIDIA GPU is present but the engine is
  CPU-only, so the silent-degradation case is impossible to miss.
- **Release channels: `stable` vs `latest`.** `stable` tracks the latest `v*` release tag (which carries the
  tested prebuilt engines); `latest` tracks main (source-built bleeding edge). The channel is inferred from
  the checkout (a release tag -> stable, a branch -> latest) so it never disagrees with git state; an explicit
  `bob update --channel <x>` or the installer's `--dev` flag overrides it. Fresh installs default to `stable`
  (the installer checks out the latest release tag), and `stable` never downgrades a checkout already at or
  ahead of the latest release. A prebuilt update is a fast driver-only binary swap rather than a recompile.
- **Commit-match guard keeps prebuilt and source in lockstep.** A prebuilt engine is used only when its
  `builtFromCommit` equals the commit this checkout pins the submodule to, so a prebuilt is never a different
  llama.cpp version than a `--from-source` build would produce here (on `latest`/main, whose commit no release
  built, the guard skips the prebuilt and builds from source). Paired with the runtime self-check, the two
  install paths can neither diverge in version nor leave a machine with a non-running engine.

### Added
- Per-release **`engines.json` manifest asset** (component/os/arch/tier -> URL + SHA256 + the commit it was
  built from) that `lifecycle.ensure_engine` fetches and verifies; `config/engines.json` is an optional local
  override for development, testing, or an air-gapped mirror. The manifest lives with the release, not the
  repo, so `versions.lock` stays the source trust root (submodules + models) and never churns as engines are
  added.

## [1.2.0] (2026-07-14)

Sharper daily driver: a current local coder, refreshed cloud peers, and a faster, tougher voice path.
See [ROADMAP.md](ROADMAP.md) for where Bob is headed next.

### Changed
- **Local coder moved to Qwen3-Coder-30B-A3B (MoE), right sized per VRAM profile.** Off the older
  Qwen2.5-Coder-14B onto the current coder-specialized MoE (Apache 2.0, 256K context). It lands per tier
  because no single coder fits every card: Q4_K_M with CPU expert offload (`--n-cpu-moe`) on the tight
  12 GB and 16 GB cards, native Q4 on 24 GB and Q6_K on 32 GB (no offload), a small dense coder
  (Qwen2.5-Coder-7B) on 8 GB, and the tiny model on the CPU tier. One coder family, fewer lock entries.
  `versions.lock` is re-pinned and the old 14B dropped; `bob update` fetches the new coder and offers to
  prune the old.
- **STT default swapped to faster-whisper (CTranslate2).** Speech to text now runs on faster-whisper
  behind the same HTTP contract (`POST /inference`), selected by `voice.sttEngine` (default
  `faster-whisper`, with `whisper.cpp` kept as a fallback). Built-in Silero VAD handles endpointing and
  the model loads once and stays warm. `/voice`, `bob voice`, and the transcript contract are unchanged.
- **Cloud peers refreshed.** The GLM peer moves to GLM-5.2 on the z.ai OpenAI-compatible endpoint, and a
  new Moonshot Kimi K2.7 Code peer is added. Both are opt in (`enabled: false`; flip it and set the key),
  and one cloud coding peer is active at a time. Cloud peers stay off the local-first default and route
  through LiteLLM as `*-pro` roles.

### Added
- **faster-whisper STT server** (`scripts/faster_whisper_server.py`): a small local server on `sttPort`
  exposing the whisper.cpp-compatible `POST /inference` plus a `GET /health`, run under the runtime venv.
  `voice.sttComputeType` picks the CTranslate2 compute type (`auto` uses float16 on GPU, int8 on CPU). The
  CT2 model is fetched into `models/faster-whisper/<size>/` by setup and by `bob update`.
- **Hardened voice loop.** A missing mic or capture-device failure, an engine crash mid-turn, an empty
  transcript, and an unreachable backend now each degrade to a clear message instead of a traceback: mic
  errors are wrapped, transcription timeouts / 5xx / malformed responses are caught, and the loop restarts
  the STT server once and retries the turn before leaving voice mode.
- **`bob update` lands a fully working default.** It provisions voice (STT model + piper voice + audio
  deps) exactly as a fresh setup does, and offers to prune model files a release dropped (opt in, guarded
  so it never deletes a current model that has not downloaded yet).
- **CI gates fresh-install voice.** `acceptance-cpu` runs a faster-whisper CPU `/inference` round trip
  (`scripts/smoke_voice.py`) on Linux and Windows, so a broken default voice backend blocks the PR.

### Fixed
- **DeepSeek cloud peer on the V4 model IDs.** The `deepseek` pro peer used `deepseek-chat` and
  `deepseek-reasoner`, which DeepSeek deprecates on 2026-07-24. It now uses `deepseek-v4-flash`
  (chat/coder/vision) and `deepseek-v4-pro` (ponder). Base URL and OpenAI-compatible routing through
  LiteLLM are unchanged; a model-string swap only.

## [1.1.0] (2026-07-14)

Easy to install and get started: a new machine goes from nothing to a working Bob in one command, and a
normal install never trips over Docker. See [ROADMAP.md](ROADMAP.md) for where Bob is headed next.

### Added
- **One-command install.** A hosted install script per OS replaces the clone plus two-script dance:
  `curl -fsSL <url>/install.sh | sh` on Linux and `irm <url>/install.ps1 | iex` on Windows PowerShell.
  It ensures git, clones with submodules, runs the prereq and setup steps, then runs
  `python -m bob.kernel verify-install` to check installed submodules and model checksums against
  `versions.lock`. Idempotent on re-run (an existing clone fast-forwards). Linux and Windows ship now;
  macOS arrives with 2.0. The scripts live at `install/install.sh` and `install/install.ps1`.
- **Native trace sink.** Agent tracing defaults to a local file sink (`logs/traces/<trace_id>.jsonl`,
  viewed with `bob traces`), so observability works offline with no Docker. `agent.tracingSink` selects
  `file` (default) or `otlp`; `otlp` exports to `agent.otlpEndpoint` (for example an opted-in Langfuse).
- **Guided Docker install.** Opting into a Docker service (`bob services searxng|langfuse start`) runs a
  guided Docker install through the same package-manager seam setup uses (apt/dnf/pacman/zypper/
  rpm-ostree/winget) when Docker is missing, then brings the service up.
- **Reasoning mode (`/think`).** Reasoning is now a per-session mode on whatever model is active, not a
  swap to a separate model: `/think on|off` in the shell (and `bob think` / `bob chat --think`) toggles
  it, so your chat model can reason without switching to the bigger ponder. It rides the request's
  `enable_thinking` chat-template kwarg to llama-server; the reasoning trace stays in the model's
  reasoning channel and never enters the transcript or memory. Config default `agent.think` (off). The
  30B ponder remains a separate, explicit `/model ponder`.

### Changed
- **Docker-free default install.** Nothing in Bob's core needs Docker, and setup no longer provisions or
  starts any Docker service. Web search defaults to the in-process `ddgs` metasearch provider (no service,
  no daemon, no Docker). n8n now runs native on the Node toolchain as an opt-in (`bob services n8n start`).
  SearXNG and Langfuse remain Docker, now explicit opt-ins started on demand. All add-on services are
  lazy: they start only when asked, never at setup.
- **Onboarding and entry clarity.** First-run polish for the "it doesn't know me yet" case, and a single
  unmistakable entry point for a new user.
- **Renamed the reasoning model role `planner` to `ponder`** so it no longer collides with the new
  `/think` reasoning mode: `planner` was both a model and (loosely) a "thinking" concept. The role is
  now `ponder` everywhere (config, clients, docs); select it with `/model ponder`. "think" now means
  only the mode, which any model can use.
- **Refreshed vendored submodules to latest releases:** llama.cpp `b9827` to `b9993`, llama-swap `v230`
  to `v239`, fabric `v1.4.455` to `v1.4.458` (whisper.cpp is already ahead of its newest tag, so it stays
  put). `versions.lock` re-pinned; rebuilt and verified (unit suite green, live smoke green). Per-upstream
  details in [docs/VENDOR-CHANGELOG.md](docs/VENDOR-CHANGELOG.md).

### Fixed
- **MoE models fit small cards again via `--n-cpu-moe`.** The b9993 engine bump stopped auto-spilling
  excess layers at `-ngl 99`, so the 30B-A3B ponder OOM'd a 16 GB GPU on load. `generate.py` now emits
  `--n-cpu-moe N` for MoE models that overflow VRAM (per-profile `nCpuMoe` in `config/models.json`; the
  `16gb` profile uses 24 and `24gb` uses 12), keeping the experts of the first N layers in system RAM. `/model ponder` now
  loads at ~11.7 GB, and this is the path to running an 80B-A3B class MoE on a single small card.
- **`/model` accepts task names, not just served-model names.** The shell now takes roleTable task
  names (`/model code`, `/model ponder`, `/model voice`) and resolves each to the served model, while
  still accepting raw model names (`coder`, `ponder`, `chat`) and offering both in tab-completion.
  Previously `/model` only knew the served-model names and warned "not a known role" for a task name.
- **`bob update` now rebuilds every moved submodule, not just llama.cpp.** An update previously advanced
  all submodule source but rebuilt only the engine, silently leaving stale llama-swap / fabric / whisper
  binaries after a bump. It now rebuilds each of llama.cpp, whisper.cpp, llama-swap, and fabric whose
  pinned commit moved, under one `bin/` snapshot with per-binary verify and rollback on failure.

## [1.0.0] (2026-07-13)

Bob becomes a coherent product: one command, one engine, cross platform, reproducible, and test backed.
This release consolidates the frontier agent capability, the frontier product layer (durable autonomy,
in loop multimodal, and gated computer use), a real coding agent, a redesigned memory and context layer,
the interactive shell, and the full retirement of PowerShell in favour of a single Python harness.

### Added: frontier agent capability
- **Sub agents and delegation**, **parallel tool execution**, and **context compaction** (summarize,
  don't drop) in the agent loop, plus **planning, reflection, and self repair**.
- **OS level sandbox** (Linux namespaces and seccomp, Windows job objects) and a **granular permission
  model** (`allow`, `ask`, `deny`, per tool, per owner, audited).
- **MCP both ways**: an MCP *client* that mounts external servers' tools, and the existing MCP *server*
  that exposes Bob's tools.
- **Auth and observability**: an owner scoped token store with RBAC and rate limits, and OpenTelemetry
  tracing exported to Langfuse.
- A **skill execution engine** behind the shell's skill catalog.

### Added: frontier product (durable autonomy, multimodal, computer use)
- **Durable and resumable runs**: run state checkpoints to the session store and resumes across a restart
  or crash without re running side effects (`agent.checkpoint`).
- **Detached background tasks** (`bob task start|status|logs|resume|cancel|rewind`): jobs that survive the
  client disconnecting, owner scoped and resumable.
- **Deep multimodal in the loop**: images thread through the agent loop and auto route to the vision role,
  and `/voice` is a spoken mode of the unified session.
- **Computer use** (opt in, off by default): `screenshot`, `click`, `type`, `key`, `scroll`, with every
  action approval gated, a virtual display by default, rate limiting, a kill switch, and an audit trail.
  It is never available in an unattended run without an explicit opt in.
- **Long horizon eval plus a test backed computer use security review** (`docs/SECURITY.md`), and a
  documented autonomy dial.

### Added: coding agent
- **Repo map and symbol index**, plus fast ripgrep based **code search** for code aware retrieval.
- **Structured edits**: search and replace, and unified diff patches (was whole file writes only).
- A **lint plus run tests and fix loop**, a filesystem guard, per step **checkpoint and rewind**, and a
  **diff preview** before edits land.

### Added: memory and context engineering
- Typed memory rows (profile, preference, project, fact, episodic) in SQLite with BGE-M3, **blended
  recall** (semantic, recency, importance), pin and unpin, per project scoping, human editable
  `BOB.md` and `AGENTS.md`, conflict aware consolidation, and provenance.
- **Context engineering**: reranking, self editing memory blocks, and conversation paging (config gated).

### Added: interactive shell
- A single `bob` front door: splash, streamed replies, Ctrl-C cancel, a live tool, skill, and command
  catalog, and a slash command cockpit. Fuzzy and history completion, and `config/ui.json` theming (it
  honours `NO_COLOR`).

### Changed: PowerShell fully retired (a single Python harness)
- **Zero PowerShell.** The entire PowerShell layer is gone (the front door, the OS seam library, the
  generators, lifecycle, and provisioning scripts, and the pwsh test harness), all replaced by Python. The
  only `.ps1` left is the sample `plugins/play/invoke.ps1`.
- **Python cold-start kernel.** `install_prereqs.sh`/`.bat` + `setup.sh`/`.bat` are now thin shell stubs
  that ensure `python3` and hand off to `python -m bob.kernel` (`scripts/bob/kernel.py` +
  `install_prereqs.py`), which *imports* the same capability functions the agent and `bob --run` use.
- **CLI is Python-only.** `scripts/bob/registry.py` is the single source for command dispatch + help;
  `bob <verb>` still works.
- **Inference auto-starts on demand.** `bob`, `bob chat`, and `bob agent` bring the stack up if it isn't
  running; `bob up` is now an optional pre-warm.
- **Broader Linux support:** batched toolchain install (one `sudo` prompt), and **atomic Fedora**
  (Bazzite/Silverblue via `rpm-ostree`, with a Fedora-distrobox recommendation). No longer Windows-first.
- Setup flags are now lowercase double-dash (`--skip-models`, `--profile cpu`, `--cpu`, `--launch`); the
  gates are Python (`scripts/check.py`, `scripts/smoke.py`).

### Changed: command surface tidy-up
- **Two surfaces, clearly framed.** `bob <verb>` (scripting) and the shell's `/commands` (cockpit) are
  kept distinct-with-overlap; `bob help` now lists which commands are also available live in the shell,
  computed from the registry ∩ the shell's slash set so it can't drift.
- **`bob doctor --quick`** runs the fast health check, identical to `bob setup check`, which stays as a
  back-compat alias over the one `health.health_check` core.
- **Help catalog re-bucketed** so no group is a wall: the former 17-verb `Run`/`Config` groups split into
  `Run` (daily lifecycle), `Services` (per-daemon control), `Models`, `Diagnose`, and `Setup`. Dispatch is
  unchanged, and every `bob <verb>` works exactly as before.
- **`GROUP_ORDER` single-sourced** in `scripts/bob/registry.py` (was copied in three modules); `bob up`'s
  help now shows the POSIX `--no-open`/`--with-services` spellings (the legacy `-NoOpen`/`-WithServices`
  still work).

### Added
- **zypper / openSUSE support** across the install seam (`osenv.py` package-manager resolution)
  and `install_prereqs.sh`, with an `opensuse/tumbleweed` cell in the new CI distro matrix.
- **`--with-webui`** on `setup`: Open WebUI (torch/transformers, multi-GB) is now **opt-in**
  rather than installed by default. `venv-eval` is likewise lazy (provisioned on first `bob eval`).
- **CI**: a `lint` gate (shellcheck) and a non-gating `prereqs-distro` matrix
  (fedora/arch/opensuse) that runs the documented Linux entry + `diagnose` in each distro container.
- `osenv.new_bob_venv` / `osenv.bob_python` seam helpers (one venv-build path) and honest-failure
  helpers so a failed step aborts loudly instead of continuing.

### Fixed
- `bob tools`/`agent`/`clip` were dead on Linux (a path-handling bug in the launcher); fixed along
  with the same class in the eval bootstrap and plugin invoke.
- Docker install + daemon-start were Windows-only; now branch to the package-manager seam
  + `systemctl` on Linux, and gate `docker info` on its exit code (not stdout).
- NUMA node count always returned 1; `bob diagnose` mis-reported CUDA (missed `/opt/cuda` +
  `cuda-*` dirs), mlock (Windows-only privilege claimed granted on Linux), and NUMA on Linux, and
  now exits non-zero on any failed check (brew-doctor style).
- `bob stop` orphaned worker grandchildren on Linux (now process-group kill); whisper STT bound to
  `0.0.0.0` (now loopback); cron registered without a daemon guard; nvcc host-compiler now fails early
  with an actionable message; model fetch discards poisoned `.part` files and verifies mmproj.

## [0.1.0] (2026-07-02)

First versioned release: Bob reliably installs and runs on **Windows and Linux** (NVIDIA/CUDA, or a
GPU-less CPU tier), with a reproducible, checksum-verified install and a fresh-install CI gate on both
OSes. This tag consolidates the module history below.

### Added
- **Release, reproducibility & cross-OS acceptance.**
  - `versions.lock` (neutral JSON): pins submodule commits, the
    per-venv `requirements.lock`, minimum toolchain versions, and the model manifest
    (repo → revision → sha256, incl. the CPU-tier GGUF). Generated by `bob lock`; a
    gate fails on drift. Model fetches install *from the lock* and verify the checksum (fail-loud on
    mismatch).
  - `bob doctor` reproducibility check: installed submodules/models vs the lock.
  - CI fresh-install acceptance matrix (`acceptance-cpu`) on Ubuntu **and** Windows every PR (CPU tier,
    gating); native-from-source CUDA acceptance (`acceptance-gpu`) on release tags only.
  - `VERSION` + this `CHANGELOG.md`; `bob version` reports the release + components; `bob update` is
    release-aware, cross-platform, and rolls back the build output on a failed upgrade.
  - Shared cross-OS end-to-end smoke test.
- **Cross-platform provisioner** (Windows + Linux): an OS seam, `install_prereqs` / `setup`
  one-command entries, cross-platform CUDA build, GPU-less CPU tier, OS-aware `bob doctor`.
- **Portability foundation**: neutral `config/defaults.json`, the
  `python -m bob` runtime, the `osenv.py` seam, and the OS-agnostic CI core suite.
- Earlier work: the agent runtime, HTTP server, tools, memory, voice, vision, and the
  inference stack. See `docs/ROAD-TO-BOB.md` for the full history.

[1.2.0]: https://example.invalid/bob/releases/tag/v1.2.0
[1.0.0]: https://example.invalid/bob/releases/tag/v1.0.0
[0.1.0]: https://example.invalid/bob/releases/tag/v0.1.0
