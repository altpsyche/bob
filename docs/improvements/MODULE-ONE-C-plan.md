# Module ONE-C — Capabilities-as-tools + the deterministic invoker (detailed plan)

**Status:** executing. **C0 ✓ DONE** (§1a `--run` invoker + §1b osenv process/path/url seams, on main;
572 tests green). Decisions D1–D6 resolved (see Part 5). **Prereq:** ONE-A ✓ (config single-sourced),
ONE-B ✓ (one engine; text/vision/voice on the loop). **Read first:**
[MODULE-ONE-bob.md](MODULE-ONE-bob.md) (§ONE-C, the deprecation ledger, the architectural invariant),
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) (C1 dispatch, C6 registries, **C7 provisioner
native-first**), [../../plugins/AUTHORING.md](../../plugins/AUTHORING.md) (three-layer placement rule).

## Goal (unchanged from the module doc)

Turn the ~45 still-`pwsh` verbs into **Python capability functions**, each reachable three ways with **no
duplicated logic**:

```
one core fn (invoke.py / a tools/*.py module)
  ├─ agent tool     tool.py TOOL_DEFS+DISPATCH  → the loop calls it ("restart litellm")
  ├─ deterministic  bob --run <cap> [json]      → CI/scripts (same fn, no model)
  └─ kernel         scripts/bob/kernel.py        → cold-start calls the fn directly (ONE-D)
```

As each verb flips to `runtime=python` in the registry it stops delegating to `bob.ps1`
([cli.py `_exec_pwsh`](../../scripts/bob/cli.py); [bob.ps1 dispatch prologue :29-54](../../scripts/bob.ps1)).
ONE-C lands the capabilities + `--run`; ONE-D deletes the pwsh handlers and ports the bootstrap kernel;
ONE-E deletes the verb table. C7 governs: **native-from-source stays the default**, behind the
`capability_probe`/osenv seam — ONE-C does not build a portable provisioner.

---

## Part 1 — Shared infrastructure (build FIRST; every slice depends on it)

### 1a. The `bob --run <cap> [json]` deterministic invoker  *(small, do first)*
`--run` reuses the **exact** agent path: `ToolRegistry.build(config).dispatch_call(cap, args_json, ctx)`
([tool_registry.py:248](../../scripts/tools/tool_registry.py)) — no parallel dispatch, so CI and the agent
hit identical code. Design:
- Add a branch in [cli.py `main`](../../scripts/bob/cli.py): if `argv[0] == "--run"` (or a hidden `run`
  verb), take `cap = argv[1]`, parse the rest as one JSON object (`{}` if omitted) **or** `key=value`
  pairs; call `dispatch_call`; print the returned string; exit non-zero if it starts with an error marker
  (reuse the shell's `_ERR_MARKERS`, or have capabilities raise → dispatch returns `Tool error (...)`).
- **Decision D5:** flag `--run` vs verb `run`, and JSON-only vs `key=value` sugar. *Recommend* `--run`
  (matches the module doc + reads as a mode, not a capability) with JSON args + a `key=value` convenience.
- Tests: `--run memory_recall '{"query":"x"}'` returns the same as the tool; unknown cap → non-zero + clear
  message; malformed JSON → non-zero.

### 1b. osenv seams to ADD  *(the OS core of every lifecycle/provisioning port)*
From the seam audit. `Stop-ServiceByPid`/`Test-PortInUse` today live in `_models.ps1` (not `_platform.ps1`);
in Python the low-level primitive goes in [osenv.py](../../scripts/osenv.py), the orchestration wrapper
above it. Add (mirroring the pwsh `Resolve-*`/executor split, keeping `BOB_FORCE_OS` test hook):

| Seam | Windows | Linux | Notes |
|---|---|---|---|
| `os_name()` → win/linux/macos | `platform.system()` + `BOB_FORCE_OS` | idem | replaces 2-way `is_windows()`; `play_audio` already branches Darwin inline |
| `is_port_in_use(port,host)` | `socket.create_connection` | idem | identical both OSes (pwsh already cross-platform) |
| `pid_alive(pid)` | `psutil.pid_exists` / `os.kill(pid,0)` | idem | |
| `stop_process_tree(pid)` | `psutil.children()` + terminate (or `taskkill /T /F`) | `os.killpg(getpgid,SIGTERM)` + `pkill -P` | best-effort; dead pid ≠ error |
| `stop_processes_by_name(names)` | `psutil.process_iter` / `taskkill /IM` | `pkill -f` | C++ binaries killed by name (survive stale pidfiles) |
| `start_detached(argv, pidfile)` | `Popen(creationflags=DETACHED_PROCESS\|CREATE_NO_WINDOW)` | `Popen(start_new_session=True)` (setsid — required so killpg works) | writes pidfile |
| `exe_name(base)` | `base+".exe"` | `base` | |
| `venv_exe(venv,exe)` | `tools/<venv>/Scripts/<exe>.exe` | `tools/<venv>/bin/<exe>` | |
| `bin_exe(base)` | `bin/<base>.exe` | `bin/<base>` | staged native binaries |
| `home_config_dir(app)` | `%USERPROFILE%\.config\<app>` | `$XDG_CONFIG_HOME\|~/.config/<app>` | |
| `open_url(url)` | `webbrowser.open` (or `os.startfile`) | `xdg-open` | `up` opens the WebUI |
| scheduler quartet | see Part 3 | see Part 3 | `register/unregister/agent_task_status/crontab_available` |

Already covered (no work): `data_dir`, `cache_dir`, `secret`, `notify` (Linux), `default_shell`,
`play_audio`, `record_audio`. **HTTP: use `requests` directly — drop the `Get-CurlExe` seam** (health
checks/downloads become `requests` calls; a resumable download uses a `Range`-header stream if needed).
**Toast:** `notify()` exists but the Windows path is a `win10toast` best-effort; the rich WinRT toast stays
in `bob-toast.ps1` for now (low urgency). **Build-time seams (CUDA/cmake/venv/package/NUMA/RAM) are NOT in
this list** — see Part 4 / Decision D2.

### 1c. Neutralize `models.psd1` → `models.json`  *(the gating prerequisite — the biggest risk)*
**This is the single hard dependency for the whole provisioning half of ONE-C.** Today `config/models.psd1`
(241 lines, PowerShell hashtable DSL) is the sole source of model selection and is read **only** by
PowerShell (`Get-ModelsConfig`, then `gen-*.ps1`, `fetch-models.ps1`, `verify-urls.ps1`, `eval.ps1`,
`diagnose.ps1`, `_versions.ps1`, and the `status/models/show/profile/profiles/bench` verbs). **No Python
reads it** — `Get-BobConfig` copies only the port scalars from `models.psd1.defaults` into `config.json`;
the profiles/repos/ggufs/macros/group/peers are a PowerShell-only island.

Reading `.psd1` from Python is brittle (bespoke parser, or shelling `pwsh` — the very dependency ONE removes).
**The right move (precedent: `defaults.json` NB1 + the NB7 `bob.psd1`-thinning): convert to `models.json`,**
read by Python `json.load` and by PowerShell `ConvertFrom-Json -AsHashtable` (one-line change in
`Get-ModelsConfig`). The data maps cleanly (bools→true/false, `flags`→arrays, `setParams`/`pro`→objects,
`_`-prefixed metadata preserved). Watch-items:
1. **`Set-ActiveProfile` write-back** ([_models.ps1:427](../../scripts/_models.ps1)) regex-rewrites the
   `activeProfile` line in place to preserve comments; JSON has no comments. **Decision D4:** split the
   writable `activeProfile` (and user-tunable defaults) into a tiny file (e.g. `config/active-profile.json`
   or `data/`-side state); keep the big registry **read-mostly + documented**. `bob profile` writes only
   the small file.
2. **`user.psd1` overlay** (deep-merge in `Get-ModelsConfig`) re-homes to neutral `user.json`; reuse
   Python's existing [`_deep_merge`](../../scripts/bob_config.py).
3. **Parity gate** like `test_defaults_parity`: both languages resolve the registry identically.
4. **The four generators stay PowerShell for now** — see Slice F. Neutralizing the *data* unblocks all the
   read-only verbs immediately; porting the generators comes later.

---

## Part 2 — Full capability inventory (so nothing is missed)

Disposition legend: **AGENT** = expose as an in-loop tool; **CLI** = keep as a `bob`/`--run` command, not a
default agent tool (foreground/blocking, privilege, or long/dangerous); **read-only** vs **mutating**
matters for approval-gating. "Ported?" notes existing Python surface.

### Orchestration / lifecycle (depends on §1b osenv seams; NOT on models.json except `status`)
| Verb | Does | Disposition | Notes / port risk |
|---|---|---|---|
| `up` | bg bring-up: start.ps1 detached → poll `/v1/models` → open-webui → open browser; `-WithServices`→docker | AGENT (mutating, long) | needs `start_detached`, readiness poll (`requests`+`is_port_in_use`), `open_url`; structured readiness reporting; support `--no-open` |
| `serve` | foreground llama-swap+litellm (regens config first) | CLI | blocking; agents use `up` |
| `restart` | name-kill llama-swap/llama-server/open-webui + pidfile kills → start.ps1 | AGENT (mutating) | hand-rolls kills today (no child reap) — **unify on `stop_service_by_pid`/`stop_process_tree`**; prefer bg restart for agent |
| `stop` | teardown: name-kill C++ bins + pidfile tree-kill python svcs + `docker compose down` | AGENT (mutating) | canonical teardown |
| `down` | **no handler today** | — | **Decision D1:** add as `stop` alias (recommend yes) |
| `status` | `/v1/models` + configured-model table + whisper/piper port probes | AGENT (read-only) | model table needs the registry → **light dep on §1c**; degrade to endpoint+probes if registry absent |
| `ps` | per-service pidfile → RAM/uptime/liveness | AGENT (read-only) | `psutil` (rss, create_time) |
| `logs` | tail `logs/llama-swap.log` (`-Wait` follow) | AGENT (bounded read) + CLI (follow) | expose bounded read to agent; follow stays CLI |
| `webui` | foreground open-webui | CLI | opt-in, blocking |
| `litellm` | start/stop/status LiteLLM (pidfile) | AGENT | `stop_service_by_pid`, `start_detached` |
| `whisper` | start/stop/status whisper-server (pidfile, native bin) | AGENT | `bin_exe`, name stale-kill |
| `piper` | start/stop/status piper (python host wrapping native bin) | AGENT | `bin_exe`+`venv_exe`; tree-kill reaps piper child |
| `services` | docker compose up/down/status/logs (langfuse/searxng/n8n) | AGENT (optional) | needs docker; lower priority |

PID convention: `logs/<svc>.pid`. Name-kill set (survive stale pidfiles): `llama-swap`, `llama-server`,
`whisper-server`, `open-webui`. The `start-*.ps1` helpers (start.ps1, start-litellm.ps1, start-whisper.ps1,
start-piper-server.ps1, up.ps1) are the real logic to port into a `scripts/tools/stack.py`-style module.

### Model registry — read-only + profile (depends on §1c models.json)
| Verb | Does | Disposition | Notes |
|---|---|---|---|
| `models` | list active profile roles + load state (queries `/v1/models`) | AGENT (read-only) | trivial once registry is Python-readable |
| `show <role>` | file/VRAM/repo/SHA/disk for one role | AGENT (read-only) | reads `models/manifest.json` |
| `profiles` | list VRAM profiles + sizes + suggestion | AGENT (read-only) | |
| `profile <name\|auto>` | switch active profile; `auto`=detect VRAM; regens llama-swap + `fetch --list` | AGENT (mutating) | **write-back → the split writable file (D4)**; needs GPU VRAM detect |
| `verify-urls [profile]` | HEAD every HF resolve URL | AGENT (read-only, network) + CI gate | `requests` HEAD; uses `main` rev |
| `bench [role]` | `llama-bench` on a role's gguf | AGENT (read-only-ish) | `bin_exe`, resolves role→gguf via registry |
| `eval <role> [task]` | lm-eval quality benchmark | CLI (very long) | separate `venv-eval`; reads `tokenizer` from registry |

### Config generation (depends on §1c; largest port)
| Verb | Does | Disposition | Notes |
|---|---|---|---|
| `gen` | regenerate llama-swap.yaml + litellm.yaml + continue + webui.db | AGENT (mutating) + CLI | ~400 lines pwsh across 4 generators; llama-swap `cmd` assembly + webui sqlite writer are the tricky bits; port LAST |

### Agent lifecycle + scheduling (see Part 3 for the seam)
| Verb | Does | Disposition | Notes |
|---|---|---|---|
| `agent install/uninstall/status` | register/remove/query the every-minute OS task | AGENT/CLI | scheduler quartet seam |
| `agent log` | tail `logs/bob-agent.log` (follow) | AGENT (bounded) + CLI | |
| `agent schedule add/list/run/remove/enable/disable/install/status` | CRUD over `data/schedules.json` + fire jobs | AGENT (mutating for CRUD) | the runner `bob-agent.ps1` + `Test-CronDue` must port |

### Memory + meta (mostly trivial re-exposures — good early wins)
| Verb | Does | Disposition | Ported? |
|---|---|---|---|
| `remember <text>` | store a memory | AGENT already (`memory_store` tool) + verb | **wire verb to `bob_core.memory_store`** |
| `recall <query>` | recall memories | AGENT already + verb | **wire to `bob_core.memory_recall`** |
| `memory <sub>` | status/clear/list/show/forget/edit/pin/export/migrate/init-profile | CLI | 1:1 onto existing `bob_memory.py` argparse subcommands |
| `budget` | LiteLLM spend + db size summary | AGENT (read-only) | `requests` + regex on litellm.yaml |
| `tools [list\|test\|info]` | tool catalog | CLI | engine `tool_loader.py` already Python; `agent tools` dup |
| `plugins list` | enumerate `plugins/*/` | CLI | filesystem scan |
| `fabric` | passthrough to `bin/fabric` | CLI | `bin_exe` + subprocess |
| `aider` | passthrough to `venv-aider/aider` | CLI | `venv_exe` + subprocess |

### Health / diagnostics
| Verb | Does | Disposition | Notes |
|---|---|---|---|
| `setup [check]` | `Invoke-BobHealthCheck` (deps/registration) | AGENT (read-only) | shares one fn with `doctor` |
| `doctor` | health + runtime (endpoint/GPU/writable/lock) | AGENT (read-only) | port together with `setup` |
| `diagnose` | deep machine-readiness (GPU arch/VRAM/RAM/CUDA/NUMA/mlock/model files) | AGENT (read-only) | **deepest OS discovery** (nvidia-smi, CUDA dirs, NUMA, secedit/ulimit) — heavy; reads registry |
| `version` | Bob/binary/submodule versions | AGENT (read-only) | `bin_exe`+git |

### Setup / build / privilege — **stay pwsh in ONE-C** (Decision D2), port in ONE-D
| Verb | Why keep pwsh (for now) |
|---|---|
| `setup` (full via setup.bat/sh), `setup-voice` | multi-step build+download; whisper.cpp build; OS archive extract |
| `build`, `update` | cmake<4 / VS-vs-Ninja / CUDA host-compiler pinning / DLL staging / git submodules / bin rollback — deepest toolchain |
| `mlock` | `secedit`+UAC elevation (Win) vs `ulimit`/limits.conf (Linux); privilege-heavy; expose only `-Check` read-only |
| `lock` | ND1 reproducibility gate; git gitlinks + pip freeze; `--check` is a CI gate |
| `fabric-setup` | Go build + submodule + `~/.config/fabric` |
| `fetch` | agent-facing ("download the coder model") but needs streaming + long-timeout; port when convenient (needs §1c) |

These become the ONE-D tail (port using the build-time osenv seams — CUDA/cmake/venv/package — or move to a
Python `bootstrap`/`build` module). Until then they remain `runtime=pwsh` and delegate to `bob.ps1`; that is
compatible with the phased C1 contract.

---

## Part 3 — The scheduling seam (the trickiest agent-lifecycle piece)

Two layers, both must port:

**Layer 1 — the OS task that ticks every minute** (osenv scheduler quartet):
- `register_agent_task(script, task_name="BobAgent")` — Win: `schtasks /Create /SC MINUTE /MO 1 /TN BobAgent
  /TR "pwsh ... -File <runner>" /F` (pwsh ScheduledTasks cmdlets aren't callable from Python → use
  `schtasks.exe`); Linux: idempotent `crontab -l | filter-tag | crontab -` adding `* * * * * <pwsh> -File
  "<runner>" # BobAgent` (the `# BobAgent` tag is the removal/detection key). Guard on `crontab_available()`;
  warn if no daemon (`systemctl is-active cronie/cron/crond`).
- `unregister_agent_task` / `agent_task_status` → `{registered,state,next_run}` / `crontab_available`.
- **Note:** the runner it fires is `bob-agent.ps1` today; once ported the task should fire `bob --run
  agent_tick` (or `python -m bob …`) so the runner is Python too.

**Layer 2 — the runner + cron evaluator** (pure Python, not an osenv seam):
- Port [`Test-CronDue` (_models.ps1:399-425)](../../scripts/_models.ps1): UTC, 5 fields, supports `*` / comma
  lists / ranges `a-b` / integers only — **no `*/n` step syntax, no JAN/MON names**, day-of-week Sunday=0,
  and a **60-second re-fire guard**. **Decision D3:** port these exact semantics (parity + zero dep) vs adopt
  `croniter` (richer, but changes behavior). *Recommend* exact port now; upgrade deliberately later.
- Port the runner (`bob-agent.ps1`): gate on `agent.enabled`; read `data/schedules.json`; for each enabled
  entry, `Test-CronDue` → run `run_agent(goal, role, agency="silent")` in-process (not a subprocess now that
  the loop is Python); update `lastRun`/`lastRunResult` (truncate to `agent.maxResultChars`); atomic
  write-back; `osenv.notify()` if `entry.notify`.
- `data/schedules.json` schema (JSON array): `{name, cron, action:{type:"agent",goal,role}, notify,
  notifyTitle, enabled, lastRun, lastRunResult, createdAt}`. Atomic writes (temp+replace).

---

## Part 4 — Recommended sequencing (each slice = independently shippable + committable)

Ordered by dependency and value; a slice is the template the rest follow.

- **C0 — foundation** *(no verb ports):* §1a `bob --run` invoker · §1b osenv process/path/url seams (+ tests).
  Small, unblocks everything.
- **Slice 1 — Memory + meta** ✅ **DONE** (on main; 587 tests green): `remember`/`recall` → cli handlers over
  `bob_core.memory_store/recall`; `memory <sub>` → delegates to `bob_memory.py`'s argparse; `budget` →
  new `scripts/tools/budget.py` (agent tool `budget_summary` + `bob budget` verb + `bob --run
  budget_summary` — the full 3-adapter template); `tools`/`plugins`/`fabric`/`aider` → cli handlers over
  `tool_loader.py` / a `plugins/` scan / `osenv.bin_exe`+`venv_exe` passthroughs. All 8 verbs flipped to
  `runtime=python`, verbs.json regenerated, the 8 dead `bob.ps1` cases + orphaned `bob-budget.ps1`
  deleted (`bob-memory.ps1` kept — still used by `onboard.ps1`, an ONE-D script). D6 functional-grouping
  noted in AUTHORING.md. tests/test_slice1_meta.py (15).
- **Slice 2 — Lifecycle** *(the real template; high agent value):* `ps`, `logs`, `stop`, `restart`, `up`,
  `serve`, `webui`, `litellm`, `whisper`, `piper`, `services`, `down`(new). Depends on §1b. Port the
  `start-*.ps1` logic into a `scripts/tools/stack.py` module; mark mutating ones approval-gated.
- **Slice 3 — Health:** `setup`(check), `doctor`, `version`, then `diagnose` (heaviest — GPU/CUDA/NUMA discovery).
- **C0c — models.json neutralization** *(the gating prerequisite for the rest):* convert `models.psd1` +
  `user.psd1` → JSON with the split writable `activeProfile`; teach `Get-ModelsConfig` to read JSON; add a
  Python model-registry reader + parity gate.
- **Slice 4 — Registry readers:** `models`, `show`, `profiles`, `profile`, `verify-urls`, `bench`. (`eval`
  optional/CLI.) Depends on C0c.
- **Slice 5 — Scheduling:** the Part 3 seam + runner + `agent install/uninstall/status/log/schedule`.
- **Slice 6 — Generators:** port `gen` (llama-swap/litellm/continue/webui). Largest; depends on C0c.
- **Deferred to ONE-D:** `setup`(full)/`setup-voice`/`build`/`update`/`mlock`/`lock`/`fabric-setup`/`fetch`
  (toolchain/privilege/git-heavy; need build-time seams). Keep `runtime=pwsh` meanwhile.

After each slice: flip the verbs to `runtime=python` in [registry.py](../../scripts/bob/registry.py),
regenerate `verbs.json` (`python -m bob.registry`), delete the now-dead `bob.ps1` case(s), run the parity +
verbs-sync gates, and commit.

---

## Part 5 — Decisions (RESOLVED 2026-07-05)

- **D1 `down`:** ❌ NO alias. "One clean way to start and stop, no silly alias that confuses which is
  correct later." `stop` stays the single canonical teardown; the existing hidden `down` registry entry
  gets **deleted** in Slice 2 (not ported).
- **D2 build/privilege verbs:** port `fetch`, `mlock -Check` (read-only path only), and `lock --check`
  (CI check path only) **in ONE-C**; everything else
  (`build/update/setup-voice/fabric-setup/setup`-full + the write/privilege paths of mlock/lock) stays
  pwsh and ports in **ONE-D**.
- **D3 cron:** ✅ exact `Test-CronDue` port (parity, zero dep). Upgrade to richer syntax deliberately later.
- **D4 models.json split:** writable `activeProfile` lives in **`data/`-side state** (e.g.
  `data/active-profile.json`); the `models.json` registry stays fully read-only + version-controlled.
- **D5 `--run` surface:** ✅ single clean surface — **`bob --run <cap> '{json}'`** (a mode flag, JSON-only,
  no `run` verb, no `key=value` sugar). Kept out of the registry/catalog. *(implemented in C0.)*
- **D6 placement:** ✅ **functional grouping** into a few `scripts/tools/*.py` modules (`stack.py`,
  `models.py`, `schedule.py`, `provision.py`) each exposing several related tool fns; cli handlers +
  `--run` import the same cores. Bends the strict one-dir-per-verb AUTHORING rule — note it in
  AUTHORING.md when Slice 1 lands.

**C0 implementation notes:**
- §1a `--run` is a branch at the top of [cli.py `main`](../../scripts/bob/cli.py) → `_handle_run` →
  `_build_registry` (mirrors the loop's `disabledTools` parsing) → `dispatch_call`. Exit non-zero on an
  error result (reuses `shell._is_error_result`); JSON must be an object.
- §1b osenv seams added: `os_name()` (tri-state, honors `BOB_FORCE_OS`; `is_windows()` now delegates),
  `is_port_in_use`, `pid_alive` (psutil-optional, **zombie-aware on Linux via /proc**), `stop_process_tree`,
  `stop_processes_by_name`, `start_detached`, `exe_name`/`venv_exe`/`bin_exe`/`home_config_dir`, `open_url`.
  psutil is an optional accelerator (lazy import + stdlib fallback) — it is NOT installed in this env.
  **Caveat:** `stop_processes_by_name` uses `pkill -f <name>` on POSIX (broader than pwsh's exact
  `Get-Process -Name`, but needed to catch python-hosted services like open-webui) — Slice 2 must pass
  specific daemon names. Scheduler quartet (`register/unregister/agent_task_status/crontab_available`)
  deferred to Slice 5.

## Part 6 — Acceptance (per the module doc)
Each capability invoked identically via the agent and via `bob --run <cap>` (one function in the stack);
`git grep` finds no capability logic duplicated across adapters; the parity + verbs-sync gates stay green;
a live smoke: the agent brings the stack up, reports status, restarts litellm, switches profile, and
schedules/fires a job — all in-loop.
