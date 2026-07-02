# bob Improvement Modules

Eight independent improvement modules. Each has a full implementation spec.
Implement in the order shown — Module A is the foundation.

## Implementation Order

| # | Module | File | Effort | Depends on |
|---|--------|------|--------|-----------|
| 1 | **A** — Config Tuneability | [MODULE-A-config-tuneability.md](MODULE-A-config-tuneability.md) | 3–4 h | — |
| 2 | **B** — Automation Hardening | [MODULE-B-automation-hardening.md](MODULE-B-automation-hardening.md) | 5–6 h | — |
| 3 | **E** — Tools Integration | [MODULE-E-tools-integration.md](MODULE-E-tools-integration.md) | ~14 h | — |
| 4 | **H** — Services Stack | [MODULE-H-services.md](MODULE-H-services.md) | 2–3 h | E |
| 5 | **C** — Speed / VRAM | [MODULE-C-speed-vram.md](MODULE-C-speed-vram.md) | 3–4 h | A |
| 6 | **D** — CLI Features | [MODULE-D-cli-features.md](MODULE-D-cli-features.md) | 7–8 h | A |
| 7 | **F** — Submodules + Ecosystem | [MODULE-F-submodules.md](MODULE-F-submodules.md) | 11–16 h | A |
| 8 | **G** — Vision / Multimodal | [MODULE-G-vision.md](MODULE-G-vision.md) | 5 h | A |
| 9 | **J** — Low-VRAM Inference Maximization | [MODULE-J-low-vram-inference.md](MODULE-J-low-vram-inference.md) | ~2.5 h | A, C |
| 10 | **K** — API Pro Peers ✓ implemented | [api-pro-peers.md](api-pro-peers.md) | ~3 h | A, E |
| 11 | **L** — Model Setup & Token Efficiency ✓ implemented | [MODULE-L-model-setup.md](MODULE-L-model-setup.md) | ~4 h | E, K |
| 12 | **M** — Agent Harness Quality & Reliability ✓ implemented | [MODULE-M-harness-quality.md](MODULE-M-harness-quality.md) | ~47 h | — |
| 13 | **N** — Road to 10/10 (multi-user / MCP hardening) ✓ implemented | [MODULE-N-road-to-10.md](MODULE-N-road-to-10.md) | ~31 h | M |
| 14 | **NB** — Portability Foundation (OS-agnostic core) — NB1–NB6 ✓ implemented (2026-07-02); NB7 deferred | [MODULE-NB-portability-foundation.md](MODULE-NB-portability-foundation.md) | spent NB1–NB6; NB7 ~6–10 h deferred | N |
| 15 | **NC** — Cross-Platform Provisioner (Win+Linux parity) — draft | [MODULE-NC-cross-platform-provisioner.md](MODULE-NC-cross-platform-provisioner.md) | ~40–57 h | NB |
| 16 | **ND** — Release, Reproducibility & Cross-OS Acceptance — draft | [MODULE-ND-release-acceptance.md](MODULE-ND-release-acceptance.md) | ~19–27 h | NB, NC |
| 17 | **NE** — Unified `bob` Interface (one front door) — draft | [MODULE-NE-unified-interface.md](MODULE-NE-unified-interface.md) | ~22–32 h | NB, NC, ND |
| 18 | **O** — Frontier Class (capability) — draft | [MODULE-O-frontier-class.md](MODULE-O-frontier-class.md) | ~71–96 h | NB, NC, ND (NE) |
| 19 | **P** — Frontier Product (durable autonomy, multimodal, computer-use) — draft | [MODULE-P-frontier-product.md](MODULE-P-frontier-product.md) | ~39–57 h | O |
| — | **Architecture Contracts** (cross-module seams — read before building NB–P) | [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) | — | — |

**Total estimated effort:** ~56–67 h (A–L) + ~47 h (M) + ~31 h (N) + the post-N roadmap: ~25–34 h (NB)
+ ~40–57 h (NC) + ~19–27 h (ND) + ~22–32 h (NE) + ~71–96 h (O) + ~39–57 h (P) ≈ **~216–303 h** planned.

> **Sequencing:** `N (done) → NB → NC → ND → NE → O → P`. NB makes the core run OS-agnostic; NC
> installs & runs the whole stack on Windows **and** Linux (incl. a CPU tier); ND proves it's
> reproducible + shippable on both; NE gives it one coherent front door. **Only then** does O add
> frontier *capability* (surfaced inside NE), and P the frontier-*product* layer (durable autonomy,
> multimodal, computer-use). The cross-module seams (dispatch, config, secrets, data-dir, CI,
> registries) are defined **once** in [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) — every
> module references it rather than re-deciding, so the roadmap docs can't drift.

---

## Quick Summary

### A — Config Tuneability
Adds a `defaults` block to `models.psd1` exposing ngl, KV quant level, flash attention,
batch size, parallel slots, and port as named knobs. Adds `config/user.psd1` (gitignored)
for local overrides that survive `git pull`. Generator builds macros dynamically from defaults.

### B — Automation Hardening
Six targeted fixes: shared port-conflict detection function, URL verifier script (HEAD checks
all HF URLs, flags GATED/MISSING/ERROR), disk-space pre-check before downloads, atomic build
output with .bak rollback, pip failure as throw, winget exit code checks.

### C — Speed / VRAM
24 GB profile (better quants: Q5_K_M/Q6_K), 32 GB profile (Q8_0 throughout).
`--mlock` for fim/embed to prevent OS paging spikes. Speculative decoding for coder
via fim as draft model (~20–40% generation speedup; tokenizer-safe pairing only).

### D — CLI Features ✓ implemented
Nine new/improved commands: `bob status` (live VRAM view), `bob restart`, `bob logs`,
streaming `bob chat` via SSE with `--sys`/`--max` flags, `bob models` with backing model names,
`bob up` running silently (no popup windows) with `-NoOpen` flag, `bob profile auto`,
SHA256 model manifest, `bob update` (submodule-aware). Plus: `bob ps` (PID/RAM/uptime),
`bob show <role>` (model details), `bob version`, tab completions via `install-cli.ps1`.

### E — Tools Integration ✓ implemented
Continue config: `contextLength` + `maxTokens`, four MCP servers (filesystem, fetch, github,
searxng). USAGE.md: Qwen3 Thinking Mode section, function-calling example, fabric patterns,
ecosystem services. Extension presence checks in setup-clients.ps1. fabric CLI patterns
(`bob fabric-setup`). LiteLLM proxy venv + config + start script (`bob litellm`).
lm-eval venv + benchmark runner (`bob eval`). setup.bat now installs Node.js and uv; fabric is built from the `external/fabric` Go submodule.

### H — Services Stack ✓ implemented
Docker Desktop setup script. docker-compose.yml for Langfuse (:3001), SearXNG (:8888),
n8n (:5678). Ports configurable via `config/user.psd1`. `bob services start|stop|status|logs`.
`bob up -WithServices` starts Docker stack alongside inference.

### F — Open-Source Submodules + Ecosystem Integrations
**Research basis:** 113-agent adversarial verification run. All tool/model claims below are 2/3-vote verified.

**whisper.cpp** (ggml-org): same CMake+CUDA build stack as llama.cpp. `whisper-server.exe`
on `:8082` with OpenAI-compatible `/v1/audio/transcriptions`. Open WebUI voice input works out-of-the-box.
CLI: `bob transcribe <file>`, `bob listen`.

**Qdrant**: production vector DB (Windows native binary, no Docker). Replaces Open WebUI's
SQLite-backed ChromaDB. HNSW index, payload filtering, REST dashboard on `:6333`.

**Hermes model profiles** (NousResearch): adds `agent` role to all profiles — Hermes 3 8B
(Q4_K_M=4.92 GB, ChatML format, single-token `<tool_call>` delimiters for reliable streaming).
Hermes 4.3-36B (Q4_K_M=21.76 GB) as alternative `planner` in `hermes-24gb` profile — hybrid
reasoning + tool calling. Refuted claim: "Hermes 3 beats Llama 3.1 Instruct on evals" (0-3 vote).

**fabric** (danielmiessler/fabric, Apache 2.0, ~47k stars): Go binary built from `external/fabric` submodule (`cmd/fabric/`); patterns from `data/patterns/` (254 patterns).
Routes patterns through local endpoint via `OPENAI_BASE_URL`. `bob fabric-setup` auto-configures.
Patterns: code review, commit messages, summarization, 200+ developer workflows.

**Tabby** (TabbyML, Apache 2.0): Windows native `.exe` code completion server, alternative to
Continue.dev FIM. Documented in FALLBACKS.md; recommend investigating OpenAI-compatible endpoint
passthrough before adopting.

**OpenHands** (All-Hands-AI, MIT, ~50k stars): agentic coding framework. Recommends Qwen3.6-35B-A3B
(same family as our planner). Supports `openai/<model>` prefix against localhost:8080. Windows needs
Docker. Local model reliability caveat documented.

### G — Vision / Multimodal
Adds `vision` role to models.psd1 (Qwen2-VL-7B-Instruct Q4_K_M + mmproj file).
Generator emits `--mmproj` flag in vision model cmd. Downloader fetches both files.
CLI: `bob describe <image> [prompt]`. Open WebUI image attachment works natively.

### K — API Pro Peers ✓ implemented
Adds `chat-pro`, `planner-pro`, `coder-pro` model names backed by external API providers.
Routes litellm → API directly (no llama-swap hop, no platform fee). Default: DeepSeek API
(chat/planner) + Zhipu GLM API (coder). Fully configurable from `config/models.psd1`;
overridable per-machine in `config/user.psd1`. Adds `langfuseEnabled` flag for Langfuse
tracing without manual litellm.yaml edits. `bob gen` now generates both yaml files.

### L — Model Setup & Token Efficiency ✓ implemented
Fixes context window mismatches, exposes all model roles in Continue, upgrades pro model routing, and adds system prompts as a first-class config feature.
**coder**: contextLength 16k → 32k in Continue (was wasting ~1.3 GB KV VRAM), maxTokens 4096 → 8192.
**Continue model picker**: added `chat`, `chat-pro`, `coder-pro`, `planner-pro` (was only `coder` + `planner`).
**coder-pro**: migrated from Zhipu GLM-4-Flash to DeepSeek V4 (same `DEEPSEEK_API_KEY`; better code quality).
**planner-pro**: request timeout raised 120 → 600 s (R1 thinking phase can exceed 2 min before first output).
**Per-model maxTokens**: pro model config changed from string to hashtable (`@{ model; maxTokens; systemPrompt? }`); `gen-litellm.ps1` emits `max_tokens` per entry.
**Budget cap**: `budget = 15.0; budgetPeriod = '30d'` in `user.psd1` enforces a LiteLLM-side monthly ceiling.
**drop_params**: added to `gen-litellm.ps1` general_settings — silently drops unsupported client params instead of 400-erroring.
**MCP filesystem**: restricted from `C:\` to `C:\Users\vsiva\dev` + `C:\bob` (strict whitelist).
**Aider map-tokens**: capped at 1024 to prevent context overflow on large repos.
**System prompts**: new top-level `prompts` key in `models.psd1`; new `scripts/gen-webui.ps1` syncs prompts to Open WebUI SQLite on every `bob gen`; `llm.ps1` gen case updated to call it.

### J — Low-VRAM Inference Maximization ✓ closed
Seven sub-modules targeting RTX 5080 (16 GB VRAM) + Qwen3-30B-A3B (17.3 GB Q4).
**Result: mlockBig fixed (was silently broken); noMmap on planner; coder/chat context doubled to 32k; 9 bugs fixed. Speed unchanged on RTX 5080 — flash-attn and q8_0/q8_0 were pre-existing. Main win is correctness.**
J1: KV cache quant — q8_0/q8_0 default (safe all GPUs, ~50% VRAM savings vs f16). Pre-Blackwell (RTX 20/30/40): q5_1/q4_0 saves ~75%. Blackwell (RTX 50, sm_120): sub-q8_0 + flash-attn regresses 15× — keep q8_0.
J2: `--no-mmap` for planner — loads 17.3 GB into heap RAM, zero disk seeks during inference.
J3: `mlockBig` flag — extends `--mlock` to swap models; `grant-mlock.ps1` + `bob mlock` automate `SeLockMemoryPrivilege`.
J4: `ubatch` knob exposed (default 512; benchmark before raising — measured neutral/slightly negative on RTX 5080).
J5: NUMA strategy exposed (`numa = 'isolate'`; no-op on AM5/Windows single NUMA node).
J6: MoE layer offloading documented — `-n-cpu-moe` does not exist; use `-ngl` threshold.
J7: `user.psd1.example` fully rewritten with all J knobs and inline guidance.

### M — Agent Harness Quality & Reliability ✓ implemented
Remediation of the 2026-07-01 harness audit (18 sub-items), all landed. **Do Now:** killed the
config/doc drift (setup check #12 now delegates to the Python loader; `agent.tools` allowlist retired
for a `disabledTools` denylist), atomic `config.json` write, guarded `resp.choices` + client-side LLM
timeout/retry, wrapped `bob_memory` HTTP calls, agent server bound to loopback + bearer auth.
**Do Next:** single source of truth for ports (`$script:BobPortDefaults` / `_PORT_DEFAULTS`),
token-aware context truncation + compact tool-schema budget, shared routing helper
(`Get-RoleForTask` / `get_role`), loud-fail edges (XML arg → `__parse_error__`, contract mismatch is a
hard skip, `web_fetch` SSRF guard, voice/vision try-catch), retired `llm.ps1`, real `bob doctor`
pre-flight, lazy `config.json` regen (mtime stale-check). **Do Later:** session + auth abstraction
(`bob_session.SessionStore` + `agent.apiTokens`), stdlib-unittest harness in `tests/` (wired as
`test-dry-run.ps1` section [11]), memory injected into the agent loop, SSE streaming
(`POST /v1/agent/completions/stream` + `--stream`), cold-start & operational hardening (registry cache,
SIGINT graceful unwind, structured per-run logging), low-sev cleanup. New: `docs/AGENT-SERVER.md`,
`CONTRIBUTING.md`. The full multi-user / MCP hardening lands in **Module N** (below).

### N — Road to 10/10 (multi-user / MCP hardening) ✓ implemented
Takes the harness from ~8/10 to production-grade for a multi-client / MCP future (10 sub-items,
all landed; 56 → 99 tests). **Identity + ownership (N1):** `agent.apiTokens` are `@{token;owner}`
records mapped to an owner id; sessions carry `owner_id` and every route is owner-scoped (cross-owner
= 404, no existence leak). **Session store (N2):** WAL + per-thread connections + atomic
`BEGIN IMMEDIATE` `append_turn` — concurrent create/append/read with zero lost turns.
**Cancellation (N3):** a `CancelToken` unifies the stream/non-stream LLM call so SIGINT and client
disconnect abort an in-flight step in ~1s; the SSE route is async, polls `is_disconnected()`, and
records no bogus turn. **Streaming (N6):** a split-safe `<tool_call>` boundary detector replaces the
substring suppression — a final answer that mentions the literal streams in full. **Observability
(N5):** `RotatingFileHandler`, a request id threaded client→server→loop, and a per-run metrics line
(`grep <rid>` reconstructs any run). **Cold-start (N4):** measured (registry build ~140 ms cold vs
~16 ms warm) → deleted the dead per-process cache, documented `bob agent serve` as the warm path.
**Ports (N7):** `bob_memory`/`web`/plugins now read ports via `_port()` — zero re-inlined literals.
**Security (N9):** `docs/SECURITY.md` (test-backed) + a `file_read`/`file_write` secrets denylist
(`config.json`, `*.psd1`, `*.db`, `logs/`, `.env*`) and a `git_*` path allow-list. **CI (N8):**
`scripts/check.ps1` (py_compile + PowerShell AST parse + unittest) + a versioned pre-commit hook,
wired into `test-dry-run.ps1` [12]. **MCP (N10):** `scripts/bob_mcp_server.py` exposes the tool
registry over MCP behind `agent.mcpEnabled` (`bob agent mcp`). New: `docs/SECURITY.md`,
`docs/improvements/MODULE-N-road-to-10.md`, `scripts/{check.ps1,install-hooks.ps1,bob_mcp_server.py}`.

### O — Frontier Class (capability) — draft, not implemented
N made the harness *trustworthy*; O makes it *capable*, closing the architecture gap to frontier
harnesses. It deliberately lifts the M/N non-goal and changes the agent loop. Runs **after** the
portability track, so its OS-touching items are dual-platform. Eleven sub-items:
**O6** granular permission/approval model (per-tool `allow|ask|deny`, per-owner, audited);
**O5** OS-level tool sandbox (**Windows restricted-token/Job-Object + Linux namespaces/seccomp**);
**O2** parallel tool execution; **O3** context compaction (summarize, don't drop); **O1**
sub-agents/delegation (isolated context, depth-capped, parallel fan-out — the centerpiece); **O4**
planning + reflection + self-repair; **O7** MCP *client* (mount external servers' tools); **O8**
frontier auth (SQLite token store, hot revoke, RBAC scopes, rate limits, via the C3 secret seam);
**O9** OpenTelemetry tracing (span tree → Langfuse); **O10** agent-capability eval *extending* the CI
matrix (C5); **O11** skill execution engine (runs the sub-agent-backed skills NE only lists — C6).
~71–96 h. See [MODULE-O-frontier-class.md](MODULE-O-frontier-class.md).

### P — Frontier Product (durable autonomy, multimodal, computer-use) — draft, not implemented
The *last* module — takes Bob from a frontier *harness* to a frontier *product*, closing the gaps O
doesn't: **P1** durable & resumable runs (checkpoint/resume across restarts); **P2** background /
detached long-running tasks (survive disconnect, `bob task start|status|resume`); **P3** deep
multimodal *in the loop* (vision as image content the agent reasons over mid-run; voice as an NE shell
mode); **P4** computer-use / desktop automation (screenshot/click/type — opt-in, sandboxed via O5,
permission-gated via O6, audited, kill-switched); **P5** long-horizon eval + a test-backed computer-use
security review. Everything opt-in and gated. ~39–57 h. See [MODULE-P-frontier-product.md](MODULE-P-frontier-product.md).

### NB — Portability Foundation (OS-agnostic core) — draft, not implemented
The PowerShell/Python split is a *drag* (port/role constants hand-mirrored across languages) and a
*wall* (the Python runtime can only boot after PowerShell generates `config.json` from `.psd1`).
NB is the first, verifiable step toward OS-agnostic **without** rewriting the Windows orchestration
or losing the auto-install/setup: **NB1** one neutral `config/defaults.json` both languages read
(kills the mirror); **NB2** a Python config resolver so the runtime boots without PowerShell;
**NB3** an `osenv` seam (shell/paths/notify) removing Windows assumptions from the Python core;
**NB4** the dispatch front door (a dumb `bob` shim + `config/verbs.json` routing + the `scripts/bob/`
package + the command registry — C1/C6, owned here so NC/ND/NE build on it); **NB3** also adds the
C3 secret seam + OS-aware denylist and the C4 data-dir policy; **NB5** a provisioner contract keeping
the Windows setup as a pluggable asset, not a dependency; **NB6** creates the CI (C5), proving the
core runs off-Windows; **NB7** (phased) a neutral authoring format (psd1 → TOML). NB is the foundation
the whole NB→P chain builds on. ~25–34 h (+10 h phased).
See [MODULE-NB-portability-foundation.md](MODULE-NB-portability-foundation.md).

### NC — Cross-Platform Provisioner (Windows + Linux at parity) — draft, not implemented
NB makes the *core* portable; NC delivers the auto-install/setup experience on **Linux too**, at
Windows parity — so Bob is an installable, working product on both. The enabler: `pwsh` runs on
Linux, so the config-based orchestration stays and just becomes OS-aware (branch `winget`/scheduled
tasks/WinRT/`nvidia-smi` behind one seam); llama.cpp/whisper (CUDA), llama-swap (Go), LiteLLM
(Python) and the docker services already build/run cross-platform. **NC1** OS-aware orchestration
seam; **NC2** Linux prereq bootstrap (`install_prereqs.sh`); **NC3** cross-platform CUDA build;
**NC4** service lifecycle — **nohup+pidfile + a 1-min cron scheduler baseline** (systemd is a later,
seam-swappable backend; nohup is what runs in GPU-less CI — decision in the module doc); **NC5**
portable model fetch + client wiring; **NC6** Linux GPU/VRAM detection → profile auto-select
(degrades w/o GPU); **NC7** cross-platform `bob doctor` + a Linux end-to-end smoke; **NC8** a **CPU /
no-GPU build+serve tier** (`cpu` profile = Qwen2.5-0.5B; smoke scoped to serve+answer, tool round-trip
stays in the deterministic unit tests) (required by ND2's per-PR CI on
GPU-less runners). ~40–57 h. See [MODULE-NC-cross-platform-provisioner.md](MODULE-NC-cross-platform-provisioner.md).

### NE — Unified `bob` Interface (one front door) — draft, not implemented
Bob has ~30 verbs bolted onto a PowerShell `switch` and no single front door. NE gives it one `bob`
command — a splash + live tool/skill catalog + interactive REPL, like Claude Code / Hermes-Agent.
Cheap because Bob already auto-discovers tools (`ToolRegistry`), so the catalog is nearly free.
**NE1** command *grouping + help* over NB4's registry (NB4 owns the registry/dispatch — C6);
**NE2** interactive REPL/TUI shell (`bob` with no args → splash + prompt, streamed, Ctrl-C cancels to
prompt); **NE3** auto-rendered tools/commands/skills catalog; **NE4** skills *registry + catalog only*
(execution moves to **O11** — the C6 split that removes the sub-agent chicken-egg); **NE5** in-shell
owner-scoped sessions + memory continuity; **NE6** generated help/onboarding. Built **registry-driven**
so O's features (sub-agent trees, permission prompts, parallel-tool progress) surface by registering,
not by rewriting the UI. Sits `ND → NE → O`. ~22–32 h.
See [MODULE-NE-unified-interface.md](MODULE-NE-unified-interface.md).

### ND — Release, Reproducibility & Cross-OS Acceptance — draft, not implemented
The capstone that turns "runs on two OSes" into "reliably installs and updates on two OSes." **ND1**
pin + checksum everything (`versions.lock` — submodule commits, dep locks, model manifest);
**ND2** a cross-OS acceptance matrix (CI on Windows + Linux that provisions a clean machine and runs
the end-to-end smoke — a CPU tier every PR, a GPU tier on release tags — this is the real "reliable"
proof); **ND3** versioned releases + a safe cross-platform `bob update` (lockfile-to-lockfile,
rebuild-only-changed, roll back on failure); **ND4** one documented install command per OS + an
honest Supported Matrix; **ND5** unified Windows/Linux SETUP docs. ~19–27 h.
See [MODULE-ND-release-acceptance.md](MODULE-ND-release-acceptance.md).

---

## Roadmap: portability → coherence → capability → product

Cross-module seams (dispatch, config, secrets, data-dir, CI, registries) are defined once in
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md); every module below references it.

```
N   ✓ done   reliable & safe on one machine (Windows)
 └─ NB       portable core            — runtime boots/runs OS-agnostic; owns C1/C2/C3/C4/C6; creates CI (C5)
     └─ NC   cross-platform provisioner — installs & runs the whole stack on Windows + Linux (+CPU tier)
         └─ ND  release & acceptance    — reproducible, auto-proven on both OSes (extends CI), shippable
             └─ NE  unified interface   — one `bob`: splash + live catalog + interactive REPL
                 └─ O  frontier capability — sub-agents, parallel tools, dual-OS sandbox, ... (surfaces in NE)
                     └─ P  frontier product — durable/resumable runs, in-loop multimodal, computer-use
```

**NB → NC → ND → NE land before O; O before P.** A reliable, installable, *coherent-to-use* Bob on
Windows *and* Linux is the entry condition for capability work — building O on a Windows-only,
drift-prone, 30-scattered-verbs base would deepen the very hole this track closes. NE is
registry-driven so O's capabilities appear *inside* the one interface by registering, not by a UI
rewrite; P then adds the long-horizon-autonomy / multimodal / computer-use layer that separates a
frontier *product* from a frontier *harness*.

---

## What Each Module Touches

```
config/models.psd1              A     E  C  G        J  K  L
config/user.psd1                A                    J     L
config/user.psd1.example                             J  K
config/continue/config.yaml           E                    L
config/litellm.yaml                   E           (new)  K  L
config/aider/.aider.conf.yml                               L
scripts/_models.ps1             A  B     G              K  L
scripts/gen-llama-swap.ps1      A     C  G           J
scripts/gen-litellm.ps1                                  K  L
scripts/gen-webui.ps1                                       L  (new)
scripts/start.ps1               A  B  D                  K
scripts/up.ps1                  A  B  D     F  H
scripts/llm.ps1                 A  B  D  E  F  G     J  K  L
scripts/fetch-models.ps1           B        F  G
scripts/diagnose.ps1               B        F  G     J
scripts/build-llama.ps1            B
scripts/bootstrap.ps1              B     E  F
scripts/setup.ps1                  B     E
scripts/setup-clients.ps1                E
scripts/setup-fabric.ps1                 E           (new)
scripts/bootstrap-litellm.ps1           E           (new)
scripts/start-litellm.ps1               E           (new)
scripts/bootstrap-eval.ps1              E           (new)
scripts/eval.ps1                        E           (new)
scripts/setup-docker.ps1                      H     (new)
scripts/verify-urls.ps1            B                (new)
scripts/build-whisper.ps1                  F        (new)
scripts/start-whisper.ps1                  F        (new)
scripts/start-qdrant.ps1                   F        (new)
scripts/grant-mlock.ps1                          J  (new)
tools/compose/docker-compose.yml              H     (new)
docs/USAGE.md                          E  F  G  H   J  K  L
docs/FALLBACKS.md                         F
docs/TUNING.md                    C                 J     L
docs/SETUP.md                                       J
docs/improvements/MODULE-J-low-vram-inference.md    J  (new)
docs/improvements/api-pro-peers.md                     K  (new)
docs/improvements/MODULE-L-model-setup.md               L  (new)
```

---

## Verification (after all modules)

```powershell
# Full test suite
.\scripts\test-dry-run.ps1

# Config tuneability
echo '@{ defaults = @{ ngl = 30 } }' > config/user.psd1
bob gen
Select-String '\-ngl 30' config/llama-swap.yaml   # should match
Remove-Item config/user.psd1
bob gen

# End-to-end
bob serve
bob status
bob models
bob chat coder "hello" --max 64
bob describe <any-png>
bob transcribe <any-wav>
bob stop
```
