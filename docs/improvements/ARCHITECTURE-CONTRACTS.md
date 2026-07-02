# Architecture Contracts (roadmap NB → P)

**Purpose.** The portability + capability modules (NB, NC, ND, NE, O, P) all touch a handful of
cross-cutting seams — the command front door, config resolution, secrets, data location, CI, and the
registries. If each module defines these independently they drift (the review of the first drafts
found *three* modules describing *three* different front doors). This file defines each seam **once**;
every module references it instead of re-deciding. If a module needs to change a contract, it changes
it *here* and notes it — never silently in-module.

Read this before implementing any NB–P module.

---

## C1 — The `bob` dispatch contract (front door)

**Problem it resolves:** who runs `bob <cmd>` on each OS, given that (a) some commands are Python
runtime, (b) some are PowerShell orchestration, (c) **bootstrap commands run before the Python venv
exists**, and (d) the split is **per fully-qualified command, not per top-level verb** — e.g.
`bob agent serve` (Python) vs `bob agent install` (pwsh scheduled task) live under the same `agent`.

**Contract:**
1. `bob` is a **dumb shim** (`.cmd` on Windows, a POSIX `bob` script elsewhere) — no logic beyond
   routing, and no hand-maintained command list.
2. **Routing is per fully-qualified command via `config/verbs.json`** (checked-in plain JSON, read by
   the shim *without* Python so bootstrap works, and generated from / kept in sync with the C6 command
   registry). Each entry declares `runtime: pwsh | python`. The shim looks up the command path
   (`<verb>` or `<verb> <sub>`) and routes accordingly; unknown → `python -m bob` (the default) which
   prints help.
3. **`pwsh` (orchestration/bootstrap)** — these stay PowerShell (they're OS-native or pre-venv):
   `setup, install, up, down, restart, start, serve, stop, services, build, gen, fetch, doctor,
   update, status, profile, mlock, models`, plus the orchestration **sub**commands of `agent`
   (`install, status, schedule, log`). **Note:** `bob serve` = the **inference stack** (start.ps1,
   llama-swap + LiteLLM) — a stable user-facing verb; it is **not** the agent server (see naming).
4. **`python -m bob` (runtime)** — the agent brain + tools: `agent "<goal>"`, `agent serve`
   (the FastAPI **agent** HTTP server), `agent mcp`, `skill`, and **no-arg on an interactive TTY**
   (the NE interactive shell; a non-interactive/piped/CI no-arg prints help, so scripts are
   unaffected — NE Decision C). Plus any runtime verb that has a Python entry (`clip`, and the plugin
   verbs `summarise, draft, search, play`). `python -m bob` is a **real importable package**
   (`scripts/bob/`) exposing `run_agent_events` et al. as an API, so NE's REPL consumes events
   in-process. The event API is **bidirectional** (NE0): it carries an `approval_required`
   request/`approve`-callback response and `call_id`-correlated tool events, so the shell renders
   approvals without a blocking `input()`.
5. **Phased migration (do NOT rewrite working code in NB4).** Several *conceptually-runtime* verbs are
   **implemented in PowerShell today** (`chat` via Invoke-BobStream, `voice` loop, `describe` vision,
   and the `recall` wrapper). NB4 ships the **mechanism** (shim + `verbs.json` + `scripts/bob/`
   package + registry) and routes only the verbs that **already have a Python entry** to
   `python -m bob`; the pwsh-implemented runtime verbs keep `runtime: pwsh` in `verbs.json` and are
   migrated to Python by a **later** module (naturally, when NE builds the interactive shell). This is
   non-regressing and honors "switch thinned, not rewritten." The phased state is tracked in this
   contract, not silently in a module.
6. **Naming (no collision).** `bob serve` = inference stack (pwsh). The agent HTTP server is
   `bob agent serve` everywhere; on Linux/CI it is `python -m bob agent serve`. There is **no** bare
   `python -m bob serve` (it would mean something different from `bob serve`).
7. Orchestration *logic* stays in the existing PowerShell scripts; `bob.ps1`'s `switch` is **thinned
   to a dispatcher** over `verbs.json`, not rewritten (per-verb logic/scripts stay). pwsh commands may
   be invoked directly by the shim or via `python -m bob` exec'ing `pwsh`; `verbs.json` is authoritative.

**Owner:** **NB4** builds the shim, `config/verbs.json`, the `scripts/bob/` package, and the command
registry (C6). NC makes the `pwsh` side cross-platform. NE adds the interactive shell + catalog and
(later) migrates the pwsh-implemented runtime verbs to Python — it does **not** redefine the front door.

**Bootstrap note:** `bob setup` is `runtime: pwsh` and works with no venv. The Python entry is only
reached for runtime commands, which by definition run after setup.

**Status (NB4, ✅ 2026-07-02):** `config/verbs.json` (generated from `scripts/bob/registry.py`),
`scripts/bob/` package (`python -m bob`), POSIX `bob` shim, and the `bob.ps1` dispatcher prologue
all landed. `bob serve` = inference stack (pwsh); agent server = `bob agent serve` /
`python -m bob agent serve`. `check.ps1` enforces verbs.json↔registry sync.

**Status (NC, ✅ 2026-07-02):** the pwsh side is now cross-platform. `scripts/_platform.ps1`
(`Get-BobOS` + resolvers/executors) makes the orchestration OS-aware; Linux services start via
`nohup`+pidfile and the agent task registers as a crontab line (`Register-AgentTask`). New pwsh verb
`bob build` (registry + verbs.json). Front door contract unchanged.

**Status (NE0 + NE1, ✅ 2026-07-02):** NE0 made the runtime event API bidirectional (approval event +
`approve` callback, fail-closed; `call_id` on tool events; `RunContext` into `dispatch_call`; linked
`CancelToken`; `ToolRegistry.filtered`). NE1 made `scripts/bob/registry.py` the **true catalog**: every
top-level `bob.ps1` switch verb is registered and grouped (Talk/Act/Make/Know/Run/Config), with an
opt-in `hidden` flag for dispatchable-but-uncatalogued verbs (Decision B's sanctioned exception). A
`test_command_registry` parity test fails if a switch verb is missing from the registry; `verbs.json`
regenerated. The no-arg→shell routing (TTY-gated) is reserved here; the shell itself lands in NE2.

---

## C2 — The config contract (neutral `config.json` is the boundary)

**Problem it resolves:** the runtime must run without PowerShell, but re-implementing the full
PowerShell `Get-BobConfig` merge in Python re-creates logic drift.

**Contract:**
1. `data/config.json` (neutral JSON) is the **only** thing the Python runtime reads. It already is
   (`bob_core.load_config`). Nothing in the runtime parses `.psd1`.
2. **Split config by consumer:**
   - **Runtime keys** (the ~15 the Python core reads): `port`, `litellmPort`, `agentPort`,
     `searxngPort`, `litellmKey`, `routing.*`, `persona.systemPrompt`, `agent.*`, `memory.*`,
     `vision.*`. These must resolve on any OS.
   - **Provisioner keys** (profiles, peers, model file paths, build flags, `toastAppId`): consumed
     only by the PowerShell orchestration; never required by the runtime.
3. **Who writes `config.json`:**
   - Windows: `Get-BobConfig` writes the full file. It **seeds the runtime layer from
     `config/defaults.json.runtime`** (the neutral default source), then deep-overlays `config/bob.psd1`
     (the Windows-only overlay, which carries just its unique keys: `persona.name/style`, `routing`,
     `voice`, `agent.toastAppId`), then `user.psd1`, then the port/`litellmKey` injects. So both OSes now
     take runtime defaults from the *same* neutral file; psd1 is a thin overlay, no longer a second copy.
   - Non-Windows: a **small Python resolver** (`bob_config.py`) merges `config/defaults.json` (C6) +
     a neutral user override (`config/user.json`/`user.toml`) into the **runtime-subset** of the same
     `config.json` shape. It does **not** reproduce the provisioner keys and is **not** required to be
     byte-identical to `Get-BobConfig`.
4. **Shared constants** (ports, routing table) live in one neutral file `config/defaults.json`, read
   by both PowerShell (`ConvertFrom-Json`) and Python (`json.load`). No mirrored dicts. **`defaults.json.ports`
   is the sole port source** — `models.psd1`/`bob.psd1` carry no port literals; every reader resolves via
   `Get-BobPortDefault`, and a parity gate (`test_defaults_parity`) fails the build if a shadow port copy
   is reintroduced anywhere (including `bob.psd1.voice`).

**Owner:** **NB1** (`config/defaults.json` + both sides read it), **NB2** (the runtime-subset Python
resolver). NB7 (optional) unifies *authoring* to a neutral format so both sides merge the same source
files; until then, Windows authors `.psd1`, non-Windows authors `user.json`, both emit the same
runtime `config.json`.

**Acceptance rule:** parity is "the runtime receives every runtime key it needs, correctly," proven
by a resolver test — **not** "byte-identical to the PowerShell merge."

**Status (NB1+NB2, ✅ 2026-07-02):** `config/defaults.json` is the neutral single source (`ports` +
`roleTable` shared by both languages; `runtime` for the resolver). `scripts/bob_config.py`
`resolve_runtime_config()` produces the runtime subset; `bob_core.load_config()` falls back to it
when `data/config.json` is absent.

**Status (NB7 + ports single-source, ✅ 2026-07-02):** Windows `Get-BobConfig` no longer duplicates the
runtime defaults — it seeds `$base` from `(Get-BobDefaults).runtime` and deep-overlays `bob.psd1` (which
now keeps only `persona.name/style`, `routing`, `voice`, `agent.toastAppId`), so `defaults.json.runtime`
is the single default source on both OSes (NB7 done via **Option A**, superseding the deferred TOML plan).
Ports are likewise single-sourced to `defaults.json.ports`: no port literals remain in `models.psd1`/
`bob.psd1`, and `test_defaults_parity` fails on any reintroduced shadow copy. A resolver-parity test
asserts the Windows `config.json` runtime keys equal the Python `resolve_runtime_config()` keys.

---

## C3 — The secrets contract

**Problem it resolves:** API keys (DeepSeek/HF/Langfuse), `litellmKey`, and per-client `apiTokens`
have no cross-platform home, and the N9 `file_read` denylist is Windows-path-shaped.

**Contract:**
1. A single resolver seam — `osenv.secret(name)` (Python) / `Get-Secret` (PowerShell) — resolves in
   this precedence: **process env → OS keychain (Keychain/Credential Manager/`secret-tool`) →
   `<config-dir>/secrets.json` (chmod 600 / ACL'd) → default**. No secret is ever read from a
   git-tracked file.
2. `litellmKey` and `apiTokens` are **secrets**, not plain config — they live via the secret seam
   (env or `secrets.json`), never inlined in a checked-in `user.json`/`config.json`. `config.json` may
   carry a *reference*, not the value.
3. The **N9 `file_read`/`file_write` denylist is OS-aware and data-dir-relative**: it denies the
   resolved secrets file, the data dir DBs, config/`.psd1`, `logs/`, `.env*`, **plus** platform secret
   dirs (`~/.ssh`, `~/.aws`, `~/.config/bob`, `~/.gnupg`, and the systemd `EnvironmentFile` path on
   Linux; the DPAPI/credential store paths on Windows).

**Owner:** **NB3** (the `secret()` seam + OS-aware denylist upgrade to `file.py`). **O8** (token store)
builds on this seam; **NC4/NC5** wire provider secrets into services via it (e.g. systemd
`EnvironmentFile` fed from the secret seam).

**Status (NB3, ✅ 2026-07-02):** `osenv.secret(name)` resolves env → keychain (`keyring`, optional)
→ `data/secrets.json` → default; `bob_core._litellm_key` uses it. `file.py` denylist is now OS-aware
(adds `secrets.json`, `~/.ssh`/`.aws`/`.gnupg`/`.config/bob`, resolved secrets file). PowerShell
mirror (`Get-Secret`) is NC1's job.

**Status (NC1, ✅ 2026-07-02):** `Get-Secret` (`scripts/_platform.ps1`) mirrors the precedence exactly —
env (exact name, then `BOB_<UPPER>`) → OS keychain (`secret-tool` on Linux; no-op on Windows) →
`<data_dir>/secrets.json` → default.

---

## C4 — Data & state location policy

**Problem it resolves:** `sessions.db`, `bob.db`, `schedules.json`, `logs/` are repo-relative Windows
paths today; NB3/NC gesture at XDG dirs but nothing decides, and nothing migrates.

**Contract:**
1. **Default: repo-relative `data/` and `logs/`** on all OSes (the local-first, single-checkout case —
   simplest, zero migration). Paths are stored POSIX-relative and resolved with `pathlib`.
2. `osenv.data_dir()` / `Get-DataDir` return the repo-relative dirs by default, and an XDG/`%LOCALAPPDATA%`
   location **only** when `BOB_DATA_DIR` (or a config key) is set — reserved for a future
   system-install / multi-user mode.
3. When the non-default dir is used, a **one-time migration** copies existing `data/*` on first run.
   Not needed for the default path.

**Owner:** **NB3** defines `data_dir()`/`Get-DataDir` and the policy; NC/ND inherit it (no migration on
the default path).

**Status (NB3, ✅ 2026-07-02):** `osenv.data_dir()`/`cache_dir()` return repo-relative `data/`/`logs/`
by default; `BOB_DATA_DIR` relocates them with a one-time copy migration (stamped, never re-copies).
PowerShell `Get-DataDir` is NC1's job.

**Status (NC1, ✅ 2026-07-02):** `Get-DataDir`/`Get-CacheDir` (`scripts/_platform.ps1`) mirror the policy —
repo-relative `data/`/`logs/` by default, `BOB_DATA_DIR` override with a one-time `.migrated`-stamped
copy (`Invoke-DataDirMigration`). `bob doctor`'s writable-dir checks resolve through them.

---

## C5 — CI ownership (one workflow, additive)

**Problem it resolves:** NB6, ND2, and O10 each "create" `.github/workflows/ci.yml` with conflicting
shapes.

**Contract:** there is **one** `.github/workflows/ci.yml`, extended (never recreated) in order:
1. **NB6** creates it: the Python **core suite** on `ubuntu-latest` **and** `windows-latest`, plus
   `check.ps1` (py_compile + PS AST parse + unittest). Runs on every PR. No GPU.
2. **ND2** extends it: a **fresh-install acceptance matrix** — a **CPU tier** (no GPU, uses the NC CPU
   build, C-per H3) on both OSes every PR, and a **GPU tier** (self-hosted) on release tags.
3. **O10** extends it: the **agent-capability eval** job, running on the existing matrix (not a new
   Windows-only runner).

**Owner:** NB6 creates; ND2 and O10 add jobs to the same file.

**Status (NB6, ✅ 2026-07-02):** `.github/workflows/ci.yml` created — `core-suite` job on
`ubuntu-latest` + `windows-latest` runs `check.ps1` (py_compile + PS AST parse + verbs sync +
unittest) via a `BOB_PYTHON` override. ND2/O10 extend this file (do not recreate).

---

## C6 — Registries: commands, tools, skills (data, not hardcoded)

**Problem it resolves:** the interactive UI and help must render "everything Bob can do" without a
hand-maintained menu, and O's new capabilities must appear without a UI rewrite.

**Contract:**
1. **Tools** — already auto-discovered by `ToolRegistry`. Unchanged.
2. **Commands** — a **command registry** (`{name, group, summary, args, handler, runtime, hidden?}`
   where `runtime ∈ {python, pwsh}` and optional `hidden` keeps a command dispatchable but out of the
   catalog) is the single source for dispatch (C1), help, and the catalog. Built in **NB4**;
   reconciled to cover **every** verb + grouped by **NE1**. A parity test forbids a switch verb that
   isn't registered, so the catalog can't drift from what dispatch accepts.
3. **Skills** — a **skills registry** parallel to the tool registry (auto-discovered, contract-
   validated). **NE** builds the registry + catalog rendering (skills appear in the splash). **O**
   builds skill *execution* (a skill may spawn a sub-agent — an O1 consumer). NE never executes a
   sub-agent-backed skill; it lists it and hands execution to the runtime.
4. The interactive shell (NE) and every UI surface render **from these registries** and from the
   `run_agent_events` event stream — so O's new event types (sub-agent spawned, permission required,
   parallel-tool progress) surface by being emitted, not by editing the shell.

**Owner:** command registry → **NB4**; tool registry → existing; skills registry/catalog → **NE**;
skill execution → **O**.

**Status (NB4, ✅ 2026-07-02):** command registry landed in `scripts/bob/registry.py`
(`{name, group, summary, args, runtime, handler}`; `commands()` enumerable); `config/verbs.json` is
generated from it and kept in sync by the `check.ps1` gate.

**Status (NE1, ✅ 2026-07-02):** the command registry is now the **true catalog** — every top-level
`bob.ps1` switch verb is registered and grouped into the six human buckets (Talk/Act/Make/Know/Run/
Config), with an opt-in `hidden` flag; `commands(include_hidden=False)` feeds help. Parity enforced by
`test_command_registry.TestSwitchParity`. **Skills registry/catalog (NE4) and the catalog renderer
(NE3) are still pending;** skill execution stays with **O**.

---

## C7 — Provisioner backend strategy (native default now; portable when Linux/mac get real users)

> **Decision (2026-07-02): keep native-from-source as today's default; do NOT build a `provisionMode`
> multi-backend abstraction now. Portable backends (prebuilt binary → `docker compose` inference →
> BYO OpenAI-compatible endpoint) become the Linux/macOS default *when those platforms get real
> daily-driver users* — added behind the existing NB5/NC1 seam, with zero caller changes. Native-from-
> source stays the opt-in max-control tier. macOS, when it comes, is scoped portable-first.**

**Problem it resolves:** the review floated flipping NC's Linux default from native-from-source to a
portable provisioner. The question is *timing*, not architecture — should the extra backends be built
before ND, or deferred?

**Why defer (YAGNI on a zero-user platform).** The expensive, hard-to-retrofit part — the provisioner
*seam* (NB5's "runtime only needs a resolvable config + a reachable OpenAI-compatible endpoint" +
NC1's `_platform.ps1` + `capability_probe`) — **already exists**. The additional backends are cheap to
add later and speculative now: Linux/macOS have **zero daily-driver users today** (CI proves the core
is OS-agnostic; nobody runs Bob natively on Linux in anger, and macOS doesn't exist yet). A 4-backend
selector built for zero users is premature abstraction — designed against imagined usage, reworked when
the first real user surfaces with real requirements. Deferring lets the backends be designed against
real needs, behind a seam that already supports them.

**What this contract fixes for the modules below it:**
- **NC** — native-from-source is the shipped default (as built) *and* the opt-in max-control tier. No
  churn to committed NC1–NC8 work. When portable backends are added, they slot behind the NC1 seam as
  `Resolve-*` alternatives; callers are unchanged.
- **ND** — the per-PR **gating** acceptance path is the portable/CPU tier (NC8 CPU build), **not**
  native-from-source CUDA. Native-from-source builds are exercised only in the non-gating GPU/release-
  tag tier, so a fragile native build can never red the per-PR gate (see ND2).
- **macOS (future module)** — scoped **portable-first** (prebuilt/BYO), never native-from-source-first,
  recorded here so that module starts from the right default.

**Owner:** policy recorded here; NC honors it (native default, seam ready); ND2 honors it (portable/CPU
gates, native non-gating); the future macOS module inherits portable-first. Adding the portable backends
later is an additive change behind the NB5/NC1 seam — it **fulfills** this contract, it does not reopen it.

---

## Dependency graph (authoritative)

```
N ✓
└─ NB ✓ portable core        — C1 dispatch, C2 config, C3 secrets, C4 data, C6 command registry, NB6 creates CI (C5)  [NB1–NB6 done; NB7 deferred]
    └─ NC ✓ provisioner        — cross-platform pwsh (_platform.ps1); CUDA + CPU build tiers; degrades w/o GPU; native default, portable-later behind seam (C7)  [NC1–NC8 done]
        └─ ND  release          — reproducible; extends CI with the fresh-install matrix (C5)
            └─ NE  interface     — one `bob`; extends the command registry (C6); skills catalog only
                └─ O  capability — depends NB+NC+ND; dual-OS sandbox; extends CI eval (C5); skill execution
                    └─ P  frontier product — durable/resumable runs, deep multimodal, computer-use
```

**Rule:** a module may only *extend* a lower module's contract, never redefine it. Contract changes
land here first.
