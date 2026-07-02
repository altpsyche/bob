# Module ND — Release, Reproducibility & Cross-OS Acceptance

**Status:** draft / not implemented. **Depends on:** NB (portable core) + NC (Windows+Linux
provisioner, incl. the **NC8 CPU tier** ND2 runs on). **Precedes:** NE → O → P — ND is the gate that
says "Bob reliably installs and works on Windows and Linux," the entry condition for capability work.
**Read first:** [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) — ND2 *extends* the CI
workflow NB6 created (C5, never re-creates it) and depends on NC8's CPU build for the GPU-less tier.

**Why this module exists.** NC makes Bob *function* on Linux; NB6 proves the *unit* suite runs on
Linux. Neither proves the thing that matters for "reliable working Bob": that a **fresh machine**,
following the documented steps, ends up with a working stack — repeatably, on both OSes, and that you
can **ship and update** it. "Works on my box" is not reliability. ND turns two-OS *capability* into
two-OS *dependability*: pinned/reproducible installs, an automated fresh-install acceptance matrix,
and a real release/update path. This is the capstone that finalises the portability circle
(N → NB → NC → **ND**) before O begins.

**Scope note.** ND is about *dependability and distribution*, not new features. It adds no runtime
capability; it makes what NB+NC built trustworthy to install and upgrade. Targets Windows + Linux
(NVIDIA/CUDA), matching NC.

## Overview

| Sub | Name | Turns "runs" into "reliably installs/updates" | Impact | Effort |
|-----|------|----------------------------------------------|--------|--------|
| ND1 | Reproducible setup (pin + checksum everything) | non-deterministic installs → a locked, verifiable manifest | HIGH | 4–6 h |
| ND2 | Cross-OS acceptance matrix (fresh-install gate) | "worked once" → automated Win+Linux clean-install proof | HIGH | 6–8 h |
| ND3 | Release & versioning (`bob version`/`update`) | ad-hoc pulls → versioned releases + a safe upgrade path | MED | 4–6 h |
| ND4 | Packaging + supported-matrix doc | scattered scripts → one-command install per OS, documented | MED | 3–4 h |
| ND5 | Unified cross-platform SETUP docs | two implicit stories → one accurate Win/Linux guide | LOW | 2–3 h |

**Total:** ~19–27 h.

---

## ND1 — Reproducible setup (pin + checksum everything)

### Problem
A "reliable" install must be deterministic, but today versions float: the toolchain (CUDA/Python/Go/
Node/cmake), the `external/*` submodule commits, the Python deps, and the model files aren't pinned
as one coherent, verifiable set. Two people (or the same person next month) can get different builds.
Model integrity has a SHA256 manifest (Module D) but the *rest* of the stack doesn't.

### Change
- A single **`versions.lock`** (neutral JSON/TOML, read by both `pwsh` and Python) pinning: submodule
  commits (`external/llama.cpp`, `whisper.cpp`, `fabric`), a `requirements.lock` for each venv,
  minimum toolchain versions, and the model manifest (name → repo → revision → SHA256) — **including
  the tiny CPU GGUF the NC8 CPU tier serves in CI**. The provisioner (NC) installs *from the lock*,
  not "latest."
- Verify-on-install: checksums for downloaded models/binaries; submodules checked out to the pinned
  commit; venvs installed from the lockfile. A mismatch fails loudly (loud-fail convention).
- `bob doctor` (NC7) gains a "reproducibility" check: are the installed versions == `versions.lock`?

### Effort: 4–6 h.
### Acceptance
Two clean installs from the same `versions.lock` on the same OS produce identical builds/model
checksums; a drifted submodule or dep is flagged by `bob doctor`; the model manifest still validates.

---

## ND2 — Cross-OS acceptance matrix (the fresh-install gate)

### Problem
NB6 runs *unit tests* on Linux; NC7 is *one manual* Linux e2e. Nothing automatically proves that a
**clean machine** → documented steps → working Bob, on **both** OSes, and nothing gates a change on
that. That automated fresh-install proof *is* "reliable."

### Change (extends the NB6 CI workflow — C5, does not create it)
- **Add a matrix to the existing `.github/workflows/ci.yml`** (NB6 owns the file; ND2 appends jobs).
  Each job provisions from scratch per the docs (`install_prereqs` → `setup` → `bob up`) against
  `versions.lock` (ND1), then runs the NC7 end-to-end smoke: build present → services up →
  `bob agent "say hi"` answers → `serve` health + owner-scoped session (N1) + SSE stream (N3/N6).
- Two tiers so it's affordable: a **fast CPU tier** on hosted Windows+Linux runners using the **NC8
  CPU build + tiny CPU GGUF** (proves the provisioner + wiring + runtime path end-to-end without a
  GPU) run on every PR; a **full GPU tier** (real inference) on a self-hosted runner, run on release
  tags. Promote NC7's `smoke-linux.ps1` into a shared cross-OS `smoke.ps1`.
- **Gating scope (C7):** the per-PR **gate is the portable/CPU tier only**. **Native-from-source CUDA
  builds are exercised solely in the non-gating GPU/release-tag tier** — a fragile native build can
  never red the per-PR gate. This is the ND consequence of the
  [C7 provisioner-strategy decision](ARCHITECTURE-CONTRACTS.md#c7--provisioner-backend-strategy-native-default-now-portable-when-linuxmac-get-real-users):
  the reproducible portable path is what every PR must prove; native-from-source is a heavier,
  release-time tier, not a merge blocker.
- Green matrix = release-eligible; red = blocked.
- **Realism note:** building llama.cpp (even CPU) in CI is minutes-scale; cache the build + submodule
  checkouts by `versions.lock` hash so per-PR runs are incremental, not from-scratch every time.

### Effort: 6–8 h.
### Acceptance
The CPU tier runs on every PR on both OSes and blocks on failure; the GPU tier runs on release tags;
a deliberately broken provisioner step (e.g., a wrong pinned version) fails the matrix. A fresh
Windows *and* Linux install is demonstrably reproducible from CI logs.

---

## ND3 — Release & versioning (`bob version` / `bob update`)

### Problem
`bob update` (Module D) is submodule-aware on Windows but there's no *versioned release* concept, no
changelog, and no safe cross-OS upgrade path (pull → rebuild only what changed → re-verify).

### Change
- Tagged releases carrying the `versions.lock` (ND1); `bob version` reports the release + component
  versions on both OSes; `bob update` becomes cross-platform (via NC1's seam): fetch the target
  release, update submodules/deps/models to the new lock, rebuild only what changed, run `bob doctor`,
  and roll back on failure (atomic-swap the build output — the Module B `.bak` pattern, generalized).
- A `CHANGELOG.md` generated from the module history; upgrades are lockfile-to-lockfile and verifiable.

### Effort: 4–6 h.
### Acceptance
`bob update` moves a working install from release N to N+1 on both OSes, rebuilding only changed
components, verifying via `bob doctor`, and rolling back cleanly on a simulated mid-upgrade failure.

---

## ND4 — Packaging + supported-matrix doc

### Problem
Install is "clone + run scripts." Fine, but there's no one-command entry per OS and no explicit
statement of what's actually tested/supported — a reliability and trust gap.

### Change
- A single documented entry per OS: `install_prereqs.bat`/`setup.bat` (Windows) and
  `install_prereqs.sh`/`setup.sh` (Linux) as the blessed one-command paths (they already exist after
  NC2 — ND *documents and version-stamps* them, doesn't reinvent).
- A **Supported Matrix** table: OS × GPU generation × profile, each cell marked
  tested/works/unsupported, kept honest by the ND2 matrix results. macOS/AMD explicitly listed as
  "not yet."

### Effort: 3–4 h.
### Acceptance
A newcomer on either OS has exactly one documented command to start; the Supported Matrix reflects
what ND2 actually proves (no aspirational "works everywhere" claims).

---

## ND5 — Unified cross-platform SETUP docs

### Problem
`SETUP.md` / `MANUAL-INSTALL.md` / `README.md` are written Windows-first; a Linux user pieces the
path together. After NB+NC that's inaccurate as much as incomplete.

### Change
- One SETUP that presents both OS paths side by side (prereqs, build, first run, verify), pointing at
  the ND4 one-command entry and the `bob doctor` pre-flight. `PORTABILITY.md` (from NB/NC) becomes the
  "how the split works" reference; SETUP becomes the "how to install" guide for both OSes.

### Effort: 2–3 h.
### Acceptance
A reader following SETUP on a clean Linux box, and another on Windows, both reach a working `bob up`;
docs match what the ND2 matrix runs.

---

## Traceability (goal → sub-item)

| Goal | Sub-item(s) |
|------|-------------|
| Installs are deterministic + verifiable | **ND1** |
| A fresh install is *proven* working on Windows + Linux, automatically | **ND2** |
| Ship + safely upgrade across OSes | **ND3** |
| One documented install path per OS; honest support claims | **ND4** |
| Accurate, unified install docs | **ND5** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| new `versions.lock`, `requirements.lock`(s); `bob doctor` repro check | ND1 |
| `.github/workflows/ci.yml` (**extend** NB6's file with the matrix — C5); new shared `scripts/smoke.ps1` | ND2 |
| `bob version`/`bob update` in `bob.ps1` (via NC1 seam); new `CHANGELOG.md` | ND3 |
| `install_prereqs.*`, `setup.*` (stamp/doc); `README.md` Supported Matrix | ND4 |
| `docs/SETUP.md`, `docs/MANUAL-INSTALL.md`, `docs/PORTABILITY.md` | ND5 |

## Verification

- ND2 matrix is the top-level proof: green fresh-install + e2e smoke on Windows and Linux (CPU tier
  every PR, GPU tier on release tags).
- `bob doctor` reports reproducibility (installed == `versions.lock`) on both OSes.
- `bob update` round-trips N→N+1→(rollback on failure) on both OSes.
- `check.ps1` (N8) + the NB6 core suite stay green throughout. Cite `file:line` for every claim.

## Non-goals

New runtime capability (that's Module O). macOS/AMD support (later). A GUI installer or app-store
packaging (the one-command scripts are the target). Hosting/distributing model weights (we pin repo
+ revision + checksum and download from the source).

---

## The full circle (see ARCHITECTURE-CONTRACTS.md for the authoritative graph)

```
N ✓ → NB → NC → ND → NE → O → P
```
- **NB** portable core · **NC** cross-platform provisioner (+CPU tier) · **ND** reproducible +
  auto-proven + shippable · **NE** one coherent front door · **O** frontier capability · **P**
  frontier product (durable runs, multimodal, computer-use).

**Do NB → NC → ND (→ NE) before O.** O adds power to the agent brain; NB/NC/ND make sure that brain
runs reliably and installs on more than one OS first. Building O on a Windows-only, drift-prone base
would just deepen the hole this track exists to fill.
