# Changelog

All notable changes to Bob are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions carry a `versions.lock`
(pinned submodules + deps + toolchain + model manifest) so a release is reproducible.
`bob version` reports the running release; `bob update` moves between releases lockfile-to-lockfile,
rebuilds only what changed, verifies, and rolls back on failure.

## [Unreleased]

_Nothing yet. See [ROADMAP.md](ROADMAP.md) for what's planned (1.1 and beyond)._

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

[1.0.0]: https://example.invalid/bob/releases/tag/v1.0.0
[0.1.0]: https://example.invalid/bob/releases/tag/v0.1.0
