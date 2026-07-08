# Architecture Contracts (roadmap NB → P)

**Purpose.** The portability + capability modules (NB, NC, ND, NE, O, P) all touch a handful of
cross-cutting seams — the command front door, config resolution, secrets, data location, CI, and the
registries. If each module defines these independently they drift (the review of the first drafts
found *three* modules describing *three* different front doors). This file defines each seam **once**;
every module references it instead of re-deciding. If a module needs to change a contract, it changes
it *here* and notes it — never silently in-module.

Read this before implementing any NB–P module.

> **Migration note (current).** The MODULE-ONE migration is complete: Bob is now **one word, one
> engine (Python), zero PowerShell orchestration**. The contracts below were written during the
> two-runtime era; each has been updated to the shipped code. Where a contract once described a
> `pwsh`/`python` split, a generated verb table, or a `.psd1` config source, that machinery is
> **deleted** — the current reality is stated in each contract body and its Status note. The only
> tracked `.ps1` left is the sample plugin `plugins/play/invoke.ps1`.

---

## C1 — The `bob` dispatch contract (front door)

**Problem it resolves:** who runs `bob <cmd>` and how, given that (a) commands split into
fully-qualified paths, not just top-level verbs (e.g. `bob agent serve` vs `bob agent install` both
live under `agent`), and (b) **bootstrap commands run before the Python venv exists.**

*Historical:* the split used to be per-command across two runtimes (PowerShell orchestration +
Python runtime). That is gone — **every verb is Python** now.

**Contract:**
1. `bob` is a **dumb shim** (`bob.cmd` on Windows written by the kernel's `install_cli`, a POSIX `bob`
   script elsewhere) — no logic beyond resolving the repo dir and handing off to `python -m bob`
   (see `/home/siva/dev/bob/bob`). It carries **no command list**.
2. **Dispatch is data-driven from ONE source:** `scripts/bob/registry.py`'s `COMMANDS` list. There is
   **no** generated verb table (`config/verbs.json` and all its machinery are **deleted**) and **no**
   per-command `runtime` field (every verb is Python). `cli.py:_resolve()` matches a 2-token path
   (e.g. `agent serve`) before the bare verb, looks the name up in `registry.by_name()`, and calls the
   matching `_HANDLERS[...]` entry (`scripts/bob/cli.py:17,46,1004`). An unknown command prints help
   and exits 2. Adding a command = **one entry in `COMMANDS` + one `_HANDLERS` handler** — no regen,
   no sync gate.
3. **Bootstrap (pre-venv)** goes through the shell stubs (`setup.sh`/`install_prereqs.sh`) →
   `python -m bob.kernel` (Tier 1, `scripts/bob/kernel.py`), which itself sits on
   `scripts/bob/install_prereqs.py` (Tier 0, stdlib-only). `bob setup` therefore works with no venv;
   the normal Python entry (`python -m bob`) is reached for everything after provisioning.
4. **The whole catalog is `python -m bob`** — `setup, up, serve, stop/restart, build, gen, fetch,
   doctor, models, profile, agent` + its subcommands, `chat/code/think, voice/listen/transcribe/speak,
   describe/screenshot, memory/recall/remember, skill, tools`, etc. There is **no PowerShell
   orchestration layer and no `bob.ps1` front door.**
5. `python -m bob` is a **real importable package** (`scripts/bob/`) exposing `run_agent_events` et al.
   as an API, so NE's REPL consumes events in-process. The event API is **bidirectional**: it carries
   an `approval_required` request / `approve`-callback response and `call_id`-correlated tool events,
   so the shell renders approvals without a blocking `input()`.
6. **No-arg on an interactive TTY launches the shell** (`cli.py:main` → `bob.shell.is_interactive()` →
   `_handle_shell`); a non-interactive/piped/CI no-arg prints help, so scripts are unaffected. The
   **deterministic invoker** `bob --run <cap> '{json}'` dispatches one capability through the exact
   agent path (`ToolRegistry.dispatch_call`) — a mode flag, **not** a registered verb / catalog entry.
7. **Naming (no collision).** `bob serve` = the **inference stack** (llama-swap + LiteLLM), foreground;
   `bob up` = background bring-up (endpoint + proxy + WebUI). The **agent HTTP server** is
   `bob agent serve` (FastAPI, Bearer auth). There is **no** bare `python -m bob serve` meaning the
   agent server.
8. **The one remaining PowerShell touchpoint is intentional and is *not* a front door:** the OS-native
   *tool* shell used to execute command strings is `pwsh` on Windows via `osenv.default_shell()`
   (`scripts/osenv.py:47` — bash/sh elsewhere). That is a tool-execution seam, not orchestration.

**Owner:** **NB4** built the shim, the `scripts/bob/` package, and the command registry (C6). ND/NC
made provisioning cross-platform. ONE-D ported the cold-start kernel to Python
(`scripts/bob/kernel.py` + `install_prereqs.py`). **ONE-E deleted the verb table and the last
PowerShell** (see Status).

**Status (current, post ONE-E):** `scripts/bob/registry.py` `COMMANDS` is the **sole** dispatch + help
+ catalog source. `config/verbs.json` and its generator/sync-gate are **gone**; the `runtime` field is
**gone**. `python -m bob` (via the POSIX `bob` shim / `bob.cmd`) is the only front door; cold-start is
shim → `python -m bob.kernel` → `install_prereqs.py`. **Zero orchestration PowerShell remains** —
`git ls-files '*.ps1'` = only `plugins/play/invoke.ps1`.

---

## C2 — The config contract (neutral `defaults.json` + `user.json`, resolved in Python on every OS)

**Problem it resolves:** the runtime must run on any OS without re-implementing a PowerShell config
merge in Python and drifting from it.

*Historical:* Windows once compiled `config/bob.psd1` → `data/config.json` via `Get-BobConfig`, while
non-Windows used a Python resolver. **Both `bob.psd1` and the compiled `data/config.json` step are
retired**, and `Get-BobConfig` no longer exists — there is now **one** resolve path.

**Contract:**
1. **One resolve path, every OS:** `config/defaults.json` **deep-merged** with an optional
   `config/user.json` (or `user.toml` if present), in pure Python via
   `scripts/bob_config.py:resolve_runtime_config()`. `bob_core.load_config()` calls it
   **unconditionally** (`scripts/bob_core.py:87`) — nothing reads a `.psd1`, nothing compiles or reads
   `data/config.json`, and the runtime never *requires* `bob gen`.
2. **Runtime subset only.** The resolver produces the ~15 keys the Python core reads: `port`,
   `litellmPort`, `agentPort` (nested under `agent`), `searxngPort`, `litellmKey`, `routing.*`,
   `persona.*`, `agent.*`, `memory.*`, `vision.*`, `voice.*`. It never reproduces provisioner keys
   (profiles, model file paths, build flags) — those live in `config/models.json` and are consumed by
   the provisioner, never required by the runtime.
3. **Shared constants live once.** Ports and the `roleTable` live only in `config/defaults.json`, read
   by Python (`json.load`). Routing default *values* are **derived** from the `roleTable`
   (`bob_config._routing_from_role_table`), not mirrored. `defaults.json.ports` is the sole port
   source; a parity test fails the build if a shadow port copy is reintroduced.
4. **User overrides are top-level, deep-merged.** `config/user.json` is the runtime-config shape (e.g.
   `{"agent": {"maxSteps": 3}}`), **not** wrapped under a `bob` key, deep-merged over the defaults via
   the single `load_user_overlay` loader (`scripts/bob_config.py:45`). A malformed overlay is
   **ignored**, never fatal.

**Owner:** **NB1** (`config/defaults.json`), **NB2** (the Python resolver `bob_config.py`).

**Acceptance rule:** parity is "the runtime receives every runtime key it needs, correctly," proven by
a resolver test — there is no longer a PowerShell merge to be byte-identical to.

**Status (current):** `config/defaults.json` is the neutral single source (`ports` + `roleTable`
shared, `runtime` for the resolver). `scripts/bob_config.py:resolve_runtime_config()` produces the
runtime subset; `bob_core.load_config()` calls it on every OS. `config/bob.psd1`, the compiled
`data/config.json` step, and `Get-BobConfig` are all retired.

---

## C3 — The secrets contract

**Problem it resolves:** API keys (DeepSeek/HF/Langfuse), `litellmKey`, and per-client `apiTokens` need
a cross-platform home that is never a git-tracked file.

**Contract:**
1. A single resolver seam — `osenv.secret(name)` (`scripts/osenv.py:118`) — resolves in this
   precedence: **process env (exact name, then `BOB_<UPPER>`) → OS keychain (`keyring`, optional:
   Keychain / Credential Manager / `secret-tool`) → `<data-dir>/secrets.json` → default**. No secret is
   ever read from a git-tracked file. `bob_core._litellm_key` resolves through it
   (`scripts/bob_core.py:104`).
2. `litellmKey` and `apiTokens` are **secrets**, not plain config — they live via the secret seam (env
   or `secrets.json`), never inlined in a checked-in `user.json`/config. Config may carry a
   *reference*, not the value.
3. The **`file_read`/`file_write` denylist is OS-aware and data-dir-relative** (`scripts/tools/file.py`):
   it denies the resolved `secrets.json`, the data-dir DBs, `logs/`, `.env*`, **plus** platform secret
   dirs (`~/.ssh`, `~/.aws`, `~/.config/bob`, `~/.gnupg`, and the systemd `EnvironmentFile` path on
   Linux; the DPAPI/credential store paths on Windows).

**Owner:** **NB3** (the `secret()` seam + OS-aware denylist in `file.py`). **O8** (token store) builds
on this seam; **NC** wires provider secrets into services via it (e.g. a systemd `EnvironmentFile` fed
from the secret seam).

**Status (current):** `osenv.secret(name)` resolves env → keychain → `data/secrets.json` → default
on every OS; there is **no** separate PowerShell mirror (the former `Get-Secret` seam is retired with
the rest of the pwsh layer). `file.py`'s denylist is OS-aware.

---

## C4 — Data & state location policy

**Problem it resolves:** `sessions.db`, `bob.db`, `schedules.json`, `logs/` need a defined home that is
portable and requires no migration in the common single-checkout case.

**Contract:**
1. **Default: repo-relative `data/` and `logs/`** on all OSes (the local-first, single-checkout case —
   simplest, zero migration). Paths are resolved with `pathlib`.
2. `osenv.data_dir()` / `osenv.cache_dir()` (`scripts/osenv.py`) return the repo-relative dirs by
   default, and an XDG / `%LOCALAPPDATA%` location **only** when `BOB_DATA_DIR` (or a config key) is set
   — reserved for a future system-install / multi-user mode.
3. When the non-default dir is used, a **one-time migration** copies existing `data/*` on first run
   (stamped, never re-copies). Not needed for the default path.

**Owner:** **NB3** defines `data_dir()`/`cache_dir()` and the policy; NC/ND inherit it (no migration on
the default path).

**Status (current):** `osenv.data_dir()`/`cache_dir()` return repo-relative `data/`/`logs/` by default;
`BOB_DATA_DIR` relocates them with a one-time, stamped copy migration. There is **no** PowerShell
`Get-DataDir` mirror — the policy is Python-only, used by `bob doctor`'s writable-dir checks.

---

## C5 — CI ownership (one workflow, additive)

**Problem it resolves:** NB6, ND2, and O10 each "create" `.github/workflows/ci.yml` with conflicting
shapes.

**Contract:** there is **one** `.github/workflows/ci.yml`, extended (never recreated) in order:
1. **NB6** creates it: the Python **core suite** on `ubuntu-latest` **and** `windows-latest`, plus the
   **`scripts/check.py` gate** (py_compile + `versions.lock` sync + entrypoint exec-bits + unittest).
   Runs on every PR. No GPU.
2. **ND2** extends it: a **fresh-install acceptance matrix** — a **CPU tier** (no GPU, `bob build --cpu`
   / `python -m bob.kernel`) on both OSes every PR, plus an **end-to-end smoke** (`scripts/smoke.py`),
   and a **GPU tier** (self-hosted) on release tags.
3. **O10** extends it: the **agent-capability eval** job, running on the existing matrix (not a new
   Windows-only runner).

**Owner:** NB6 creates; ND2 and O10 add jobs to the same file.

**Status (current):** `.github/workflows/ci.yml` runs `core-suite` (the `python scripts/check.py`
gate) on `ubuntu-latest` + `windows-latest`, plus `acceptance-cpu` (fresh-install + `scripts/smoke.py`
on the CPU tier, both OSes, gating) and a non-gating GPU/release-tag tier. The gate is
**`scripts/check.py`** (the former `check.ps1`/`smoke.ps1` were ported to `scripts/check.py` /
`scripts/smoke.py` in ONE-E). There is **no** verbs-table sync gate (the table is deleted).

---

## C6 — Registries: commands, tools, skills (data, not hardcoded)

**Problem it resolves:** the interactive UI and help must render "everything Bob can do" without a
hand-maintained menu, and O's new capabilities must appear without a UI rewrite.

**Contract:**
1. **Tools** — auto-discovered by `ToolRegistry`. Unchanged.
2. **Commands** — a **command registry** (`{name, group, summary, args, handler}` + optional `hidden`
   to keep a command dispatchable but out of the catalog) is the **single source for dispatch (C1),
   help, and the catalog** (`scripts/bob/registry.py`). There is **no `runtime` field** (every verb is
   Python) and **no generated verb table** to keep in sync. A parity test forbids a dispatchable verb
   that isn't registered, so the catalog can't drift from what dispatch accepts.
3. **Skills** — a **skills registry** parallel to the tool registry (auto-discovered, contract-
   validated). **NE** builds the registry + catalog rendering (skills appear in the splash). **O**
   builds skill *execution* (a skill may spawn a sub-agent — an O1 consumer).
4. The interactive shell (NE) and every UI surface render **from these registries** and from the
   `run_agent_events` event stream — so O's new event types (sub-agent spawned, permission required,
   parallel-tool progress) surface by being emitted, not by editing the shell.

**Owner:** command registry → **NB4**; tool registry → existing; skills registry/catalog → **NE**;
skill execution → **O**.

**Status (current):** the command registry (`scripts/bob/registry.py` `COMMANDS`) is the true catalog —
every verb is registered and grouped into six buckets (Talk/Act/Make/Know/Run/Config), with an opt-in
`hidden` flag; `commands(include_hidden=False)` feeds help. It is now the **sole** dispatch source too:
no generated `verbs.json`, no `runtime` field, no sync gate. Skills registry/catalog and execution stay
per the owners above.

---

## C7 — Provisioner backend strategy (native default now; portable when Linux/mac get real users)

> **Decision: keep native-from-source as today's default; do NOT build a `provisionMode` multi-backend
> abstraction now.** Portable backends (prebuilt binary → `docker compose` inference → BYO
> OpenAI-compatible endpoint) become the Linux/macOS default *when those platforms get real
> daily-driver users* — added behind the existing provisioner seam, with zero caller changes.
> Native-from-source stays the opt-in max-control tier. macOS, when it comes, is scoped portable-first.

**Problem it resolves:** whether to flip the Linux default from native-from-source to a portable
provisioner *now* — a *timing* question, not architecture.

**Why defer (YAGNI on a zero-user platform).** The expensive, hard-to-retrofit part — the provisioner
*seam* ("runtime only needs a resolvable config + a reachable OpenAI-compatible endpoint") — **already
exists**, now entirely in Python: the OS seams in `scripts/osenv.py`, the cold-start
`scripts/bob/kernel.py` (+ `install_prereqs.py`), and `capability_probe`. The additional backends are
cheap to add later and speculative now. Deferring lets the backends be designed against real needs,
behind a seam that already supports them.

**What this contract fixes for the modules below it:**
- **NC** — native-from-source is the shipped default (via `python -m bob build` / `bob.kernel`) *and*
  the opt-in max-control tier. When portable backends are added, they slot behind the existing Python
  seam; callers are unchanged.
- **ND** — the per-PR **gating** acceptance path is the portable/CPU tier (`bob build --cpu`), **not**
  native-from-source CUDA. Native-from-source builds are exercised only in the non-gating GPU/release-
  tag tier, so a fragile native build can never red the per-PR gate (see ND2 / C5).
- **macOS (future module)** — scoped **portable-first** (prebuilt/BYO), never native-from-source-first.

**Owner:** policy recorded here; NC honors it (native default, seam ready); ND2 honors it (portable/CPU
gates, native non-gating); the future macOS module inherits portable-first. Adding the portable
backends later is an additive change behind the Python provisioner seam — it **fulfills** this contract.

---

## Dependency graph (authoritative)

```
N ✓
└─ NB ✓ portable core        — C1 dispatch, C2 config, C3 secrets, C4 data, C6 command registry, NB6 creates CI (C5)
    └─ NC ✓ provisioner        — cross-platform (osenv seams + bob.kernel); CUDA + CPU build tiers; degrades w/o GPU; native default, portable-later behind seam (C7)
        └─ ND  release          — reproducible; extends CI with the fresh-install matrix (C5)
            └─ NE  interface     — one `bob`; extends the command registry (C6); skills catalog only
                └─ O  capability — depends NB+NC+ND; dual-OS sandbox; extends CI eval (C5); skill execution
                    └─ P  frontier product — durable/resumable runs, deep multimodal, computer-use
```

**Note (post ONE-E):** the NB–NE portability + interface work is complete and the two-runtime split it
mediated is gone — Bob is one Python engine. The contracts above are kept as shared vocabulary (C1..C7
are referenced by later plans) and describe the shipped code, not a plan.

**Rule:** a module may only *extend* a lower module's contract, never redefine it. Contract changes
land here first.
