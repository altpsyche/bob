# Portability & the Provisioner Contract (Module NB)

Bob is two halves: a **portable Python runtime** (the agent loop, tools, HTTP server, sessions,
memory, MCP) and a **provisioner** (installs prerequisites, downloads models, wires clients, starts
the inference services). Module NB decoupled them so the runtime boots and runs on any OS *without*
PowerShell, while the rich Windows auto-install experience stays exactly as it was, as one
pluggable provisioner, not a hard dependency.

## The provisioner contract

A provisioner's *only* obligations to the runtime are:

1. **A resolvable config.** Either `data/config.json` exists (Windows: written by `Get-BobConfig`
   from the `.psd1` files), or the neutral sources resolve one: `config/defaults.json` +
   `config/user.json` via `scripts/bob_config.py` (`bob_core.load_config()` falls back to this when
   `data/config.json` is absent). See contract **C2**.
2. **A reachable endpoint.** An OpenAI-compatible chat endpoint on `litellmPort` (the Windows
   provisioner runs llama-swap + LiteLLM; a BYO setup can point at anything OpenAI-compatible).

*How* a provisioner satisfies these (auto-install vs bring-your-own) is entirely its business. The
runtime never assumes a particular provisioner ran; it **probes** at startup and degrades with a
clear message (`bob_core.capability_probe`).

## The Windows provisioner (unchanged)

The existing scripts are the **Windows provisioner** implementing the contract. Their behavior is
unchanged by NB:

- `install_prereqs.bat`, `setup.bat`, `scripts/bootstrap*.ps1`: prerequisites + venvs
- `scripts/fetch-models.ps1`: model downloads
- `scripts/up.ps1`, `scripts/start*.ps1`: service lifecycle
- `Get-BobConfig` (`scripts/_models.ps1`): writes the full `data/config.json`

`bob setup` / `bob up` / `.\scripts\test-dry-run.ps1` are exactly as before.

## Bring-your-own-runtime (no PowerShell)

On a box with no provisioner you can satisfy the contract manually and get a working Bob **core**
(`agent`, `agent serve`, `agent mcp`) with zero PowerShell:

1. **Config.** Optionally drop a `config/user.json` to override defaults (same runtime shape, e.g.
   `{"agent": {"maxSteps": 8}, "litellmKey": "sk-..."}`); otherwise `config/defaults.json` alone
   yields a valid runtime config. Secrets (e.g. `litellmKey`, provider keys) resolve through the
   secret seam (env var, OS keychain, or `data/secrets.json`), never a git-tracked file (contract
   **C3**).
2. **Endpoint.** Point `litellmPort` at any running OpenAI-compatible endpoint.
3. **Run.** `python -m bob agent serve` (or `./bob agent serve`) starts the FastAPI agent server;
   `python -m bob agent "<goal>"` runs a one-shot. The startup probe prints a clear message if the
   endpoint is unreachable rather than assuming the Windows setup ran.

Data and state (`sessions.db`, `bob.db`, `schedules.json`, `logs/`) default to the repo-relative
`data/` and `logs/` dirs on every OS (contract **C4**); set `BOB_DATA_DIR` to relocate them (with a
one-time migration) for a future system-install mode.

## Front door (contract C1)

One shim routes every command per `config/verbs.json` (generated from the command registry in
`scripts/bob/registry.py`): orchestration/bootstrap commands run PowerShell; runtime commands run
`python -m bob`. `bob serve` is the **inference stack**; the agent HTTP server is `bob agent serve`
(`python -m bob agent serve`).

## The cross-platform provisioner (Module NC)

NB made the *runtime* portable; **NC makes the pwsh *provisioner* cross-platform** (Windows + Linux at
parity under `pwsh` 7), plus a CPU / no-GPU tier. The lock-in was never PowerShell-the-language
(`pwsh` runs on Linux): it was the *Windows-specific commands* inside the scripts. NC moves every one
of them behind a single seam and runs the same `.ps1` on both OSes.

- **The seam: [`scripts/_platform.ps1`](../scripts/_platform.ps1)**: the pwsh mirror of `osenv.py`.
  Dot-sourced by `_models.ps1`, so every entry script gets it. `Get-Secret` mirrors `osenv.secret`
  precedence (env → keychain → `data/secrets.json` → default, C3); `Get-DataDir`/`Get-CacheDir` mirror
  `osenv.data_dir`/`cache_dir` (repo-relative default + `BOB_DATA_DIR` one-time migration, C4). Other
  primitives: `Get-BobOS`, `Get-GpuInfo`, `Get-CudaRoot`, `Get-SystemRamGB`, `Stop-ProcessTree`,
  `Send-Notification`, `Install-Package`, `Start-BobBackgroundProcess`,
  `Register-/Unregister-/Get-AgentTaskStatus`, `Get-BobExeName`/`Get-CurlExe`/`Get-VenvExe`/`Get-BinExe`.
  Each OS-branching capability is a **pure `Resolve-*`** (unit-testable via an explicit `-Os`) plus a
  thin executor. Detection funnels through `Get-BobOS`, which honors a test-only `$env:BOB_FORCE_OS`,
  so the Linux branches are proven on a Windows box (and on the CI Linux runner) by
  [`scripts/test-platform.ps1`](../scripts/test-platform.ps1), a step in `check.ps1`.
- **Linux services = nohup + pidfile; scheduler = cron.** `bob up`/`stop`/`status` and the start-*.ps1
  launchers detach via `Start-BobBackgroundProcess` (Windows: hidden process; Linux: `nohup`), reusing
  the existing pidfile stop path (`Stop-ServiceByPid` → `Stop-ProcessTree`). `bob agent install` writes
  a scheduled task on Windows, a one-per-minute crontab line on Linux: both fire `bob-agent.ps1`,
  whose `Test-CronDue` already does the cron-expression evaluation. (systemd user units are a
  documented later option behind the same seam.)
- **CPU / no-GPU tier.** `bob build --cpu` (auto-selected when `Get-GpuInfo` finds none) builds
  `-DGGML_CUDA=OFF`; the `cpu` profile in `config/models.psd1` pins one tiny GGUF. `bob profile auto`
  degrades to `cpu` with no GPU instead of erroring; `bob doctor` reports "no GPU → CPU backend".
- **Linux entry points.** [`install_prereqs.sh`](../install_prereqs.sh) + [`setup.sh`](../setup.sh) are
  thin bootstrappers (mirroring the `.bat` shims): they install `pwsh`, then hand off to the OS-aware
  `install-prereqs.ps1` / `setup.ps1`. See [MANUAL-INSTALL.md](MANUAL-INSTALL.md).
- **Proof.** `scripts/test-platform.ps1` + `test-dry-run.ps1` cover the OS branching on any host;
  [`scripts/smoke.ps1`](../scripts/smoke.ps1) (the shared cross-OS end-to-end gate, formerly
  `smoke-linux.ps1`) exercises provision → serve → `bob agent "say hi"` → `/health` + session turn +
  SSE. The ND2 CI **acceptance matrix** (`.github/workflows/ci.yml`) runs that smoke on a fresh install
  on Ubuntu **and** Windows on every PR (CPU tier), so the Linux path is proven automatically, not by hand.

## Reproducibility & releases (Module ND)

NB made the runtime portable; NC made the provisioner cross-platform; **ND makes an install
reproducible and a release shippable.** [`versions.lock`](../versions.lock) (neutral JSON, read by both
`pwsh` and Python) pins submodule commits, per-venv requirements, minimum toolchain versions, and the
model manifest (repo → revision → sha256). It is *generated* from those sources (`bob lock`), gated for
staleness by `check.ps1`, and installs run *from the lock* (`fetch-models.ps1` verifies each download's
checksum, fail-loud on mismatch). `bob doctor` reports drift; `bob version` reports the release;
`bob update` moves between releases and rolls the build output back on a failed upgrade. This page is the
"how the split works" reference; **[SETUP.md](SETUP.md) is the "how to install" guide for both OSes.**

macOS/Metal and AMD/ROCm remain non-goals; the `_platform.ps1` seam is where they slot in.
