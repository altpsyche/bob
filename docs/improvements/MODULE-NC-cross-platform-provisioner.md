# Module NC — Cross-Platform Provisioner (Windows + Linux at parity)

**Status:** draft / not implemented. **Depends on:** Module NB. **Precedes:** ND → NE → O → P — a
reliable, installable Bob on Windows **and** Linux is the prerequisite for the capability work; do
not start O until NC + ND land. **Read first:** [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md)
— NC consumes C1 (dispatch: it provides the `pwsh` side the shim routes to), C3/C4 (mirrors the
secret/data seams into `_platform.ps1`), and adds the CPU build tier that C5's CI depends on.

**Why this module exists.** NB makes the Python *runtime* OS-agnostic and proves it on Linux CI, but
it deliberately stops there: NB's non-goal is "a Linux/macOS inference provisioner." So after NB you
can *run the core* on Linux by pointing it at any OpenAI-compatible endpoint — but you can't yet
**install and stand up the whole stack** (build llama.cpp with CUDA, download models, start
llama-swap/LiteLLM/whisper/piper, wire clients, manage services) the way `setup.bat` + `bob up` do on
Windows. NC delivers that: **the auto-install/setup experience the user values, extended to Linux at
parity** — so Bob is a working, installable product on both OSes.

**The key enabler (and why this is not a rewrite).** The lock-in was never "PowerShell the
language" — **PowerShell 7 (`pwsh`) runs natively on Linux and macOS.** The lock-in is the
*Windows-specific commands* inside the scripts: `winget`, `Register-ScheduledTask`, WinRT toasts,
`secpol`, `Win32_VideoController`, `.bat` entry points, and Windows paths. So NC keeps the existing
config-based orchestration and makes it **OS-aware** — branch the platform-specific operations behind
one abstraction, run the same `.ps1` under `pwsh` on Linux. The underlying build/serve components are
already cross-platform: llama.cpp/whisper.cpp are CMake+CUDA (build on Linux), llama-swap is a Go
binary, LiteLLM is Python, the services are `docker-compose`. Only the *provisioning glue* needs OS
branches.

**Division of labor after NB+NC.** Runtime verbs (`agent`/`serve`/`mcp`/`chat`) → the portable
`python -m bob` CLI (NB4), no PowerShell. Orchestration verbs (`setup`/`up`/`services`/`build`/`gen`)
→ `pwsh`, now cross-platform (NC). A Linux user installs `pwsh` once (or `setup.sh` bootstraps it),
then gets the identical config-driven install.

**Scope note.** NC targets **Windows + Linux**, primarily **NVIDIA/CUDA**, **plus a first-class
CPU / no-GPU build+serve tier (NC8)** — required so ND2's per-PR CI can provision on GPU-less hosted
runners, and useful for dev boxes without a GPU. macOS/Metal and AMD/ROCm are explicit non-goals here
(a later module) — but the OS-abstraction NC introduces is where they'd slot in.

> **Decision (2026-07-02) — provisioner default (see [C7](ARCHITECTURE-CONTRACTS.md#c7--provisioner-backend-strategy-native-default-now-portable-when-linuxmac-get-real-users)):**
> native-from-source (NC2/NC3) is the **shipped default** *and* the opt-in max-control tier. Portable
> backends — prebuilt binary → `docker compose` inference → BYO OpenAI-compatible endpoint — are **not
> built now**; they become the Linux/macOS default *when those platforms get real daily-driver users*,
> added behind the NC1 seam with **zero caller changes**. Rationale: the seam (NB5 + NC1 +
> `capability_probe`) is the expensive, hard-to-retrofit part and **already exists**; the extra backends
> are cheap-later and speculative-now (no Linux/mac daily-driver users yet), so building a 4-backend
> selector today would be premature abstraction against imagined usage. This changes **no** committed
> NC1–NC8 work — it records the default and confirms the seam is the extension point.

## Overview

| Sub | Name | Windows today → Linux parity | Impact | Effort |
|-----|------|------------------------------|--------|--------|
| NC1 | OS-aware orchestration core (`pwsh` on Linux) | `$IsWindows`/`$IsLinux` branch behind one seam | HIGH | 6–8 h |
| NC2 | Linux prereq bootstrap | `install_prereqs.bat`/`winget` → `setup.sh` + apt/dnf | HIGH | 6–8 h |
| NC3 | Cross-platform build (CUDA) | `build-llama.ps1`/`build-whisper.ps1` on Linux | HIGH | 6–10 h |
| NC4 | Cross-platform service lifecycle | scheduled task / bg proc → systemd/nohup | HIGH | 6–8 h |
| NC5 | Cross-platform model fetch + client wiring | portable download + per-OS config paths | MED | 4–6 h |
| NC6 | Linux GPU/VRAM detection → profile auto-select (+ **degrade w/o GPU**) | `Win32_VideoController` → `nvidia-smi` | MED | 3–4 h |
| NC7 | Cross-platform `bob doctor` + Linux e2e smoke | the "it actually works on Linux" proof | HIGH | 4–6 h |
| NC8 | **CPU / no-GPU build + serve tier** | (new) CUDA-off build + tiny CPU model + no-GPU path | HIGH | 5–7 h |

**Total:** ~40–57 h.

---

## NC1 — OS-aware orchestration core

### Problem
The orchestration assumes Windows throughout: `winget`, `Register-ScheduledTask`, `Get-CimInstance
Win32_VideoController`, WinRT toasts, `secpol`, `%USERPROFILE%`-style paths, and `.bat` entry points.
None of it branches on OS.

### Change
- A single `scripts/_platform.ps1` seam (loaded by `_models.ps1`/`bob.ps1`) exposing OS-neutral
  primitives resolved once via `$IsWindows`/`$IsLinux`/`$IsMacOS`:
  `Install-Package`, `Register-Service`/`Start-Service`/`Stop-Service`/`Get-ServiceStatus`,
  `Get-GpuInfo`, `Send-Notification`, `Get-DataDir`/`Get-CacheDir`, and **`Get-Secret`** (the
  PowerShell mirror of NB3's `osenv.secret()` — C3, so provisioner scripts read provider keys the
  same way the runtime does). Windows implementations wrap
  today's behavior; Linux implementations use apt/dnf, systemd/nohup, `nvidia-smi`, `notify-send`.
- `bob.ps1` and the setup/service scripts call the seam instead of Windows commands directly. The
  `switch` dispatch stays; only the platform touchpoints move behind the seam.
- Mirrors NB3's Python `osenv.py` on the PowerShell side — same concept, orchestration layer.

### Effort: 6–8 h.
### Acceptance
`bob status`/`bob gen` run clean under `pwsh` on Linux (no Windows-only cmdlet errors); the seam
returns correct values on both OSes; `[Parser]::ParseFile` clean; Windows behavior unchanged.

---

## NC2 — Linux prereq bootstrap

### Problem
Prereqs install via `install_prereqs.bat` + `winget` (CUDA, Python 3.12, Go, Node, cmake, Docker) —
Windows-only. Linux has no equivalent entry.

### Change
- `install_prereqs.sh` (+ `setup.sh`): detect the distro's package manager (apt/dnf/pacman), install
  the toolchain (CUDA toolkit, python3.12+venv, golang, nodejs, cmake, git, docker), then **hand off
  to the same `pwsh` setup logic** (install `pwsh` first if absent). No duplicated install logic —
  the `.sh` files are thin bootstrappers; the real work stays in the OS-aware `.ps1` (NC1).
- Idempotent + re-runnable (the existing Windows convention); clear messaging when a package needs a
  manual step (e.g., NVIDIA driver, Docker group membership).

### Effort: 6–8 h.
### Acceptance
On a clean Ubuntu box with an NVIDIA GPU, `./install_prereqs.sh && ./setup.sh` reaches a built,
configured stack (or fails with a precise, actionable message). Re-running is safe.

---

## NC3 — Cross-platform build (CUDA)

### Problem
`build-llama.ps1` / `build-whisper.ps1` assume MSVC/Windows CUDA (DLL outputs, `nvcc` via
`CUDA_PATH`, Windows cmake generator). llama.cpp/whisper.cpp build cleanly on Linux, but the build
scripts don't emit the Linux invocation (Unix Makefiles/Ninja, `.so` outputs, arch flags from the
detected GPU).

### Change
- Branch the build scripts (via NC1's seam): on Linux use the Ninja/Make generator, gcc/nvcc,
  `-DGGML_CUDA=ON` with `CMAKE_CUDA_ARCHITECTURES` derived from the detected compute capability
  (NC6), and resolve `.so`/binary outputs. Reuse the existing CUDA-arch → generation mapping (already
  tested in `test-dry-run.ps1` [1]–[4]) — it's compiler-agnostic.
- Keep `external/llama.cpp` / `external/whisper.cpp` as the source of truth; only the invocation and
  output paths are OS-branched.

### Effort: 6–10 h (build-matrix debugging is the variable).
### Acceptance
`bob build` produces a CUDA-enabled `llama-server` (+ whisper server) on Linux; a tiny inference
returns tokens; the `test-dry-run.ps1` arch-mapping sections pass under `pwsh` on Linux.

---

## NC4 — Cross-platform service lifecycle

> **Decision (2026-07-02): nohup + cron is the baseline backend; systemd is a later, seam-swappable
> option — NOT the primary path.** The architecture here is the NC1 `*-Service` seam; the question is
> only *which backend to land first behind it*, and it must be the one that's **verifiable**. Two
> facts decide it: (1) `systemd --user` units need `loginctl enable-linger` + a real user session,
> which **GitHub Actions ubuntu runners don't provide** — so a systemd-primary path can't start
> services in ND2's CPU-tier CI, the very gate meant to *prove* NC works; and (2) systemd can't be
> tested on the Windows dev box either, so it would land blind. nohup+pidfile + a 1-min cron tick
> works everywhere (containers, CI, WSL, minimal distros), needs no root/linger, and is
> unit-testable. systemd (auto-restart, journald, boot-persistence) is a real improvement but an
> *enhancement*, added behind the same seam and verified on a real box when **P2** (detached
> long-running tasks) needs crash-restart. Choosing the universal, verifiable backend first — with
> the nicer one swappable behind the interface — is the correct sequencing, not a shortcut.

### Problem
Services start as Windows background processes; the agent scheduler uses `Register-ScheduledTask`;
stop/status logic reads Windows PIDs. Linux needs a service-start + scheduler backend that runs
without root/linger and, critically, inside GPU-less CI.

### Change
- Implement the NC1 `*-Service` primitives for Linux with a **`nohup`+pidfile backend** (default):
  `bob up`/`bob services` start llama-swap, LiteLLM, whisper, piper as detached processes with
  pidfiles; **stop/status must be solid** — reuse `Test-PortInUse` + the M10 `Stop-ServiceByPid`
  helper, detect+reap stale pidfiles (never trust a pidfile blindly). Docker services
  (`docker compose`) already work cross-platform — reuse as-is.
- **Scheduler:** `bob agent install` writes a **one-line crontab** running `bob-agent.ps1` every
  minute; the existing `Test-CronDue` cron-expr logic decides which goals are due (the direct analog
  of the Windows scheduled-task → runner model). No linger, no root.
- **Seam contract for the systemd upgrade:** the backend is a selector (`nohup` default; `systemd`
  opt-in when detected *and* linger is enabled) so systemd user units + timer slot in later with
  **zero caller changes**. Document the nohup tradeoff honestly (no auto-restart / journald / boot
  persistence).

### Effort: 6–8 h.
### Acceptance
`bob up` / `bob stop` / `bob status` / `bob services start|stop|status` behave equivalently on Linux
via the nohup backend, **including inside ND2's GPU-less CI**; `bob agent install` registers a
working 1-min cron entry that fires due goals; stale-pidfile is detected; ports/PIDs report
correctly. A systemd backend, when added later, passes the same acceptance behind the same seam.

---

## NC5 — Cross-platform model fetch + client wiring

### Problem
`fetch-models.ps1` and client-config writers (Continue/aider) assume Windows paths and helpers.

### Change
- Portable downloader: `Invoke-WebRequest` is cross-platform under `pwsh`; keep resumable/atomic
  download (Module B) and the SHA256 manifest — only path handling is OS-branched (via NC1
  `Get-DataDir`). Alternatively route downloads through the Python side (already portable).
- Client configs written to the correct per-OS locations (VS Code / Continue / aider config dirs
  differ by OS); reuse the existing generators with OS-branched target paths.

### Effort: 4–6 h.
### Acceptance
`bob fetch` downloads + validates models into the right Linux data dir; Continue/aider pick up the
generated config on Linux; checksums verified.

---

## NC6 — Linux GPU/VRAM detection → profile auto-select (+ degrade with no GPU)

### Problem
Profile auto-selection reads VRAM via `Get-CimInstance Win32_VideoController` (Windows-only). Without
it, Linux can't auto-pick the 8/12/16/24/32 GB profile. And on a **GPU-less** box (CI hosted runners,
dev laptops) `nvidia-smi` is absent — today `Get-GpuVramGB` returns `$null` and `bob profile auto`
errors ("Cannot detect GPU VRAM"), which would break the NC8/ND2 CPU tier.

### Change
- Implement NC1's `Get-GpuInfo` on Linux by parsing `nvidia-smi --query-gpu=name,memory.total,compute_cap
  --format=csv`. Feed the existing profile-suggestion logic (already unit-tested in
  `test-dry-run.ps1` [5]) — VRAM-number-driven and OS-agnostic once fed.
- **Degrade cleanly with no GPU:** when `Get-GpuInfo` finds none, `bob profile auto` selects the
  **`cpu` profile** (NC8) with a clear message instead of erroring; `bob doctor` reports "no GPU →
  CPU backend" rather than failing.

### Acceptance
`bob profile auto` picks the right GPU profile on a Linux NVIDIA box **and** falls back to `cpu`
with no GPU (no error); the suggestion tests pass under `pwsh` on Linux.

### Effort: 3–4 h.
### Acceptance
On a Linux NVIDIA box, `bob profile auto` selects the same profile the VRAM warrants; the
suggestion tests pass under `pwsh` on Linux.

---

## NC7 — Cross-platform `bob doctor` + Linux end-to-end smoke

### Problem
`bob doctor` (M11) checks Windows-specific conditions; nothing proves a *fresh Linux install*
actually works end-to-end.

### Change
- Make `bob doctor` OS-aware (endpoint reachability, GPU via NC6, data/cache writable, config
  resolves via NB2, services up via NC4) — one honest pre-flight on both OSes.
- A scripted **Linux end-to-end smoke**: from a built+configured box, `bob up` → wait for readiness →
  `bob agent "say hi"` returns an answer → `bob agent serve` answers `/health` + an owner-scoped
  session turn (N1) + an SSE stream (N3/N6). This is the "reliable working Bob on Linux" gate.

### Effort: 4–6 h.
### Acceptance
`bob doctor` passes on a healthy Linux install and pinpoints failures precisely; the e2e smoke goes
green on a real Linux+CUDA box. This smoke is what ND promotes into the release matrix.

---

## NC8 — CPU / no-GPU build + serve tier

> **Decision (2026-07-02): `cpu` profile = Qwen2.5-0.5B-Instruct (Q8_0); and the CPU-tier smoke is
> scoped to *serve + coherent answer*, NOT a real-model tool round-trip.** The architectural error to
> avoid is conflating two concerns: (a) *does the stack stand up and serve on a GPU-less box* — a
> wiring proof, and (b) *does a model reliably emit a well-formed tool call* — a model-quality
> property. You **cannot build a reliable gate on an unreliable model**, and both 0.5B and 1B are
> flaky at structured tool-calling on CPU/greedy. So tool-protocol correctness stays where it's
> already **deterministic** — the N-era fake-client unit tests (hermes + OpenAI wire formats) — and
> the CPU-tier smoke asserts only what it uniquely can: provisions → serves → `bob agent "say hi"`
> gets a coherent answer. With the smoke model-agnostic, pick the **smallest model that loads and
> generates → Qwen2.5-0.5B-Instruct** (cheapest download, fastest CPU inference, least flaky,
> same-family as the existing Qwen roles so the chat template is understood). Its weakness at
> tool-calling is irrelevant because the gate no longer depends on it.
>
> **If** a real-model tool round-trip in CI is later wanted (fakes can drift from reality), add it as
> a **separate, non-blocking (advisory)** job with a 1B model — never a gating step, so a bad model
> day can't red the gate. `verify-before-use`, pinned in ND1's `versions.lock` either way.

### Problem
NC1–NC7 assume NVIDIA/CUDA: NC3 builds `-DGGML_CUDA=ON`, NC2 installs the CUDA toolkit as a required
prereq, NC6 profiles from `nvidia-smi`. But **ND2's per-PR CI acceptance runs on GPU-less hosted
runners** (and dev boxes may have no GPU). Without a CPU path, the reliability gate that ND's whole
value rests on cannot build or serve — it would be perpetually red or skipped. This is a hard
dependency ND2 assumes but no NC item currently provides.

### Change
- A **CPU build variant**: `bob build --cpu` (and auto-selected when `Get-GpuInfo` finds none) emits
  a `-DGGML_CUDA=OFF` build; CUDA toolkit becomes an *optional* prereq (NC2 skips it in CPU mode).
- A **`cpu` profile** = **Qwen2.5-0.5B-Instruct (Q8_0, ~0.5 GB)** — the smallest model that reliably
  loads + generates on a CPU CI runner. Pinned in `versions.lock` (ND1), `verify-before-use`.
- The runtime + orchestration **degrade cleanly** with no GPU (NC6): `bob profile auto` → `cpu`,
  `bob doctor` reports the CPU backend, services start CPU-only, `bob agent "say hi"` returns.
- **Smoke scope:** the CPU tier proves *provisioning + serve + reachability + a coherent answer* — it
  is **model-agnostic** and does **not** gate on the tiny model emitting a tool call. Tool-protocol
  correctness is covered deterministically by the fake-client unit tests (N-era).
- Not for real use — it's the *provable* tier: wiring + serve, not performance or tool reliability.

### Effort: 5–7 h.
### Acceptance
On a GPU-less Linux (and Windows) box, `install_prereqs` (CPU mode) → `setup` → `bob build --cpu` →
`bob up` → `bob agent "say hi"` returns a **coherent answer** using the Qwen2.5-0.5B CPU model;
`serve` health + an owner-scoped session turn work; the NC7 smoke passes with **no GPU present**. The
gate does **not** assert a real-model tool call (that stays in the fake-client unit tests). This is
exactly the path ND2's per-PR CPU tier runs.

---

## Traceability (goal → sub-item)

| Goal | Sub-item(s) |
|------|-------------|
| Orchestration runs on Linux (same scripts, OS-aware) | **NC1** |
| Auto-install prereqs on Linux (parity with Windows) | **NC2** |
| Build the CUDA inference stack on Linux | **NC3** |
| Start/stop/schedule services on Linux | **NC4** |
| Download models + wire clients on Linux | **NC5** |
| Auto-pick the VRAM profile on Linux (+ degrade w/o GPU) | **NC6** |
| Prove a fresh Linux install actually works | **NC7** |
| Build + serve with no GPU (CI tier / dev boxes) | **NC8** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| new `scripts/_platform.ps1`; `scripts/_models.ps1`, `scripts/bob.ps1` | NC1 |
| new `install_prereqs.sh`, `setup.sh` | NC2 |
| `scripts/build-llama.ps1`, `scripts/build-whisper.ps1` | NC3 |
| `scripts/up.ps1`, `scripts/start-*.ps1`, service/scheduler logic in `bob.ps1` | NC4 |
| `scripts/fetch-models.ps1`, client-config generators | NC5 |
| `scripts/diagnose.ps1` / GPU detection helpers | NC6 |
| `bob doctor` in `bob.ps1`; new `scripts/smoke-linux.ps1` | NC7 |
| `build-llama.ps1` (`--cpu`), `config/models.psd1` (`cpu` profile), `fetch-models.ps1` (tiny CPU GGUF) | NC8 |
| `docs/SETUP.md`, `docs/PORTABILITY.md`, `docs/MANUAL-INSTALL.md` | docs |

## Verification

- `[Parser]::ParseFile` on all `.ps1` under `pwsh` (Windows + Linux); `check.ps1` gate (N8) green on
  both (via NB6 CI).
- **Windows regression:** `setup.bat` + `bob up` + `.\scripts\test-dry-run.ps1` unchanged.
- **Linux end-to-end (NC7):** clean box → `install_prereqs.sh` → `setup.sh` → `bob up` → `bob agent`
  answers → `serve`/`/health`/session/SSE work — no `.bat`, no Windows-only cmdlet in the path.
- Cite `file:line` for every claim.

## Non-goals

macOS/Metal and AMD/ROCm (later — the NC1 seam is where they'd land). Rewriting orchestration in
bash/Python (it stays `pwsh`, now cross-platform). The release/packaging + fresh-install acceptance
matrix — that's **Module ND**. The agent-loop capability work — **Module O**, gated behind NB+NC+ND.
