# Module ONE-D — Kill PowerShell (the toolchain / privilege / cold-start tail) — detailed plan

**Status:** deep scoping DONE — **not started** (no code yet). This is the ONE-D companion to
[MODULE-ONE-C-plan.md](MODULE-ONE-C-plan.md). ONE-C is feature-complete on `main` (suite 686 green): 52
verbs on Python, **no pwsh in the lifecycle hot path**. ONE-D ports the remaining 9 pwsh verbs + the tails
ONE-C deferred, and — the hard part — the **pre-venv cold-start kernel**, after which every `*.ps1` under
`scripts/` can be deleted (ONE-E). **Read first:** [MODULE-ONE-bob.md](MODULE-ONE-bob.md) (§ONE-D, the
architectural invariant, the deprecation ledger, **the one honest exception = the bootstrap kernel**),
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) (**C1 dispatch phased migration, C7 provisioner
native-first**, C5 CI ownership), [MODULE-ONE-C-plan.md](MODULE-ONE-C-plan.md) (D1–D6 settled; the
per-verb 3-adapter template; the osenv-seam precedent). ONE-C decisions D1–D6 are **settled — do not
re-litigate.** The new decisions **DD1–DD6 are RESOLVED (2026-07-05, see Part 5):** full Python `build` port ·
curl-subprocess `fetch` · keep `venv-eval` · `mlock` grant ported CLI-only · minimal-shell-stub → Python
kernel · ONE-D owns the kernel (D8) as capstone. **Slices D0–D4 ✅ DONE (suite 766 green); next = D5 (build — the big native port).**

## Goal — and the one distinction that shapes the whole module

Same invariant as ONE-C: every capability is **one importable Python function** reached by thin adapters
(agent `tool.py` / `bob --run <cap>` / the kernel). But ONE-D adds a split that ONE-C never had to make,
because **several ONE-D verbs run before the Python venv exists** and therefore *cannot* be a `--run`
capability (there is no interpreter to run them through yet):

```
                                 ┌─ agent tool + bob --run    (post-venv: the loop / CI call it)
port target for each verb  ──────┤
                                 └─ cold-start KERNEL fn        (pre-venv: scripts/bob/kernel.py calls it
                                                                 under the *system* python3, no venv)
```

- **CAPABILITY** (post-venv): `fetch`, `eval`, `mlock` (check), `lock`, `setup-voice`, `setup-clients`,
  `onboard`, `install-cli`, and the *rebuild* path of `build`/`update`. These need only Python (± the
  venv) + `models.json` + `requests`/`curl`; they become the standard 3-adapter capabilities and the
  kernel *also* calls them.
- **KERNEL** (pre-venv): `install-prereqs` (toolchain + a Python interpreter), the venv creation
  (`bootstrap*.ps1` / `New-BobVenv`), the *first* `build`, and the *first* config generation. These run
  under a bare interpreter before any venv exists. They port into **`scripts/bob/kernel.py`**, run by the
  shrunk `setup.sh`/`setup.bat`/`install_prereqs.*` entrypoints.

The magic of the invariant: the kernel does not *re-implement* fetch/gen/build — it **imports the same
capability functions**. So the only cold-start script that today reaches into pwsh (`bootstrap.ps1` →
`gen-llama-swap.ps1`) stops doing so the moment the kernel is Python: it calls `generate.gen_all()`
directly, and `gen-*.ps1` + `start-*.ps1` retire with zero drift (they were byte-parity ports already).

C7 governs the depth: **native-from-source stays the default**, exercised only in the non-gating
GPU/release-tag CI tier. ONE-D does **not** build a portable/docker provisioner — it ports the *native*
toolchain orchestration to Python behind the build-time osenv seams (Part 1b). DD1 fixes exactly how far.

---

## Part 0 — What ONE-C already left us (so nothing is re-done)

| Already on Python (reuse, do not re-port) | Where |
|---|---|
| Lifecycle: `up/serve/restart/stop/ps/logs/webui/litellm/whisper/piper/services` | `scripts/tools/stack.py` (Slice 2) — replaces `start-*.ps1`/`up.ps1` behaviourally |
| Config generators (byte-parity, all 4) | `scripts/tools/generate.py` (Slice 6) — replaces `gen-*.ps1` |
| Model registry read + profile switch | `scripts/tools/models.py` + `scripts/bob_models.py` (Slice 4, C0c) |
| Health/diagnose (light half) + version | `scripts/tools/health.py` (Slice 3) |
| Scheduler quartet + runner | `osenv.py` + `scripts/bob_agent_runner.py` + `scripts/tools/schedule.py` (Slice 5) |
| **versions.lock READER + reproducibility check + streaming sha256** | **`scripts/bob/versions.py`** — `load_lock`/`verify_model`/`check_reproducibility` already exist |
| `bob --run <cap>` deterministic invoker; run/process/path/url osenv seams | `bob/cli.py` `_handle_run`; `osenv.py` (C0) |

**Two immediate quick wins (readers already exist — pure wiring, no new port):**
1. **doctor's degraded `versions.lock reproducibility` row** ([health.py:239](../../scripts/tools/health.py))
   → call `bob.versions.check_reproducibility()` (already written). Flip `○ pending ONE-D` → real rows.
2. **doctor/setup's degraded `BobAgent task registered` row** ([health.py:170](../../scripts/tools/health.py))
   → call `osenv.agent_task_status()` (landed in Slice 5). Flip `○ pending Slice 5` → real check.

Both are the ONE-C follow-ups I flagged; land them in **Slice D0** below.

---

## Part 1 — Shared infrastructure (build FIRST; the build/kernel slices depend on it)

### 1a. versions.lock **writer** + sync-gate (the reader is done)
`scripts/bob/versions.py` reads + verifies today. Missing (all in `_versions.ps1`, port into `versions.py`):
- `write_lock()` ← `Write-VersionsLock`/`New-VersionsLockObject`/`Get-VersionsLockText` — build the ordered
  lock object (`lockVersion/release/submodules/toolchain/requirements/models`), canonical `json.dumps`
  (depth-6 equivalent), atomic write + trailing `\n`. **Byte-parity target vs the pwsh serialization** (like
  the Slice 6 generators) so `bob lock --check` stays a clean equality gate.
- `submodule_commits()` ← `Get-SubmoduleCommits` (`git rev-parse HEAD:<path>` — reads the gitlink, works
  with submodules unchecked-out, e.g. CI core-suite).
- `lock_model_manifest()` ← `Get-LockModelManifest` — union of every profile's gguf keyed by filename,
  sha from `models/manifest.json` else the already-locked sha (TOFU-then-lock; **the fallback matters** —
  the gate must regenerate from committed sources on a machine that hasn't fetched every model). Reuse
  `bob_models.load_models_config()` for the profile/repo/path/revision/sizeGB/mmproj fields.
- `check_sync()` ← `Test-VersionsLockSync` — canonical-text equality; returns 0/1.
- **Rewire the gate:** `check.ps1` currently dot-sources `_versions.ps1` and calls `Test-VersionsLockSync`
  ([check.ps1:64-71](../../scripts/check.ps1)); repoint it at `python -m bob.versions --check` (or
  `bob --run lock_check`). Same for `ci.yml`. `_versions.ps1` cannot be deleted until this is done.
- Constants to carry over: `LockSubmodules` (the 4 submodules incl. `external/llama-swap`),
  `LockToolchain` (`python 3.12 / cmake 3.24 / cuda 12.0` floors), `LockRequirements` (`venv-litellm →
  tools/litellm-requirements.lock`). These are data — fine as module constants (single source, no drift).

### 1b. Build-time / deep-OS osenv seam-gap table (the OS core of build/diagnose/mlock/prereqs)
Runtime/lifecycle/scheduler/secret/data seams all landed in ONE-C. **What is entirely absent from
`osenv.py` is the build-time + hardware-discovery family.** Add these (mirroring the pwsh `Resolve-*`
pure-descriptor / executor split; keep the `BOB_FORCE_OS` test hook). Difficulty ranked.

| Seam (new) | pwsh source | Windows | Linux | Difficulty |
|---|---|---|---|---|
| `gpu_arch()` | `_models.ps1 Get-GpuArch` | nvidia-smi `compute_cap` (same) | same | **easy — already written twice**, in `tools/health.py:gpu_arch` + `tools/models.py`; **consolidate into osenv, delete the dups** |
| `gpu_vram_gb()` | `Get-GpuVramGB` | nvidia-smi `memory.total` (same) | same | easy — dedupe with above |
| `gpu_info()` | `Get-GpuInfo` | composes the two | same | trivial |
| `system_ram_gb()` | `Get-SystemRamGB` | CIM `Win32_OperatingSystem` | parse `/proc/meminfo` | easy (real OS fork) |
| `numa_node_count()` | `Get-NumaNodeCount` | CIM `Win32_NumaNode` | count `/sys/devices/system/node/node*` | easy (real fork) |
| `resolve_cuda_root_candidates(arch)` | `Resolve-CudaRootCandidates` | `Base=…\CUDA`, `DirPrefix='v'`, pin `v12.8`@sm120 | `Base=/usr/local`, `DirPrefix='cuda-'`, `Fixed=[/usr/local/cuda,/opt/cuda,$CUDA_HOME,$CUDA_PATH]` | **hard — pure descriptor** |
| `cuda_toolkit_version(root)` | `Get-CudaToolkitVersion` | `version.json`→`version.txt`→`nvcc --version` | same | medium |
| `best_cuda_root(arch)` | `Get-CudaRoot`/`Get-BestCudaRoot` | rank named dirs + fixed roots, newest ≥ `MinVer` | same + heavy reliance on disk-read version (unversioned symlinks) | **hard — the ranking; pin is a floor not exact-match (12.9/13.x qualify for sm120)** |
| `cuda_host_compiler()` | `Get-CudaHostCompiler` | no-op | newest `g++-NN` < default, honor `$NVCC_CCBIN` | medium (Linux-only) |
| `assert_cuda_host_compiler_ok(nvcc,cxx)` | `Assert-CudaHostCompilerOk` | no-op | compile a trivial `__global__` kernel; fail fast | medium (Linux-only) |
| `resolve_build_cmake_flags(cpu,arch)` | `Resolve-BuildCmakeFlags` | `Visual Studio 17 2022`, stage CUDA DLLs | `Ninja`, rpath/ldconfig, no staging | medium |
| `linux_cmake3(repo)` | `Get-LinuxCmake3` | n/a (winget pin) | system cmake if <4.0 else fetch+cache pinned Kitware 3.31.7 tarball into `tools/` | medium (network) |
| `linux_package_manager()` / `linux_os_family()` | `Get-LinuxPackageManager`/`Get-LinuxOsFamily` | n/a | probe apt/dnf/pacman/zypper; parse `/etc/os-release` | easy |
| `resolve_package_cmd()` / `install_package()` / `PACKAGE_MAP` | `Resolve-PackageCmd`/`Install-Package`/`$PackageMap` | winget | sudo + mgr; logical→concrete name table | medium (prereqs only) |
| `backup_build_output(path)` / `restore_build_output` / `remove_build_output_backup` | `Backup-/Restore-/Remove-BuildOutput` | dir↔`.bak` (cross-platform) | same | easy |
| Python provisioning: `python_at_least()` / `install_uv()` / `bob_python()` / `bob_venv_python()` / `new_bob_venv()` | `_platform.ps1:428-528` | scoop/py-launcher | uv-provisioned CPython | **hard — the venv-creator, chicken-and-egg; kernel-only** |
| ~~`curl_exe()`~~ | `Get-CurlExe` | — | — | **drop** — use `subprocess` curl by name, or `requests` (DD2) |

Already mirrored (no work): `os_name`, `secret`, `data_dir`/`cache_dir`, `bin_exe`/`venv_exe`/`exe_name`/
`home_config_dir`, the process + scheduler + audio seams. **Config seams stay OUT of osenv** (they have
homes in `bob_models.py`/`bob_core.py`/`bob_config.py`): `Get-Models`/`Get-ModelsConfig`/
`Resolve-ProfileName`/`Set-ActiveProfile`/`Get-SuggestedProfile`/`Get-EnabledPeers`/routing/ports.

### 1c. No new gating prerequisite
Unlike ONE-C (where `models.psd1`→`models.json` gated everything), ONE-D has **no data-format blocker**:
`models.json` is already neutral and `bob_models.py` reads it. The gating risk here is **CI**: the CPU
acceptance matrix + the distro-prereq matrix (Part 4) call the pwsh scripts directly and must be rewired
step-by-step as each verb flips — see the CI note in Part 3.

---

## Part 2 — Full verb + tail inventory (disposition: CAPABILITY vs KERNEL)

Disposition legend as ONE-C: **AGENT** = in-loop tool; **CLI** = `bob`/`--run` only (blocking/privilege/
long); **KERNEL** = pre-venv, into `kernel.py`. Many are **dual** (kernel calls the capability fn).

### The 9 remaining pwsh verbs

| Verb | pwsh source | Does | Disposition | Port risk |
|---|---|---|---|---|
| `fetch` | `fetch-models.ps1` (173) | download active-profile GGUFs: resume (`curl -C -`), SHA256 verify vs versions.lock, TOFU manifest write, mmproj, disk pre-check | **AGENT (mutating, long) + CLI**, **also KERNEL** (bootstrap calls it) | resumable DL + `versions.verify_model` (exists) + manifest writer + `bob_models` roles; DD2 = curl vs requests. **Recommended FIRST** — agent-facing, unblocks nothing else but is self-contained |
| `lock` | `_versions.ps1` + `bob.ps1` case | (re)write versions.lock from sources; `--check` = CI gate | **CLI + AGENT (read-only for --check)** | Part 1a writer + gate rewire; **byte-parity** |
| `mlock` | `grant-mlock.ps1` (167) | `-Check` status (both OS); grant = secedit+UAC (Win) / ulimit guidance (Linux) | **check → AGENT (read-only)**; **grant → CLI-only** (privilege) | DD4 = reproduce UAC self-elevation + secedit-INF rewrite in Python (ctypes `ShellExecuteW runas`) vs a minimal shim; feeds diagnose/doctor mlock row |
| `eval` | `eval.ps1` (64) | lm-eval quality bench via `venv-eval` | **CLI-only (very long)** | shells `venv_exe('venv-eval','lm_eval')`; reads tokenizer from `models.json` (`bob_models`); endpoint check. DD3 = keep the separate venv |
| `setup-voice` | `setup-voice.ps1` (189) | provision whisper model + piper binary/voice + audio deps + smoke | **CLI + AGENT (mutating, long)** | post-venv; downloads + archive extract + `build-whisper.ps1` (native) + `start_whisper` (stack.py); reuses `fetch`-style DL |
| `fabric-setup` | `setup-fabric.ps1` (81) | Go build fabric submodule + `~/.config/fabric` | **CLI**, **also KERNEL** (bootstrap step) | Go build subprocess + `home_config_dir('fabric')`; native (Go toolchain) |
| `build` | `build-llama.ps1` (207) + `build-llama-swap.ps1` | (re)build llama.cpp (CUDA/CPU) + llama-swap (Go); bin swap | **CLI**, **also KERNEL** (first build) | **hardest** — cmake/VS/Ninja/CUDA-host-compiler/DLL-staging via the 1b build seams; DD1 = how far to port vs one surviving native script |
| `update` | `bob.ps1 update` case | git ff/checkout + submodule sync + venv reinstall + conditional rebuild w/ bin rollback + relock + doctor | **CLI** | orchestration over `fetch`/`build`/`lock`/`doctor` + `backup/restore_build_output`; lands after build+lock |
| `setup` (full) | `setup.ps1` (213) | the 12-step fresh-machine orchestrator | **KERNEL** (the capstone) | Part 3 — the cold-start kernel itself |

### The ONE-C tails ONE-D owns

| Tail | Source | Disposition | Notes |
|---|---|---|---|
| doctor's `versions.lock` degraded row | health.py:239 | wire-up | reader exists → **Slice D0 quick win** |
| doctor/setup's `BobAgent task` degraded row | health.py:170 | wire-up | `osenv.agent_task_status` exists → **Slice D0 quick win** |
| `diagnose` DEEP OS discovery | `diagnose.ps1` (196) | fold into `health.diagnose` | CUDA-toolkit resolve / system RAM / NUMA / mlock priv / Linux package mgr rows — needs the 1b seams (`best_cuda_root`, `system_ram_gb`, `numa_node_count`, `linux_package_manager`, `mlock_status`). Retires `diagnose.ps1` |
| retire `gen-*.ps1` | 4 files | delete | only `bootstrap.ps1` (pre-venv) still calls one directly → dies when the kernel imports `generate.gen_all` |
| retire `start-*.ps1` + `up.ps1` | 5 files | delete | behaviourally replaced by `stack.py` (Slice 2); `start.ps1`/`up.ps1` are the last callers, both in the cold-start/kernel path |
| retire `bootstrap*.ps1` + `setup-clients.ps1` + `onboard.ps1` + `install-cli.ps1` | pre/post-venv | port → kernel/capabilities | Part 3 |

### Test / CI pwsh harness (ONE-D must rewire; deletion in ONE-E)
`check.ps1` (the gate — already calls `python -m bob.registry --check`; add the lock gate), `smoke.ps1`/
`smoke-linux.ps1`, `test-platform.ps1`, `test-dry-run.ps1`, and `ci.yml`'s `pwsh -File scripts/bob.ps1 …`
steps (`build --cpu`, `fetch`, `profile`, `up`, `diagnose`, `bootstrap-litellm.ps1`). Each flips as its
verb lands; the **CPU acceptance matrix + distro-prereq matrix are the real gate** — keep them green.

---

## Part 3 — The cold-start kernel (`scripts/bob/kernel.py`) — the hard part

This is "the one honest exception" from the module doc: an agent can't boot its own brain, so a
non-conversational path survives. Today that path is a chain of pwsh; ONE-D ports it to Python. **Two
tiers** — the distinction is *what interpreter is guaranteed to exist*:

```
TIER 0  — the shell prereq installer (bare machine: NO python yet)
  install_prereqs.sh / .bat  →  today: install pwsh, hand to install-prereqs.ps1
                                 ONE-D: ensure a system python3 (one package-manager call),
                                        then hand to  python3 -m bob.kernel prereqs
  install-prereqs.ps1 (363)  →  install_prereqs.py  [KERNEL: winget/scoop | apt/dnf/pacman/zypper,
                                 CUDA repo, uv-provisioned CPython 3.12, Go/Node/cmake/Docker]

TIER 1  — the Python cold-start kernel (python3 exists; venv does NOT)
  setup.sh / .bat  →  python3 -m bob.kernel setup
  scripts/bob/kernel.py  ← ports setup.ps1 (orchestration) + bootstrap.ps1 (the venv-creating pivot):
     1. diagnose            → health.diagnose (deep, once 1b seams land)
     2. toolchain checks    → osenv build seams
     3. git submodule update
     4. build (first)       → build.build_llama() + build.build_llama_swap()   [capability fn, kernel-called]
     5. create venvs        → osenv.new_bob_venv(venv-aider, venv-litellm[, venv-webui])   [KERNEL-only]
     6. gen configs (first) → generate.gen_all()   [capability fn — kills the last pre-venv pwsh gen call]
     7. fetch models        → fetch.fetch_models()  [capability fn, kernel-called]
     8. setup-clients / setup-fabric / install-cli / setup-voice / onboard  → capability fns
```

**Why the split is unavoidable:** step 5 *creates* `venv-litellm` — the venv `bob --run` itself lives in.
Anything at or before it (prereqs, first build, venv creation) cannot be a `--run` capability; it runs
under the *system* python3. Everything after it (fetch onward) is a capability the kernel merely *calls* —
same function the agent and `--run` reach. So `fetch`/`gen`/`setup-voice`/`onboard` are written **once**
and used by both the kernel and the loop; only `install_prereqs` + `new_bob_venv` + the *first* `build`
are kernel-exclusive.

**Chicken-and-egg (DD5):** Tier 0 installs Python, so it can't *be* Python on a truly bare box. Resolve by
shrinking `install_prereqs.sh`/`.bat` to a minimal shell stub — "ensure `python3` present via the OS
package manager, then `exec python3 -m bob.kernel prereqs`" — and porting the heavy per-distro/winget
logic into `install_prereqs.py` behind the 1b package seams. That keeps exactly **one thin shell layer**
(unavoidable: something must run before any interpreter) and **zero pwsh**.

**Kernel imports, never shells:** the kernel must `import` the capability modules (`fetch`, `generate`,
`build`, `stack`, `health`) — not subprocess them — so there is one function per capability. It runs from
`scripts/` on `sys.path` under system python3; the capabilities it calls pre-venv (`fetch`, `generate`,
`build`) must therefore be **import-clean under bare python** (stdlib-only or curl-subprocess — see DD2;
`generate.py` is already stdlib; `build` shells cmake/go).

---

## Part 4 — Recommended sequencing (each slice = independently shippable + committable)

Ordered by dependency and value. Capabilities first (agent-facing, low-risk, no kernel); the kernel is the
capstone once its called-capabilities exist.

- **Slice D0 — degraded-row wire-ups + 1b easy seams** ✅ **DONE** (on main; suite 708 green): wired
  health.py's two `○ pending` rows to the existing readers — the BobAgent scheduled-task row →
  `osenv.agent_task_status()` (registered → `✓` with state/next-run; not-registered → informational `○`,
  never `✗`), doctor's reproducibility row → `bob.versions.check_reproducibility()`/`load_lock()` (clean →
  `✓ release/N submodules/N models`; drift → `✗` per row with fix; missing lock → informational `○`). Added
  the easy 1b osenv seams: `gpu_vram_gb`/`gpu_arch`/`gpu_info` (**consolidated the two dups** — health.py +
  models.py now delegate, keeping their module-level names so existing test patches hold), `system_ram_gb`
  (ctypes `GlobalMemoryStatusEx` on Windows, `/proc/meminfo` on Linux), `numa_node_count`,
  `linux_package_manager`/`linux_os_family`, `backup/restore/remove_build_output`. Verified live via
  `bob doctor`. tests/test_osenv.py (+18: TestGpuSeams/TestRamAndNuma/TestLinuxDistroSeams/
  TestBuildOutputRollback), test_slice3_health TestHealthCheckWiredRows (rewrote the stale degradation
  class, +2 net). Unblocks diagnose-deep (D3) + build (D5).
- **Slice D1 — `fetch`** ✅ **DONE** (on main; suite 723 green): `scripts/tools/provision.py` (D6 — the
  ONE-D download/provision module) `fetch_models(profile, list_only)` — curl-subprocess resumable DL (DD2:
  `-C -` + `--fail-with-body`, curl-22 poison-prefix deletion, other-exit partial kept), SHA256-verify vs
  versions.lock (pinned mismatch → delete + raise; unpinned → TOFU warn) reusing `bob.versions.sha256_file`,
  atomic manifest writer, mmproj rides the model's revision, gguf dedup, advisory disk pre-check. Agent tool
  `fetch_models` (mutating) + `bob fetch [--list] [profile]` + `bob --run fetch_models`. Flipped
  registry→python, regen verbs.json, deleted the `fetch` bob.ps1 case (fetch-models.ps1 KEPT — bootstrap.ps1
  pre-venv still calls it; retires at D8 when the kernel calls provision.fetch_models). **Import-clean under
  bare python** (curl subprocess, no requests) so the kernel reuses the same fn. `ci.yml`'s `bob.ps1 fetch`
  stays non-regressing — the dispatch prologue routes it to `python -m bob fetch`. Verified live
  (`bob fetch --list` → 16gb profile, 7 models, dedup + mmproj + present-status). tests/test_slice_d1_fetch.py (15).
- **Slice D2 — `lock` (writer + gate)** ✅ **DONE** (on main; suite 735 green): added the writer + gate to
  `scripts/bob/versions.py` (the reader already existed) — `bob_version`/`submodule_commits`/
  `lock_model_manifest` (union of profiles' ggufs, profile-then-role sorted, first-wins, sha from manifest
  then locked-fallback = TOFU-then-lock)/`build_lock_object`/`lock_text`/`write_lock` (atomic)/`check_sync`.
  **Proven BYTE-IDENTICAL to the pwsh writer** via a live diff of `python lock_text` vs `pwsh
  Get-VersionsLockText` under the same manifest state (`json.dumps(indent=2)` matches pwsh ConvertTo-Json's
  2-space / `key: value` / `null` / float formatting), and both `--check` gates agree. Surfaces: `bob lock`
  (write, CLI) / `bob lock --check` (gate) via cli._handle_lock; agent **read-only** tool `lock_status`
  (sync + reproducibility report — the write path is deliberately NOT an agent tool); `python -m bob.versions
  --check|--write`. Rewired `check.ps1`'s gate from dot-sourcing `_versions.ps1` to `python -m bob.versions
  --check`. Flipped `lock`→python, regen verbs.json, deleted the bob.ps1 lock case. **`_versions.ps1` is
  KEPT** (correction to the pre-plan note): `_models.ps1` dot-sources it and `fetch-models.ps1` uses
  `Get-VersionsLock` — both pre-venv, retire at D8. `ci.yml`'s pwsh `bob lock`/`check.ps1` stay
  non-regressing. Verified live (`bob lock --check`, `bob --run lock_status`). tests/test_slice_d2_lock.py (12).
- **Slice D3 — `mlock` + `diagnose` deep** ✅ **DONE** (on main; suite 756 green): added the hard CUDA
  cluster to osenv (`resolve_cuda_root_candidates` pure descriptor, `cuda_toolkit_version` [version.json→
  version.txt→nvcc], `best_cuda_root` [newest ≥ MinVer floor — 12.9/13.x qualify for sm_120],
  `cuda_host_compiler` + `assert_cuda_host_compiler_ok` for D5) + `mlock_status` (read-only: Linux ulimit /
  Windows secedit-export) + `mlock_grant` (DD4: Windows secedit-INF rewrite + ctypes `ShellExecuteW runas`
  self-elevation / Linux ulimit guidance; **CLI-only, never an agent tool**). **Healed the Slice-3 diagnose
  split** — `health.diagnose` now renders the deep rows (RAM/Package/CUDA/mlock/NUMA) via the seams; dropped
  the "ports in ONE-D" note. Agent read-only tool `mlock_status`; `bob mlock [--grant]` (cli._handle_mlock);
  `mlock`/`diagnose` verbs → python. **Parity-verified live on real Blackwell/arch hardware** —
  `best_cuda_root(120)`/`system_ram_gb`/`numa`/`pkgmgr`/`cuda_toolkit_version` all byte-match pwsh
  (`/opt/cuda` = CUDA 13.3, correctly accepted for the sm_120 ≥12.8 floor). Deleted the bob.ps1 mlock case;
  **`diagnose.ps1` + `grant-mlock.ps1` KEPT** (pre-venv setup.ps1 calls them; retire at D8).
  tests/test_slice_d3_mlock_diag.py (21) + reworked test_slice3_health TestDiagnoseLightRows.
- **Slice D4 — `eval`** ✅ **DONE** (on main; suite 766 green): `models.py:eval_model(role, task, shots,
  limit)` ports `eval.ps1` — resolves `osenv.venv_exe('venv-eval','lm_eval')` (DD3: the isolated venv),
  reads the tokenizer from the active profile (`bob_models`), checks the endpoint, builds the
  timestamped `results/` output, and runs lm_eval inheriting stdio (very long → returns the process rc).
  **CLI-only** — `bob eval <role> [task] [--shots N] [--limit N]` (cli._handle_eval); deliberately NOT an
  agent tool and NOT on `--run` (long + separate venv). Flipped `eval`→python, regen verbs.json, **deleted
  `eval.ps1`** (fully replaced — nothing else called it). `bootstrap-eval.ps1` (the venv-eval creator) is
  KEPT until D8 (pre-venv). Not runnable live here (no venv-eval/endpoint); tests cover arg-building +
  every guard path. tests/test_slice_d4_eval.py (10); test_slice4 eval-stays-pwsh assertion flipped.
- **Slice D5 — `build` + `build-llama-swap` + `fabric-setup`** *(the native toolchain; DD1):*
  `build.py` — `build_llama(cpu, arch, force)` (cmake orchestration over the 1b build seams; cmake/nvcc/go
  stay subprocess as they always were), `build_llama_swap()`, `setup_fabric()`. `bin/` swap + verify.
  `build`/`fabric-setup` verbs → python (CLI). Non-gating (GPU release tier only per C7), so low merge risk.
- **Slice D6 — `update`** *(orchestration over D2+D5):* git sync + submodule + venv reinstall + conditional
  rebuild w/ `backup/restore_build_output` rollback + relock + doctor. `update` verb → python.
- **Slice D7 — post-venv onboarding capabilities:** `setup-voice` (whisper/piper provision, reuses D1 DL +
  `build-whisper` + stack.py), `setup-clients` (Continue/aider wiring, calls `generate.gen_continue`),
  `install-cli` (PATH shim/completions), `onboard` (interactive profile + `generate.gen_all`). Each a
  capability the kernel will call. Verbs → python. `bob-memory.ps1` retires here (onboard was its last caller).
- **Slice D8 — the cold-start kernel (capstone, Part 3):** `install_prereqs.py` (Tier 0) + `kernel.py`
  (Tier 1); shrink `setup.sh`/`.bat` + `install_prereqs.sh`/`.bat` to the minimal python3-ensuring stub
  (DD5); the kernel *imports* D1/D5/D7 + generate + stack. **Now delete** every remaining `*.ps1` under
  `scripts/` (`bob.ps1`, `_models.ps1`, `_platform.ps1`, `_versions.ps1`, `_common.ps1`, `bootstrap*.ps1`,
  `start-*.ps1`, `up.ps1`, `gen-*.ps1`, `setup*.ps1`, `install-*.ps1`, `build-*.ps1`, `grant-mlock.ps1`,
  `onboard.ps1`, `bob-memory.ps1`, `bob-toast.ps1`) — except the test/CI harness (`check.ps1`/`smoke*.ps1`/
  `test-*.ps1`), whose rewire/deletion is ONE-E. Rewire the full `ci.yml` provisioning path to the kernel.

After each slice: flip verbs to `runtime=python` in [registry.py](../../scripts/bob/registry.py), regen
`verbs.json` (`python -m bob.registry`), delete the dead `bob.ps1` case(s), keep the parity + verbs-sync +
lock gates green + the CPU/distro CI matrices green, hermetic tests, commit feat + docs.

---

## Part 5 — Decisions (RESOLVED 2026-07-05)

- **DD1 — `build` depth:** ✅ **Full Python port.** Reproduce the CUDA-root / cmake-flags / host-compiler
  cluster (~82 lines) in `osenv.py`; cmake/nvcc/go stay subprocess (they always were). The only path to
  zero `.ps1`; risk contained (native build is GPU/release-tier only per C7). It is the single biggest
  chunk of ONE-D → Slice D5, kept off the per-PR gate.
- **DD2 — `fetch` download:** ✅ **curl subprocess.** Keeps `fetch` **venv-free** so the pre-venv kernel
  reuses the same function; exact parity with today's `-C -` resume + `--fail-with-body` poison-prefix
  handling. `requests` only as a fallback if curl is absent.
- **DD3 — `eval` venv:** ✅ **Keep the isolated `venv-eval`.** lm-eval is a heavy, conflicting dependency
  set. `eval` = CLI-only capability shelling `venv-eval/lm_eval`; `bootstrap-eval.ps1` → a lazy
  `osenv.new_bob_venv('venv-eval', …)` ensure (kernel-family).
- **DD4 — `mlock` grant:** ✅ **Port grant to Python, CLI-only.** Windows secedit-INF rewrite + UAC
  self-elevation via ctypes `ShellExecuteW` verb `runas`; Linux ulimit/limits.conf guidance. `-Check`
  (read-only, both OS) is an agent tool + feeds diagnose/doctor; **grant is CLI/`--run`-only, never an
  agent tool** — model-triggered privilege escalation is disallowed.
- **DD5 — Tier-0 shell stub:** ✅ **Minimal shell stub → Python kernel.** Shrink `install_prereqs.sh`/`.bat`
  to "ensure `python3` via the OS package manager, then `exec python3 -m bob.kernel prereqs`"; port the
  heavy per-distro/winget logic into `install_prereqs.py` behind the 1b package seams. Exactly one thin,
  unavoidable shell layer; **zero pwsh**.
- **DD6 — Kernel scope:** ✅ **ONE-D owns Slice D8 as the capstone.** Land D0–D7 (all capabilities, each
  independently shippable + immediately useful to the agent) first; D8 = the two-tier kernel + delete all
  `scripts/*.ps1` (except the test/CI harness → ONE-E). Value ships continuously; D8 is pure retirement.

## Part 6 — Acceptance (per the module doc)
Each capability invoked identically via the agent and via `bob --run <cap>` (one function in the stack);
the kernel calls the *same* functions (no parallel provisioning path); `git grep -l '\.ps1' scripts/`
returns only the test/CI harness (deleted in ONE-E); the CPU acceptance + distro-prereq CI matrices stay
green through the rewire; a live fresh-machine `setup` (Tier 0 → kernel → build → venv → gen → fetch → up)
runs with no PowerShell; `bob lock --check` and doctor's reproducibility/agent-task rows are real.
