# Changelog

All notable changes to Bob are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions carry a `versions.lock`
(pinned submodules + deps + toolchain + model manifest) so a release is reproducible.
`bob version` reports the running release; `bob update` moves between releases lockfile-to-lockfile,
rebuilds only what changed, verifies, and rolls back on failure.

## [Unreleased]

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
