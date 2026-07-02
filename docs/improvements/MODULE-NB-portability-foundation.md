# Module NB — Portability Foundation (OS-agnostic core)

**Status:** ✅ **NB1–NB6 implemented** (2026-07-02). NB7 **Option A implemented** (2026-07-02) —
`defaults.json.runtime` unified as the single default layer + ports single-sourced (drift killed);
NB7 Option B (neutral TOML authoring + exporter) remains deferred — *not needed* (see NB7 below).
**Depends on:** Module N. **Precedes:** NC → ND → NE → O → P.
**Read first:** [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) — NB *owns* four of the
cross-cutting contracts (C1 dispatch, C2 config, C3 secrets, C4 data, C6 command registry) and
creates the CI workflow (C5). This module is the foundation the whole NB→P chain builds on, so its
seams are defined there, once, and referenced here.

**Why this module exists.** Bob is half PowerShell (OS glue: CLI, config generation, service
lifecycle, GPU/CUDA, setup/auto-install) and half Python (the agent runtime: loop, tools, HTTP
server, sessions, memory). That split has two costs:

- **Drag** — shared constants are *mirrored by hand*: port defaults live in both
  `_models.ps1 $script:BobPortDefaults` and `bob_core.py _PORT_DEFAULTS`; role resolution in both
  `Get-RoleForTask` and `bob_core.get_role`. Every shared value must be edited in two places or it
  drifts. M6/M8/N7 were largely paying down this exact tax.
- **Wall** — the Python runtime can only *boot* after PowerShell has run: `bob_core.load_config`
  reads `data/config.json`, which is produced **only** by `Get-BobConfig` (PowerShell) from `.psd1`
  files (a PowerShell-only format). So the portable part of Bob (the agent runtime) is pinned to
  Windows by the non-portable part (orchestration). Plus scattered Windows assumptions in Python
  itself (`shell.py` hardcodes `pwsh`; `toastAppId`; `data\bob.db` backslash paths).

**The goal (and the constraint).** Make Bob **OS-agnostic going forward** — but *start somewhere*.
NB does **not** rewrite the Windows orchestration or ship a Linux end-to-end build. It does three
things: (1) kill the drift with one neutral source of truth, (2) let the Python runtime boot and
run on any OS *without* PowerShell, and (3) prove it with a Linux CI job. Crucially, it **keeps all
the config-based auto-install / prereq / setup machinery** — that stays as the Windows *provisioner*,
but behind a contract so it's neither a drag (no more mirroring) nor a wall (the runtime no longer
requires it). Other-OS provisioners can be added later; the runtime won't care.

**Scope note.** After NB, the *core runtime* (loop, tools, server, sessions, memory, MCP) runs on
Linux/macOS from a neutral config, and the Windows auto-install experience is unchanged. Full
cross-platform inference (a Linux/Metal provisioner, service management off-Windows) is a *later*
module NB builds toward — NB is the foundation, not the finish.

## Overview

| Sub | Name | What it removes | Impact | Status |
|-----|------|-----------------|--------|--------|
| NB1 | Neutral single source of truth (ports, roles) — C2 | mirror drift (drag) | HIGH | ✅ done |
| NB2 | Python runtime-config resolver — C2 | the PS→config.json wall | HIGH | ✅ done |
| NB3 | OS seam: shell / paths / notify / **secrets (C3) / data-dir (C4)** | Windows assumptions + no secrets home | HIGH | ✅ done |
| NB4 | **Dispatch front door + command registry + runtime package — C1/C6** | three-front-doors / PS-only launch | HIGH | ✅ done |
| NB5 | Provisioner contract (keep Windows setup, unbind it) | setup as a hard dependency | MED | ✅ done |
| NB6 | Cross-platform CI — **creates the workflow (C5)** | untested portability claim | MED | ✅ done |
| NB7 | Config authoring format (psd1 → neutral) — phased | the last PowerShell-only coupling | MED | ✅ Option A done (default layer unified + ports single-sourced); Option B (TOML authoring) ⏸ deferred |

### As built (concrete artifacts)

- **NB1** — `config/defaults.json` (`ports` + `roleTable` + `runtime`); `bob_core.load_defaults()`
  feeds `_PORT_DEFAULTS`/`get_role`; `_models.ps1 Get-BobDefaults` feeds `$BobPortDefaults`/`Get-RoleForTask`.
  Grep confirms **no port/role literals remain** in either loader; Py↔PS parity test proves agreement.
- **NB2** — `scripts/bob_config.py` `resolve_runtime_config()`; `bob_core.load_config()` falls back to
  it when `data/config.json` is absent (routing derived from `roleTable`, no dup).
- **NB3** — `scripts/osenv.py` (`default_shell`, `data_dir`/`cache_dir` + `BOB_DATA_DIR` migration,
  `secret` env→keychain→`data/secrets.json`→default, `notify`); wired into `shell.py`,
  `bob_core._litellm_key`, and the OS-aware `file.py` denylist.
- **NB4** — `config/verbs.json` (generated from the registry), `scripts/bob/` package
  (`python -m bob`; `__init__` re-exports `run_agent_events`, `cli`, `registry`), POSIX `bob` shim,
  and the `bob.ps1` verbs.json dispatcher prologue. `config/verbs.json` sync is enforced by the
  `check.ps1` gate (`python -m bob.registry --check`), so it can't drift on commit.
- **NB5** — `docs/PORTABILITY.md` + `bob_core.capability_probe()` (wired into `agent serve`). Windows
  provisioner scripts untouched.
- **NB6** — `.github/workflows/ci.yml` (core suite + `check.ps1` on ubuntu + windows);
  `check.ps1` `BOB_PYTHON` override so CI (no venv) reuses the one gate.

Verified on Windows: 136 unit tests green, `check.ps1` exit 0, `test-dry-run.ps1` 375/375.

**Total (spent):** NB1–NB6 landed. NB7 Option A landed (default-layer unification + ports single-source,
drift killed). NB7 Option B (neutral TOML authoring + exporter, ~6–10 h) remains optional if a real
non-Windows *authoring* need appears.

---

## NB1 — Neutral single source of truth (ports, roles)

### Problem
Shared constants are hand-mirrored across languages and will silently drift:
- ports: `_models.ps1 $script:BobPortDefaults` (~L28-38) **and** `bob_core.py _PORT_DEFAULTS` (~L19-29);
- routing: `_models.ps1 Get-RoleForTask` (~L158-185) **and** `bob_core.py get_role` (~L39-61).
The chicken-and-egg that forces the mirror: PowerShell needs these values during bootstrap, before
the Python venv exists — so today each language keeps its own copy.

### Change
- Add one **language-neutral** file, `config/defaults.json` (checked in), holding the shared
  constants: the port table and the role-routing table. Plain JSON — both PowerShell
  (`ConvertFrom-Json`) and Python (`json.load`) read it natively; it exists before the venv.
- `_PORT_DEFAULTS` / `$BobPortDefaults` become **loaders** of `config/defaults.json`, not literal
  tables. `get_role` / `Get-RoleForTask` read the routing table from the same file. The literals
  live in exactly one place.
- Keep the existing `_port()` / `get_role()` / `Get-BobPortDefault` signatures — only their *source*
  changes, so no caller is touched.

### Effort: 3–4 h.
### Acceptance
`grep` proves the port/role literals exist only in `config/defaults.json`; a test asserts the Python
loaders and a PowerShell parse produce identical values from that file; deleting a key raises a
clear error on both sides. Suite green.

---

## NB2 — Python runtime-config resolver (boot without PowerShell)

Implements **contract C2**. Read it first — the scope correction below is the point of this item.

### Problem
`bob_core.load_config` reads `data/config.json`, which only `Get-BobConfig` (PowerShell) can produce,
by merging `.psd1` files. On any non-Windows box the Python runtime cannot self-configure — the wall.
**But** the naive fix (re-implement the full `Get-BobConfig` merge in Python) would re-create, at the
*logic* level, the exact drift NB1 kills at the *data* level: two merge implementations to keep in
lockstep forever.

### Change (scoped to the runtime subset — C2)
- A `bob_config.py` resolver that produces **only the runtime keys** the Python core actually reads
  (per C2: `port`/`litellmPort`/`agentPort`/`searxngPort`, `litellmKey` *reference* (C3),
  `routing.*`, `persona.systemPrompt`, `agent.*`, `memory.*`, `vision.*`). It does **not** reproduce
  provisioner keys (profiles, peers, model file paths, build flags) — the runtime never reads those.
- It merges `config/defaults.json` (NB1) + a neutral user override (`config/user.json`/`user.toml`)
  into the **runtime-subset** of the same `config.json` shape. It is explicitly **not** required to be
  byte-identical to `Get-BobConfig`.
- `load_config` gains a fallback: use `data/config.json` if present + fresh (Windows path, unchanged);
  else resolve in Python from the neutral sources. The runtime no longer *requires* `bob gen`.
- On Windows nothing changes — `Get-BobConfig` remains authoritative and writes the full `config.json`
  (runtime subset ⊆ it); the Python resolver is the path used only when PowerShell isn't in the loop.

### Effort: 4–6 h (smaller than the original estimate — the subset is a fraction of the full merge).
### Acceptance
Parity is **"the runtime receives every runtime key it needs, correctly"** (C2 rule), proven by a
resolver test over fixture neutral sources — *not* byte-identity with the PowerShell merge. Runtime-
read keys (incl. `agent.apiTokens`/ownership, N1) resolve; provisioner keys are absent and unneeded.
On a box with no `config.json`, `bob_core.load_config()` returns a valid runtime config. Green on
Windows and (NB6) Linux.

---

## NB3 — OS-abstraction seam (shell / paths / notify / secrets / data-dir)

Implements **contracts C3 (secrets) and C4 (data-dir)** plus the OS seam itself.

### Problem
Windows assumptions are baked into the *Python* runtime, not just the PowerShell layer:
- `scripts/tools/shell.py` hardcodes `["pwsh", "-NonInteractive", "-Command", ...]`;
- `bob_core._get_db_path` / config paths carry `data\bob.db` backslashes and `.replace("\\","/")`;
- notifications assume WinRT (`toastAppId`, `bob-toast.ps1`);
- **secrets** (DeepSeek/HF/Langfuse keys, `litellmKey`, `apiTokens`) are ad-hoc env vars / inlined in
  psd1, with no cross-platform home; and the **N9 `file_read` denylist is Windows-path-shaped**
  (`data\config.json`, `*.psd1`) — it misses Linux secret paths (`~/.ssh`, `~/.aws`, `~/.config/bob`);
- **data/state** location (`sessions.db`, `bob.db`, `logs/`) is undecided cross-OS.

### Change
- `scripts/osenv.py` abstraction:
  - `default_shell()` — **the agent tool shell**, always OS-native: `bash`/`sh` on Linux/macOS,
    `pwsh` on Windows. Per C1, this is independent of whether `pwsh` is present for orchestration —
    tools must **not** assume `pwsh` cross-platform.
  - `notify()` — WinRT toast on Windows, `notify-send`/no-op elsewhere (retires `toastAppId` from
    runtime config; it becomes provisioner-only, C2).
  - `data_dir()` / `cache_dir()` — **C4 policy: repo-relative `data/`/`logs/` by default**; XDG /
    `%LOCALAPPDATA%` only when `BOB_DATA_DIR` is set (future system-install mode, with a one-time
    migration). Paths stored POSIX-relative, resolved via `pathlib`.
  - `secret(name)` — **C3 seam**: resolves env → OS keychain → `<config-dir>/secrets.json` (600/ACL)
    → default. `litellmKey`/`apiTokens`/provider keys resolve through this, never from a git-tracked
    file; `config.json` may hold a *reference*, not the value.
- `shell.py` and any exec surface call `osenv.default_shell()`; the fail-closed confirmation + N9
  posture are unchanged in spirit but the **denylist becomes OS-aware and data-dir-relative** (C3):
  deny the resolved secrets file + `data_dir()` DBs + config/`.psd1` + `logs/` + `.env*` + platform
  secret dirs (`~/.ssh`, `~/.aws`, `~/.config/bob`, `~/.gnupg`, systemd `EnvironmentFile`).
- The PowerShell side gets the mirror primitives (`Get-Secret`, `Get-DataDir`) in NC1's `_platform.ps1`.

### Effort: 6–8 h (grew: absorbs the C3 secrets seam + C4 data policy + the denylist upgrade).
### Acceptance
Per-platform tests (monkeypatched `platform.system()`): `shell_run` builds `bash -c` on non-Windows,
`pwsh` on Windows, still fail-closed with no stdin; `data_dir()` returns repo-relative by default and
the `BOB_DATA_DIR` override migrates once; `secret()` honors the env→keychain→file precedence and
never reads a tracked file; the OS-aware denylist refuses `~/.ssh/id_rsa` and the resolved secrets
file on Linux and `data\config.json` on Windows (extends the N9 tests). `notify()` no-ops off-Windows.

---

## NB4 — Dispatch front door + command registry + runtime package

Implements **contracts C1 (dispatch) and C6 (command registry)**. This is the seam NC/ND/NE/O all
build on, so it is owned here (not in NE) — NC calls `bob doctor`, ND calls `bob update`/`version`,
NE renders the command catalog, all through what NB4 defines.

### Problem
Today `bob` is a `.cmd` shim → `pwsh bob.ps1` → a monolithic `switch` that shells to Python per verb
(`& $venvPy bob_loop.py`). There is **no `python -m bob`** and no importable runtime package. Three
draft modules described three different front doors. The hard constraint the naive "make Python the
entry" answer misses: **`bob setup`/`install_prereqs` run before the venv exists**, so the entry
cannot require Python for bootstrap.

### Change (per C1 — read C1 for the full rules; the key points:)
- **`bob` becomes a dumb shim** (`.cmd` / POSIX `bob`) with no command list. It routes **per
  fully-qualified command** via **`config/verbs.json`** (checked-in JSON, read without Python so
  bootstrap works). Each entry declares `runtime: pwsh | python`. Routing is per command *path*, not
  per top-level verb — `agent serve` (python) and `agent install` (pwsh) split under the same `agent`.
- **pwsh set** (orchestration/pre-venv): `setup, install, up, down, restart, start, serve, stop,
  services, build, gen, fetch, doctor, update, status, profile, mlock, models` + `agent`'s
  orchestration subcommands. **`bob serve` = the inference stack (start.ps1) — a stable verb, NOT the
  agent server** (that's `bob agent serve`; no bare `python -m bob serve`).
- **`python -m bob` is a real package** (`scripts/bob/`) exposing the runtime as an importable API
  (`run_agent_events`, the server app, MCP) — so NE's REPL consumes events in-process. Handles
  `agent "<goal>"`, `agent serve`, `agent mcp`, `skill`, no-arg (NE shell later), and the runtime
  verbs that already have a Python entry (`clip`, plugins `summarise/draft/search/play`).
- **Phased (do not rewrite working pwsh):** `chat`, `voice`, `describe`, and the `recall` wrapper are
  PowerShell today — NB4 leaves them `runtime: pwsh` in `verbs.json` (they still work via the shim →
  pwsh) and a later module migrates them to Python. NB4 = mechanism + route the already-Python verbs.
- **The command registry (C6)** lives here: `{name, group, summary, args, handler, runtime}` — the
  single source for dispatch, help, and NE's catalog; `verbs.json` is generated from / kept in sync
  with it. `bob.ps1`'s `switch` is **thinned to a dispatcher** over it (per-verb logic/scripts stay).

### Effort: 6–8 h (grew: absorbs the shim + `verbs.json` + the `scripts/bob/` package + the registry
that NE1 previously duplicated).
### Acceptance
`bob <verb>` resolves identically on Windows and Linux via the one shim + `verbs.json`; `bob setup`
runs with no venv (routes to `pwsh`); `python -m bob agent serve` starts the agent server on Linux
(NB6 CI) and answers `/health` + an owner-scoped session turn against a stub model; `bob serve` still
launches the inference stack (pwsh, no regression); the command registry is enumerable (a test lists
every command + its `runtime`); no verb regresses on Windows.

---

## NB5 — Provisioner contract (keep the Windows setup — unbind it)

### Problem
The auto-install prereqs + model download + client wiring + service start (`install_prereqs.bat`,
`setup.bat`, `bootstrap*.ps1`, `fetch-models.ps1`, `up.ps1`, `start-*.ps1`) is genuinely valuable —
and genuinely Windows-specific. Today it's implicitly a *dependency* of running Bob at all. It must
stay an asset without being a wall.

### Change
- Define a thin **provisioner contract**: a provisioner's only obligations to the runtime are
  (a) a resolvable config (NB2) and (b) the declared service endpoints reachable (llama-swap/proxy,
  or an OpenAI-compatible URL). *How* it gets there (auto-install vs BYO) is the provisioner's
  business.
- The existing Windows scripts become the **Windows provisioner** implementing that contract —
  unchanged behavior, just named and documented as one pluggable implementation.
- The runtime performs a **capability probe** at startup (endpoints reachable? config present?) and
  degrades with a clear message instead of assuming the Windows setup ran. A non-Windows user can
  satisfy the contract manually (point at any OpenAI-compatible endpoint) with zero PowerShell.
- Document a "bring-your-own-runtime" path: neutral config + a reachable endpoint = working Bob core.

### Effort: 3–4 h.
### Acceptance
The Windows auto-install flow is byte-for-byte unchanged. On a box with **no** provisioner, setting
a neutral config pointing at any OpenAI-compatible endpoint yields a working `serve`/`agent` (probe
passes, clear error if the endpoint is down). Documented in a new `docs/PORTABILITY.md`.

---

## NB6 — Cross-platform CI (prove the core runs on Linux)

### Problem
Every test and the `check.ps1` gate run on Windows only — nothing *proves* the Python core is
portable, so "OS-agnostic" would be an unverified claim.

### Change (creates the CI workflow — C5)
- **Create `.github/workflows/ci.yml`** (per C5, NB6 is the owner; ND2/O10 later *extend* it): a
  Linux job (`ubuntu-latest`) + a Windows job (`windows-latest`) running the pure-Python core suite
  (loop, tools, sessions, server, memory, MCP, config resolver) plus `check.ps1`. No GPU.
- Mark any test that legitimately needs Windows with a skip guard; the goal is the **core** suite
  green on Linux. This job is the executable proof NB worked — and a regression fence so a future
  change can't silently re-Windows the core.

### Effort: 3–4 h.
### Acceptance
The Linux CI job runs the core suite green; a deliberately Windows-only construct in core code makes
it fail. `check.ps1` still gates Windows. Both wired into the N8 CI story.

---

## NB7 — Config authoring format (psd1 → neutral) — phased / optional

> **Update (2026-07-02): drift-killing subset implemented via Option A.** NB7 split into two
> independently useful halves:
> - **Option A — unify the *default* layer (DONE).** `Get-BobConfig` now seeds its runtime base from
>   `config/defaults.json.runtime` and deep-overlays `bob.psd1`, which keeps only its unique keys
>   (`persona.name/style`, `routing`, `voice`, `agent.toastAppId`). The ~25 duplicated runtime keys were
>   deleted from `bob.psd1`, so `defaults.json.runtime` is the single default source on both OSes. This
>   satisfies the "kill the drift (no more hand-mirrored constants)" goal without any new format or
>   exporter — cheapest path, reuses NB1/NB2, one `Get-BobConfig` edit + a deep-merge helper. A
>   resolver-parity test asserts Windows `config.json` runtime keys == Python `resolve_runtime_config()`.
>   (Ports were single-sourced to `defaults.json.ports` in the same pass.) See C2 status in
>   [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md).
> - **Option B — neutral TOML *authoring* format + `psd1 → toml` exporter (still deferred).** The
>   original blockquote below stands for this half: still not needed by the NB→P chain; revisit only if
>   non-Windows *authoring* (not running) becomes a real requirement.
>
> **Original decision (2026-07-02): deferred — not needed by the NB→P chain.** The module that would force
> it is NC, and it doesn't: NC1 keeps `.psd1` authoring on the pwsh side because
> `Import-PowerShellDataFile` is a **cross-platform pwsh 7 cmdlet** that reads `.psd1` fine under
> `pwsh` on Linux/macOS. The Python side is already psd1-free via NB2's resolver
> (`config/defaults.json` + `config/user.json`). ND/NE/O consume `verbs.json`, the registries, and
> `config.json` — none touch psd1 authoring. Building the exporter + a Python psd1 parser now would
> have **zero consumer** and bit-rot. Revisit only if non-Windows *authoring* (not running) becomes
> a real requirement.

### Problem
The deepest coupling is the authoring format: `models.psd1` / `bob.psd1` / `user.psd1` are
PowerShell data files, parseable only by PowerShell. NB1/NB2 route *around* this (neutral defaults +
a Python resolver), but the rich Windows authoring experience is still psd1.

### Change (phased — do only if/when non-Windows authoring is a real need)
- Introduce a neutral canonical authoring format (TOML recommended — comments + typed, human-first)
  as an **equal** input: PowerShell and Python both read it; `.psd1` becomes a Windows-convenience
  overlay that still works. Provide a one-shot `psd1 → toml` exporter so existing setups migrate
  losslessly.
- No forced migration: Windows users keep psd1; non-Windows users author TOML; both merge to the
  same runtime config.

### Effort: 6–10 h. **Defer** until NB1–NB6 land and a concrete non-Windows use case exists.
### Acceptance
The `psd1 → toml` exporter reproduces the current config exactly; a TOML-authored config on Linux
produces an identical runtime config to the psd1 path on Windows.

---

## Traceability (goal → sub-item)

| Goal | Sub-item(s) |
|------|-------------|
| Kill the drift (no more hand-mirrored constants) | **NB1**, **NB7** (deep) |
| Python runtime boots/runs without PowerShell | **NB2**, **NB4** |
| No Windows assumptions inside the Python core | **NB3** |
| Keep the auto-install/setup — as an asset, not a dependency | **NB5** |
| Portability is proven, not claimed | **NB6** |
| OS-agnostic authoring (future) | **NB7** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| new `config/defaults.json` | NB1 |
| `scripts/_models.ps1`, `scripts/bob_core.py` | NB1 (loaders), NB2 |
| new `scripts/bob_config.py` (runtime-subset resolver) | NB2 |
| new `scripts/osenv.py`; `scripts/tools/shell.py`, `scripts/tools/file.py` (OS-aware denylist), `scripts/bob_core.py` | NB3 |
| new `config/verbs.json`; new `scripts/bob/` package (`python -m bob`); `bob` shim (`.cmd` + POSIX); `scripts/bob.ps1` (thinned to dispatcher) | NB4 |
| `install_prereqs.bat`, `setup.bat`, `scripts/*provision*` (rename/doc only) | NB5 |
| new `.github/workflows/ci.yml` (linux + windows core jobs — **created here**, C5) | NB6 |
| new `config/*.toml` + `scripts/export-config.*` | NB7 |
| new `docs/PORTABILITY.md`; `CONTRIBUTING.md`, `docs/TUNING.md` | docs |
| `tests/*` (resolver subset, osenv, secret seam, denylist, command registry) | every sub-item |

## Verification (per item, as M/N/O)

- Python `py_compile` + the `unittest` suite; PowerShell AST parse; `scripts\check.ps1` gate (N8),
  now **also** run on Linux for the Python core (NB6).
- Config tests: the Python resolver yields every **runtime** key correctly from neutral sources (C2
  rule — not byte-identity with `Get-BobConfig`) (NB2); Python + PowerShell read identical ports/roles
  from `config/defaults.json` (NB1).
- Windows regression: the full auto-install + `bob up` + `.\scripts\test-dry-run.ps1` unchanged (NB5).
- Linux smoke: `python -m bob agent serve` → `/health`, an owner-scoped session round-trip, and an SSE
  stream against a stub model, with **no PowerShell in the process tree** (NB4/NB6).
- Cite `file:line` for every claim.

## Non-goals

A Linux/macOS **inference provisioner** (CUDA/Metal build, service management off-Windows) — NB
makes the *runtime* portable and defines the provisioner contract; a non-Windows provisioner is a
later module. Rewriting the Windows orchestration (it stays, as the Windows provisioner). Forcing a
config-format migration (NB7 is additive and phased). Changing the agent loop or tool model (that's
Module O). A web UI.
