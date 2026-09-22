<p align="center">
  <img src="bob.png" alt="Bob" width="360">
</p>

# Bob Roadmap

Where Bob is today and where it's going. This is the product level view. The exact pinned build each
release ships is in [`versions.lock`](versions.lock), and the release by release detail is in
[`CHANGELOG.md`](CHANGELOG.md).

## Versioning

Bob follows [semantic versioning](https://semver.org): `MAJOR.MINOR.PATCH`.

- **MAJOR**: a change to what Bob *is*, such as a new supported platform class, a new operating mode, or
  a breaking change to the install, config, or API surface.
- **MINOR**: new capabilities that don't break existing setups (models, tools, agent features).
- **PATCH**: fixes and hardening, with no new surface.

Every release is reproducible. [`versions.lock`](versions.lock) pins the submodule commits, the per-venv
dependency locks, the minimum toolchain, and the model manifest (repo, revision, sha256). `bob version`
reports the running release. `bob update` moves between releases lockfile to lockfile, rebuilds only what
changed, verifies, and rolls back on failure.

> **1.3 is the current line.** Bob started as a Windows first, two language (PowerShell plus Python)
> experiment. That whole plan is now complete: one command, one engine, cross platform, reproducible, and
> test backed. 1.0 marked the point where Bob became a coherent product rather than a build out; 1.1 makes
> it easy to install and get started, with one command per OS and a Docker-free default; 1.2 sharpens the
> daily driver with a current local coder, refreshed cloud peers, and a faster, tougher voice path, and its
> patch line makes a release prove itself with driver-only prebuilt engines, one install lifecycle seam, and
> a release cut that cannot drift; 1.3 moves the whole local registry a generation and adds a `writer` role
> for long-form prose. Everything up to and including 1.3 is shipped; everything above it is the plan.

---

## Today: a private, local first AI that chats, sees, speaks, and codes

The complete picture of what Bob does now. Everything here is shipped and covered by the test suite, and
the cross OS CI acceptance gate runs on every PR.

### Talk to it, one front door
- A single `bob` command opens an interactive shell: streamed replies, Ctrl-C to cancel, and a slash
  command cockpit (`/agent`, `/voice`, `/model`, `/up`, `/services`, `/stop`, `/logs`, `/session`,
  `/skill`).
- Every capability is also a direct command for scripts and pipes (`bob chat`, `bob agent`, `bob voice`,
  `bob describe`, `bob remember`, and more), and any single tool can run headless with
  `bob --run <tool> '{json}'`.
- Inference auto starts on demand, so there is nothing to launch first.

### Local inference, cloud on demand
- A driver-only llama.cpp engine (a prebuilt binary that needs only the NVIDIA driver, or built from
  source), hot swapped by llama-swap, behind an OpenAI compatible LiteLLM proxy. Any tool pointed at
  OpenAI works by changing one base URL.
- VRAM profiles from 8 GB to 32 GB, auto selected from the detected GPU, plus a CPU tier for GPU-less
  machines.
- Named roles (chat, coder, writer, ponder, vision, embed, fim, agent) with optional cloud "pro" peers
  (DeepSeek, GLM, Kimi) routed transparently when you ask for them, and a per model `ngl: "auto"` fit so a
  dense model larger than the card still runs.

### An agent that acts, safely
- A full tool using agent loop: memory, web, git, file, shell, and fabric tools plus drop-in plugins.
- Sub agents and delegation, parallel tool execution, context compaction (summarize, don't drop), and
  planning, reflection, and self repair.
- Safety by construction: an OS level sandbox (Linux namespaces and seccomp, Windows job objects),
  granular per tool and per owner permissions (`allow`, `ask`, `deny`, audited), owner scoped sessions, an
  auth token store with RBAC, and OpenTelemetry tracing into Langfuse.
- Speaks MCP both ways. It mounts external MCP servers' tools, and exposes its own over stdio or
  Streamable HTTP, so a client on another machine can borrow them.

### A real coding agent
- Repo map and symbol index, plus fast ripgrep based code search for code aware retrieval.
- Structured edits: search and replace, and unified diff patches, rather than whole file rewrites.
- A lint plus run tests and fix loop, a filesystem guard, per step checkpoint and rewind, and a diff
  preview before edits land.

### Durable, long horizon autonomy
- Resumable runs. A task checkpoints its state and survives a restart or crash.
- Detached background tasks (`bob task start|status|logs|resume|cancel|rewind`) that keep running after
  you disconnect.
- Computer use (opt in, off by default): screenshot plus mouse and keyboard, every action approval gated,
  running against a virtual display, with a kill switch and an append only audit. It is never available to
  an unattended task without an explicit opt in.

### Sees and speaks
- Vision in the loop: the agent can be handed an image mid task and reason over it. `bob describe` and
  `bob screenshot` cover one shots.
- Voice: a spoken conversation mode in the shell (`/voice`) and `bob voice`, with faster-whisper STT in
  and Piper TTS out.

### Remembers you
- Typed memory (profile, preference, project, fact, episodic) in SQLite with Qwen3-Embedding vectors, each
  row stamped with the model that produced it so an embed swap degrades to keyword recall rather than to a
  meaningless score.
- Blended recall (semantic, recency, importance), pin and unpin, per project scoping, human editable
  `BOB.md` and `AGENTS.md`, conflict aware consolidation, and provenance.
- Context engineering: reranking, self editing memory blocks, and conversation paging.

### Fits your existing tools
- Open WebUI, Continue.dev, Cline, aider, DeepSeek Harness, fabric (254 patterns), n8n, SearXNG,
  Langfuse, and Qdrant, all wired to the local endpoint by setup. The harness gets Bob's tools as well
  as its models, over MCP.

### Runs where you run
- Linux (apt, dnf, pacman, zypper, including atomic Fedora via rpm-ostree) and Windows 11, on NVIDIA CUDA
  or the CPU tier.
- One command to install per OS (`curl -fsSL <url>/install.sh | sh` on Linux,
  `irm <url>/install.ps1 | iex` on Windows): it clones with submodules, provisions, and verifies against
  `versions.lock`, and is idempotent on re run.
- Docker-free by default: nothing in the core needs it. Add-on services (SearXNG, n8n, Langfuse) are
  opt-in and lazy, and only Langfuse still needs Docker, with a guided install when you choose it.
- One engine, zero PowerShell: a pure Python runtime, provisioning, and CI.
- Reproducible installs and a `bob update` that rolls back on failure, `bob doctor` for health, and a
  fresh install CI acceptance matrix on both operating systems every PR.

---

## What's next

Each line below is grounded in where local AI and coding agents actually are in 2026 (sources at the
bottom). Version numbers are targets, not commitments; scope moves between them as things land.

### 1.1 easy to install and get started (shipped)
Delivered: one command install per OS (clone with submodules, provision, verify against `versions.lock`,
idempotent on re run), a Docker-free default (native `ddgs` web search, native n8n, SearXNG and Langfuse
as explicit opt-ins with a local file trace sink for the Docker-averse), and first run onboarding plus a
single unmistakable entry point. Linux and Windows ship now; the macOS path arrives with 2.0. See
[CHANGELOG.md](CHANGELOG.md) for the detail.

### 1.2 sharper daily driver (shipped)
Refreshed what's already here so it keeps pace, and closed the rough edges from real use.

- **Model refresh.** Moved the coder role off the older Qwen2.5-Coder-14B to a strong current local coder,
  Qwen3-Coder-30B-A3B (MoE), right sized per VRAM profile: CPU expert offload (`--n-cpu-moe`) on the tight
  12 and 16 GB cards, native on 24 and 32 GB, and a small dense coder on 8 GB and the CPU tier. Refreshed
  the cloud peers too: DeepSeek V4, GLM-5.2, and a new Moonshot Kimi K2.7 Code, all opt-in.
- **Faster, tougher voice.** Swapped the STT path to faster-whisper (CTranslate2) as the default backend,
  behind the same HTTP contract (whisper.cpp kept as a fallback), and hardened the voice loop against a
  missing mic, an engine crash mid turn, an empty transcript, and an unreachable backend.

### 1.2.x hardening (shipped)
The patch line: no new surface, just making a release trustworthy on real hardware and closing the rough
edges that only surface on a live install. This was grounded in real use. An early 1.2 build shipped a
prebuilt engine that every GPU box quietly passed over for a slow source build, and CI stayed green because
it never exercised the actual download and run path.

Delivered: prebuilt driver-only engines, published per release as a SHA verified `engines.json` asset with
build provenance, a commit-match guard, and a runtime self-check so no machine is left with a non-starting
engine; one install and update lifecycle seam (`resolve_build_tier` plus `ensure_engine`) so a GPU box can
no longer silently run CPU, with `bob diagnose` and `bob status` calling out an idle GPU; release channels
(`stable` and `latest`); `bob release <x.y.z>`, which moves the version, the lockfile, and the changelog
together so they cannot drift, plus a `bob lock --check` that no longer cries stale on a clean tree; a
hermetic engine-manifest contract test on every PR and a scheduled `manifest-contract-live` job against the
real published manifest; GPU acceptance through `scripts/smoke.py --require-gpu --expect-source` on a tag
checkout ([docs/GPU-ACCEPTANCE.md](docs/GPU-ACCEPTANCE.md)); and a CI that reuses warm caches, skips the
tiers a change cannot affect, and reuses the previous release's engine assets when the pinned llama.cpp
commit is unchanged.

GPU acceptance runs locally at release time rather than in CI on purpose. This repo is public, and a self
hosted GPU runner is a standing attack surface, because a pull request from a fork runs the workflow file
from the fork's own commit.

Closed in 1.3.x:

- **A leaner engine download.** The published CUDA build drops NCCL (~350 MB of multi-GPU collectives a
  single-GPU box never calls), every asset moved from gzip and Windows zip to a single `.tar.xz` format
  worth about another quarter of the size, and one shared packer produces both platforms' archives so
  they cannot drift. Rows carry the byte size, so the installer states the download size instead of
  going quiet for minutes.
- **An honest word for the unbuilt targets.** `lifecycle.unbuilt_target_notice` names them before the
  work starts: arm64 Linux has no prebuilt and compiles, and an AMD or Intel GPU runs the CPU tier
  because the accelerated tier is NVIDIA CUDA only. `bob diagnose` repeats it.

Carried forward, still open:

- **Windows CUDA on real Windows GPU hardware.** Bob builds and publishes that binary, but nothing has ever
  run it on an actual Windows GPU.

### 1.3 a current registry, and long-form prose (shipped)
This line refreshed the models rather than the harness, so the coding agent work originally scoped here
moved intact to 1.4.

- **The local registry moved a generation.** `chat` and `agent` to Qwen3.5-9B, `ponder` to Qwen3.6-35B-A3B,
  `vision` to the first-party Qwen3-VL-8B, `embed` and `rerank` to Qwen3-Embedding-0.6B and
  Qwen3-Reranker-0.6B. `coder` stays on Qwen3-Coder-30B-A3B and `fim` on Qwen2.5-Coder, neither having a
  newer first-party replacement. Cloud peers moved to GLM-5.3 and Kimi K3, and the vendored llama.cpp,
  llama-swap, whisper.cpp, and fabric moved to their latest stable tags.
- **A `writer` role for long-form prose**, served by DeepSeek-R1-Distill-Qwen-32B, reachable as `bob write`,
  `bob chat --write`, and `/model writer`, with `writer-pro` for the cloud peer.
- **`ngl: "auto"`** hands the offload decision to llama.cpp instead of a hand-tuned layer count that is
  wrong on every card but the one it was measured on. This is what lets a dense model larger than the card
  run at all.
- **A model swap can no longer quietly degrade recall.** Memory vectors carry the embed model that produced
  them, `bob doctor` reports any left stale, and `bob memory migrate --reembed` rebuilds them.
- **One `bob update` is the whole move.** It restarts a running endpoint, so a registry change takes effect
  without a second command nobody knew to run.

### 1.3.x the harness you already use (in progress)
DeepSeek Harness (dsh) landed in August 2026 as a model-agnostic coding agent, MIT, with a browser UI, a
headless mode, and an everything-is-a-plugin architecture. It is the strongest argument yet for what Bob
already is: a private local brain that other front ends can borrow. So Bob wires it the way it wires
Continue and aider, and takes nothing on in return, because dsh is Node and Bob's core stays one engine,
pure Python, Docker-free.

- **Shipped: dsh is a wired client.** `bob gen` writes a pi-ai provider route for every chat-capable role
  and enabled pro peer, and mounts Bob's whole tool registry in dsh as an MCP server (stdio, or HTTP when
  `agent.mcpTransport` says so). A model
  refresh reaches the harness with one command, the same way it reaches every other client.
- **Shipped: Bob's MCP server over Streamable HTTP.** `bob agent mcp --http` serves the same registry on
  `agent.mcpHost:agent.mcpPort/mcp`, authenticated with the agent API's bearer tokens and with
  DNS-rebinding protection on. stdio is one process per client and must be co-located; the HTTP transport
  keeps sessions, so a harness on another machine, or several at once, can share one running Bob.
  `bob gen` writes the dsh entry for whichever transport `agent.mcpTransport` names.
- Carried forward from 1.2.x: **Windows CUDA on real Windows GPU hardware** is still unproven, the one
  residual that needs hardware rather than code.

### 1.4 a deeper coding agent
Take the coding loop from good to measured best in class for a local harness.

- **Structural code retrieval.** Add an `ast-grep` escalation tier on top of today's ripgrep and repo map,
  and promote the tree-sitter symbol extraction in [scripts/bob_repomap.py](scripts/bob_repomap.py), today
  an optional backend behind a lazy `grep_ast` import with a regex fallback, into a first-class part of the
  install. The 2026 evidence is clear: structure aware, agent driven search beats vector RAG for code. This
  is the stack Claude Code, Cursor, and Devin converged on.
- **Self scoring in CI, against a reference scaffold.** Run a SWE-bench Verified subset and
  Terminal-Bench 2.1 against Bob's own harness on release tags, and track the score across versions, so
  "did this release get better at coding?" has a number. Nothing measures this today, which is why the
  coding claims above are qualitative. Run the same model through dsh's minimal mode (a persistent shell
  and `str_replace_editor`, nothing else) as the control: dsh's score is what the model can do, Bob's is
  what Bob's loop does with it, and the gap is the number that actually says whether the harness earns
  its keep. Without a control, a bad score cannot be blamed on the model or the loop.
- **Deterministic eval replays.** dsh pairs a versioned session format with a replay adapter, which is
  what makes a benchmark re-scoreable rather than re-run. Bob's runs already checkpoint; the missing
  piece is replaying one against a changed harness.
- **Workflow orchestration.** A plan as code layer that fans work out across many sub agents in one
  session, where the plan lives in executable control flow rather than the model's context window. dsh's
  code mode is this idea already built and in use: the model writes one program that chains many tool
  calls, instead of spending a turn on each. Treat it as the reference design.
- **Lifecycle hooks.** Expose scriptable events around the agent loop (pre and post tool, session start
  and end) so users can customize behavior without forking the harness. dsh ships this as a plugin with a
  worked event taxonomy; borrow the taxonomy rather than inventing one.
- **Scaffolding as a choice, not a constant.** dsh offers minimal, standard, and code modes as a
  first-class setting. Bob's agent has one shape, and a small local model on the 8 GB profile is exactly
  the case a two-tool minimal mode rescues, more reliably than any amount of prompt tuning.
- **Model-free context pruning.** Bob's compaction summarizes, which costs a model call per compaction.
  Pruning stale tool results needs no model at all, and dsh separates the two for that reason.

### 1.5 more model on the same GPU
Push the inference tier so bigger, better models run on the hardware people already own. 1.2 adopted
llama.cpp's `--n-cpu-moe` and 1.3 added `ngl: "auto"`; this line is about what those two put within reach.

- **An 80B-A3B class coder on a 16 GB card**, by tuning the expert offload split per VRAM profile rather
  than settling for the first split that fits.
- **Better speculative decoding.** MTP and self speculative decoding for MoE models, where the wins are
  now real rather than a mixed bag.
- **CPU tier backend option.** Offer the `ik_llama.cpp` fork for the no-GPU tier, which is meaningfully
  faster on CPU and DDR5.
- **Streaming, low latency voice.** Parakeet-TDT or Moonshine for sub second streaming STT with VAD, so
  the voice loop feels conversational.

### 2.0 everywhere, and together
The big leaps that change what Bob *is*, which is why they carry a major version.

- **New platforms.** macOS (a Metal backend) and AMD ROCm, today's honest "not yet" rows on the support
  matrix. This is also what completes the one command installer (1.1) across all three OSes: once macOS is
  a supported platform, `curl -fsSL https://get.bob.sh | sh` covers it too.
- **Team mode.** The agent server is already owner scoped with RBAC. Productize it into a shared, multi
  user Bob with per owner memory scopes.
- **Reach your Bob from anywhere.** A secure path to your own agent server from a phone or another
  machine, without exposing it to the open internet.

### Backlog: good ideas, not yet scheduled
- On device fine tuning and personalization of a small local model on your own corrections.
- A richer plugin and skill marketplace beyond the built-in set.
- Cost and latency aware routing that picks local versus cloud per request automatically.

---

## Sources for the "what's next" research

- Open weight coding model landscape (Qwen3-Coder, DeepSeek V3.2, GLM-4.6, Kimi K2, SWE-bench):
  [Kilo](https://kilo.ai/open-source-models),
  [MindStudio](https://www.mindstudio.ai/blog/best-open-source-llms-agentic-coding-2026),
  [RunLocalModel](https://runlocalmodel.com/best-local-coding-llm-2026.html)
- Agentic code retrieval beats vector RAG (ripgrep, ast-grep, tree-sitter, repo map):
  [MindStudio](https://www.mindstudio.ai/blog/is-rag-dead-what-ai-agents-use-instead),
  [dev.to](https://dev.to/nimay_04/rag-is-not-always-the-answer-anymore-how-ai-agents-search-code-in-2026-43m3),
  [ceaksan](https://ceaksan.com/en/code-search-for-ai-agents-which-tool-when)
- Agent harnesses as replaceable scaffolding (plugin architectures, minimal versus full toolsets,
  code mode): [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness),
  [The New Stack](https://thenewstack.io/deepseek-harness-open-source-plugins/),
  [A Comparison of AI Agent Harnesses in 2026](https://winder.ai/ai-agent-harness-comparison/)
- Harness engineering, sub agents, dynamic workflows, Terminal-Bench:
  [AddyOsmani](https://addyosmani.com/blog/agent-harness-engineering/),
  [State of CLI Coding Agents, Mid-2026](https://blog.arcbjorn.com/state-of-cli-coding-agents-2026),
  [Building Effective AI Coding Agents for the Terminal (arXiv)](https://arxiv.org/abs/2603.05344)
- Inference: MoE CPU offload, speculative decoding, quant kernels:
  [ik_llama.cpp](https://github.com/ikawrakow/ik_llama.cpp),
  [n-cpu-moe performance](https://partnerinai.com/blogs/llamacpp-n-cpu-moe-performance-why-speedups-happen),
  [DFlash and when spec decoding helps](https://allenkuo.medium.com/when-speculative-decoding-helps-local-llms-and-when-it-doesnt-5c41dd804e4b)
- Local STT and TTS (faster-whisper, Parakeet, Moonshine, Piper):
  [Northflank](https://northflank.com/blog/best-open-source-speech-to-text-stt-model-in-2026-benchmarks),
  [onResonant](https://www.onresonant.com/resources/local-stt-models-2026),
  [PromptQuorum](https://www.promptquorum.com/power-local-llm/local-whisper-stt-comparison-2026)
