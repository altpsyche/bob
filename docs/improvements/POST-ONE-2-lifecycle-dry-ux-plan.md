# POST-ONE-2 — Lifecycle DRY + UX coherence

> **STATUS: ✅ COMPLETE — all 9 slices landed (S1–S9), suite 869 green.** See the commit list below.
> THE SCOPED PLAN is at the top; the original planning prompt follows it, kept verbatim as the handoff
> context. The plan was produced after verifying every lead in the prompt against the repo (plus
> parallel deep-dives on config-resolution and the kernel), and after a scope confirmation with the user.

## Landed (2026-07-07) — all on `main`, unpushed

- **S1** `c6a4239` — one deep-merge + one user-overlay loader; killed stale pwsh prose.
- **S2** `0ac6a54` — one llama-swap launch spec for foreground + background.
- **S3** `5ee2274` — one `service_control` core off a daemon table (folds the litellm/whisper/piper
  triples; generated tool defs + CLI handlers; fixed a latent provision.py STT-smoke bug).
- **S4** `8c74057` — actionable dashboard: start hint per down line, URL per up line.
- **S5** `f879314` — cockpit controls `/up`, `/restart`, `/webui`.
- **S6** `68caf59` — rich cockpit dashboard (`/services` + `/status`) off one `service_snapshot`.
- **S7** `b7f2703` — toggle services in place (`/services start|stop [name]`) + re-render feedback.
- **S8** `d46f316` — one `ensure_deps` seam (inference + stt) for command dependencies.
- **S9** `03656c2` — retired the dead Windows config fork (one resolve path, all OSes; bob.psd1 gone).

Deferred (unchanged): voice robustness (adaptive RMS + mic self-test) and onboarding-reach — both
box-dependent, neither a DRY issue. GPU/mic/TTY cockpit round-trips are best verified on the user's box.

## Scope confirmation (from the user)

- DRY: **no DRY violations left** — "whatever is better for the architecture."
- UX: **the TUI is the single cockpit** — "a dashboard which can show what services are active, and we
  can toggle inside. That way TUI will be the only cockpit, with visual feedback."
- Deferred (box-dependent, and not DRY issues): voice robustness (adaptive RMS + mic self-test) and
  onboarding-reach. Revisit on the user's box.

## What the investigation found ALREADY CLEAN (leads from the prompt that need no work)

- **Kernel ↔ stack.py** — the cold-start kernel *composes* `stack.py` (`stack.configure` +
  `stack.stack_up`); it re-implements no service-launch, pidfile, readiness-poll, `LLAMA_LOCAL_ROOT`, or
  port-in-use logic. Already the intended design. **No scope.**
- **Config resolution (runtime)** — `_deep_merge` is single-sourced (`bob_config`; `bob_models` imports
  it); `regenerate_configs` is single-sourced; there is **one** runtime-resolve path off-Windows.
  `bob.psd1` was already stripped to a single Windows-only key (`toastAppId`) — the persona/ports/roleTable
  duplication NB7 flagged is gone. `_ensure_configs` is the one config-gate for up/serve/restart.
- The genuine remainders (below) are smaller than the prompt implied — mostly a launch-spec, the
  per-service control triples, stale prose, and the UX cockpit.

## The plan — 9 slices, safe→risky, one commit each, gate green per commit

Gate: `tools/venv-litellm/bin/python scripts/check.py`. Every slice adds hermetic tests (mock
servers/ports/subprocess — never launch a real daemon). Ground rules from the prompt still hold
(stdlib-import-clean orchestration; OS branches via `osenv`; Co-Authored-By trailer).

### S1 — Kill stale/dead prose + tiny helper consolidation *(safe; docs + trivial code)*
- Fix comments that still claim PowerShell where the code is now one Python engine — these literally
  read as "split": `stack.py:9-12` ("regen still PowerShell until Slice 6"), `models.py:7-8` + `:197`
  (same), and the "edit `config/bob.psd1`/`user.psd1`" user-facing strings in `bob_mcp_server.py:57`,
  `bob_agent_server.py:404`, `tools/file.py:163`, `tools/schedule.py:301` → point at `config/user.json`
  (the live override). Also `bob_loop.py:6` docstring ("config read from data/config.json … Get-BobConfig").
- `scripts/bob/theme.py:58-62` has a third private `_deep_merge` copy → import the canonical one from
  `bob_config` (verify no import cycle: `bob_config` imports neither `theme` nor `bob`).
- One shared user-overlay loader: expose `bob_config.load_user_overlay()` (JSON + TOML, one error
  policy) and have BOTH `resolve_runtime_config` and `bob_models.load_models_config` call it (today
  `bob_models` reads `user.json` directly and ignores `user.toml`). One parse policy, TOML parity.

### S2 — Unify the llama-swap launch (foreground + background share ONE spec)
- `_start_endpoint_bg` (`stack.py:358-396`) and `serve_foreground` (`stack.py:563-590`) each build the
  llama-swap exe path, `config/llama-swap.yaml` path, `--listen 127.0.0.1:{port}` argv, and
  `LLAMA_LOCAL_ROOT` env independently. Extract `_swap_launch(config) -> (exe, argv, env, port)` as the
  single source; both callers consume it (bg passes `env` to `start_detached`; fg merges into
  `os.environ` for `subprocess.run`). Per-caller messaging + the exists/port-in-use guards stay (they
  differ: bg returns a tuple, fg prints + returns an int) — only the launch SPEC is unified.
- Test: both paths produce identical argv/env for a given config (mock `osenv.bin_exe` + `_port`).

### S3 — Generic `service_control` off the SERVICES registry *(the biggest remaining DRY win)*
- `litellm_control`/`whisper_control`/`piper_control` (`stack.py:503-527`), their 3 near-identical CLI
  handlers (`cli.py:733-745`), and 3 near-identical `TOOL_DEFS` entries are hand-repeated triples.
- Give the individually-controllable daemon entries in `SERVICES` a `start` callable (the existing
  `_start_*_bg`), a display `label`, and a `url_suffix`. Add ONE core:
  `service_control(config, name, action)` → stop/status/start off the registry. The 3 named controls +
  3 CLI verbs + 3 tool names are **kept** (the user wants the options) but become thin adapters over the
  one core; the 3 `TOOL_DEFS` and the 3 CLI handlers are **generated in a loop** over the daemon
  entries, so nothing is hand-repeated. No agent/CLI surface change. `_start_*_bg` bodies stay (they
  genuinely differ — piper needs env, whisper reaps stale, litellm skips the poll); only the wrappers
  collapse. Docker services (`services_control`, compose) stay separate.
- Test: `service_control(name=…, action=…)` routes correctly per registry entry; the generated
  `TOOL_DEFS`/handlers cover exactly the daemon set.

### S4 — Actionable status dashboard (one renderer feeds `bob status` AND `/status`)
- `_service_health_lines` (`stack.py:168-183`) shows `UP/down` but no URL and no "how to start" hint.
  Add a `start_hint` + URL per `SERVICES` entry (core→`bob up`, docker→`bob services start`,
  daemon→`bob <name> start`, webui→`bob webui`, agent→`bob agent serve`) and render, per down line, the
  hint; per line, `http://localhost:{port}`.
- This is the data layer the cockpit (S5-S7) renders. Test: down services surface their hint + URL.

### S5 — TUI cockpit, part 1: lifecycle slash-commands
- Add `/up`, `/restart`, `/webui` to the shell (thin wrappers over `stack.stack_up` /
  `stack_restart` / `webui_foreground`), register in `_SLASH` + `_SLASH_HELP`. After each, re-render the
  dashboard (S6) so the state change is visible. (`/stop`, `/logs`, `/status` already exist.)
- Test (`test_bob_shell`): `dispatch("/up")` calls `stack.stack_up` (mocked), etc.

### S6 — TUI cockpit, part 2: the dashboard view *(the centerpiece — "the only cockpit, with visual feedback")*
- Render the S4 registry data as a rich Table: group → service, **coloured** UP(green)/down(red), port,
  URL, hint. Reuse it for `/status` and a dedicated `/services` (no-arg) view. Stays inline (a scrolling
  transcript block) — NOT an alternate-buffer full-screen TUI (the `shell.py` docstring forbids that: it
  breaks native scrollback). "Visual feedback" = the dashboard is re-printed after every toggle with the
  changed row now green/red.
- Test: the table renders the expected rows/state from a mocked registry snapshot.

### S7 — TUI cockpit, part 3: toggle inside
- `/services [start|stop] [name]` routes by the registry: daemon (`whisper`/`piper`/`litellm`) →
  `service_control` (S3); docker (`searxng`/`n8n`/`langfuse`, or bare `/services start`) →
  `services_control`; `open-webui` → up/stop; core → `ensure_inference`/`stop`. After the action,
  re-render the dashboard (S6) — the toggle + immediate visual confirmation is the cockpit loop.
- Test: `dispatch("/services start whisper")` → `service_control("whisper","start")`;
  `dispatch("/services stop")` → docker `services_control("stop")`; each re-renders.

### S8 — Unified `ensure_deps` seam + auto-start feedback
- One `ensure_deps(config, inference=True, stt=False, tts=False) -> (ok, lines)` in `stack.py` composes
  `ensure_inference` + whisper/piper starts. `cli._ensure_endpoint` becomes a thin printer over
  `ensure_deps(inference=True)`; the shell's `/voice` whisper preflight (`shell.py:788-799`) becomes
  `ensure_deps(stt=True)` — so "ensure the deps this command needs" lives in one place, not split across
  cli + shell.
- Feedback: in the shell (rich, main thread) wrap the ensure call in a `console.status("loading the
  model…")` spinner so auto-start isn't a silent wait; the plain-CLI path keeps an improved one-liner
  (a spinner there needs threading around the blocking poll — not worth it).
- Test: `ensure_deps` composition (mock the per-service starts); the shell shows a status during a
  not-yet-ready start.

### S9 — Retire the dead Windows config fork *(architecture; last; own commit; verify-then-remove)*
- Verified: `toastAppId` has **zero** live Python consumers, and the PowerShell `Get-BobConfig` writer
  no longer exists — so nothing writes `data/config.json`, and the Windows branch in
  `bob_core.load_config:100` only fires on a *stale* pre-migration file. It's a dead fork, and a config
  path that "forks by OS" is exactly the "feels split" smell.
- Collapse `load_config` to always `resolve_runtime_config()` (all OSes); drop the `data/config.json`
  read branch; simplify the `health.py` Windows-only `data/config.json` checks; retire `config/bob.psd1`
  (if a Windows toast ever needs `toastAppId`, it belongs in `defaults.json.runtime`/`osenv`, not a
  dead psd1). Do this LAST so a revert is trivial if anything Windows-adjacent surprises us.
- Test: `load_config` resolves identically regardless of `is_windows()` with no `data/config.json`.

## Sequencing & housekeeping
- Order = S1 … S9 (safe→risky). Each is an independent commit with hermetic tests and a green gate.
- **Deferred (not in this pass):** voice robustness (adaptive RMS + `bob listen` mic self-test) and
  onboarding-reach — both are best verified on the user's box and neither is a DRY violation.
- **`main` is 8 commits ahead of `origin/main` and unpushed** (the prompt's "~28" is stale — verified
  with `git log origin/main..HEAD`). Pushing is the user's call.
- GPU inference, the mic, and the interactive TTY are best verified on the user's box, not the sandbox.

---

# (Original) POST-ONE-2 — Lifecycle DRY + UX coherence: planning prompt

**Read this first, then investigate before you touch code.** This is a cold-start handoff — the prior
session ran long. Everything you need is below; **verify every claim against the repo** (files and line
numbers move). The gate is `tools/venv-litellm/bin/python scripts/check.py`; keep it green; commit per
fix; don't break Windows.

## Where things stand (ground truth)

- **Module ONE is complete** (PowerShell fully retired; one Python engine). The POST-ONE follow-up
  (entry model, docs, memory) is also complete. This prompt is the NEXT pass: **finish the lifecycle
  DRY cleanup and hunt for more DRY + UX scope.**
- **The gate:** `tools/venv-litellm/bin/python scripts/check.py` (py_compile + versions.lock sync +
  exec-bits + the unittest suite, ~842 tests). A local `versions.lock STALE` line is a KNOWN
  false-positive on a machine with a gitignored model manifest — it is green in CI. Don't chase it.
- **Tests** are stdlib `unittest` in `tests/`, run from that dir (they import `_common`). Everything is
  hermetic — mock servers/ports/subprocess; never launch real daemons in a test.
- **Environment reality:** this box is already provisioned and the inference stack is usually running
  (llama-swap :8080, litellm :8081, whisper :8082). Treat GPU inference + interactive TTY (the shell,
  `/voice`, mic, audio) as things to reproduce carefully, not assume. The shell is zsh/fish-flavoured —
  the Bash tool does NOT word-split unquoted vars and `pkill -f <str>` can self-match a command line
  that literally contains `<str>`.
- **`main` is ~28 commits ahead of the remote and NOT pushed.** Push is the user's call — remind them.

## The problem statement

The user's recurring complaint: **"the harness feels broken and split."** When pinned down, it was NOT
about the number of `bob` verbs (they explicitly want to keep options). It is two things:

1. **DRY violations in the service-lifecycle layer** — *"what is enabling what in different places."*
   "Start inference" (and start/stop/check each service) was implemented and triggered in several
   scattered spots, so nothing was the single source of truth for what-starts-what, and you couldn't
   cleanly see what's active or what to toggle. Concrete examples the user gave: `bob` and `bob up` both
   start inference; services/webui/litellm lifetimes were managed ad hoc.
2. **UX incoherence / poor visibility** — the assistant, the ops verbs, and the background Docker
   services felt like separate tools; the user *"didn't even know if SearXNG or n8n were working."* Plus
   voice was broken.

## Fixes already done (this session — verify against git log)

Entry model + auto-start:
- `cli._ensure_endpoint` used to read LiteLLM's 401-when-up as "down" and relaunch on every call, and
  didn't wait for the proxy to finish booting. Now a TCP-connect readiness check + a real wait (32612fd).
- TUI is the home base: `/help` shows the shell's slash-commands (not the CLI catalog); added `/stop`
  and `/logs` (32612fd).

Memory ("Bob doesn't know me") — 4 root causes, all fixed:
- Onboarding was skipped whenever `config/user.json` had a `bob` key even if the profile never seeded;
  now `_needs_onboard` keys on an actual profile row (53227d3).
- A stale PowerShell-era `data/config.json` shadowed every `defaults.json` change off Windows;
  `load_config` now ignores it on non-Windows (53227d3).
- Persona now instructs Bob to persist durable identity via `memory_store` (53227d3).
- **The real FTUE bug:** onboarding runs before inference is up, so the profile-save hit the embed
  server and silently failed. `store(..., embed_optional=True)` now persists a profile with an empty
  embedding (profile injection is a SQL read; no vector needed) (764bead).

Docs: all user-facing docs rewritten cross-OS / TUI-first / verbs-vs-tools (c32557f); positive framing
+ macOS dropped as a target (a4ab76d); command-table fix (5436ec8); README banner (a28d812).

Lifecycle + services (the DRY pass + visibility):
- `bob webui` no longer crashes when WebUI is already up — points at the running instance (47fab11).
- `bob stop` now reaps Open WebUI by process name, not just pidfile (539fa9b).
- `/voice` auto-starts whisper (was "go run bob whisper"); `bob voice` auto-starts inference (1189ce7).
- `osenv.record_audio` no longer hangs forever when the mic hears nothing — no-speech timeout +
  max-duration; the shell nudges "check your mic" on an empty capture (1309d18).
- **`bob status` / TUI `/status` is now a one-glance system dashboard** — every service (inference,
  voice, web+automation: WebUI/SearXNG/n8n/Langfuse, agent-api), shown even when inference is down
  (7b35859). This revealed the user's Docker services were simply not running.
- **`ensure_inference(config)`** is now the ONE "make core inference reachable" op; auto-start, `bob up`,
  `bob restart` all compose it (core-only — no more `bob chat` silently starting WebUI/whisper) (7bd171c).
- **`SERVICES` registry** in `scripts/tools/stack.py` is the single source of truth; `_NAME_KILL`,
  `_PS_SERVICES`, the health dashboard, `stack_stop`, and `stack_restart` all derive from it — the
  service list used to be written in 6 places (03a6f80, 1addf18).

See memory `lifecycle-single-source.md` for the resulting structure.

## Your mission — dig deeper and find MORE scope (DRY + UX)

The above closed the obvious lifecycle DRY + the worst UX gaps. The user wants you to **keep hunting**:
find the remaining DRY violations and UX rough edges, propose them, confirm scope with the user, then
fix per-commit with hermetic tests. Reproduce before fixing. Do NOT do a speculative big-bang refactor —
propose and confirm first, especially anything that changes the CLI contract or config shape.

### DRY leads to investigate (verify each; some may already be fine)
- **`serve_foreground` vs `_start_endpoint_bg`** — the foreground (`bob serve`) path re-implements the
  llama-swap launch (exe check, port-in-use, config path, `LLAMA_LOCAL_ROOT` env, listen addr) separately
  from the background core-start. Can a single launch-spec feed both fg and bg?
- **The per-service `*_control` functions** (`litellm_control`, `whisper_control`, `piper_control`) are
  near-identical start/stop/status shells. Could a generic `service_control(name, action)` be driven off
  the `SERVICES` registry (each entry carrying its start fn / stop mode) instead of one hand-written
  function each? Same for `_start_litellm_bg` / `_start_whisper_bg` / `_start_piper_bg` — they share the
  venv/bin check → pidfile → `start_detached` → readiness-poll shape.
- **Config resolution duplication** — `bob_config.resolve_runtime_config` and `bob_models.load_models_config`
  both deep-merge `config/user.json` independently. And `config/bob.psd1` still duplicates persona / ports
  / role table that live in `config/defaults.json` (NB1/NB7 flagged this). Is there a single resolve path?
- **`_ensure_configs` / `_regen_configs`** are invoked from `stack_up` / `serve_foreground` / `stack_restart`
  — confirm that's one path, not three subtly different ones.
- **Auto-start invocation sites** — `_ensure_endpoint` is called from `_chat`, `_handle_agent_run`,
  `_handle_shell`, `_handle_voice`. Fine, but is there a cleaner single "ensure the deps this command
  needs" seam (e.g. inference vs inference+STT vs inference+TTS)?
- **The kernel (`scripts/bob/kernel.py`) vs `stack.py`** — does cold-start bring-up duplicate any of the
  per-service start logic that now lives in `stack.py`?

### UX leads to investigate
- **TUI as cockpit** (the user was interested): drive `/services start|stop`, `/up`, `/webui` from inside
  the shell so you never drop to raw verbs to manage the system. The status dashboard already shows state;
  add the controls.
- **`bob status` actionability** — it shows services `down` but not how to start them. Add a per-line hint
  (e.g. "down — start: bob services start" / "bob up --with-services") and maybe the URL.
- **Auto-start feedback** — currently a one-line print then a silent wait, and the first turn can cold-load
  the model slowly with no signal. Consider a spinner / "loading model…" so it doesn't feel hung.
- **Onboarding reach** — onboarding only runs at the end of `setup`. Should a fresh interactive `bob` offer
  to seed a profile when none exists (rather than only via setup)? Verify E2E that a seeded profile shows
  up in the very next `bob chat "what's my name?"`.
- **Voice robustness** — is the RMS silence threshold (`osenv.record_audio` rms_silence=200) right across
  mics? Consider making it configurable / adaptive, and a `bob listen`-based mic self-test. Confirm the
  end-to-end `/voice` round trip on the user's box (mic → STT → loop → piper → audio out).
- **Discoverability of the "keep it running" story** — `serve` (fg) vs `up` (bg) vs auto-start: is it clear
  in the moment which is active?

### Method
Reproduce → grep for the duplication/rough edge → map it → propose to the user (get scope confirmation
for anything that changes contracts) → fix in small commits, each with hermetic tests and the gate green.

## Ground rules
- Orchestration/kernel-path code (`stack.py`, `cli.py`, `kernel.py`, `provision.py`) must stay
  **stdlib-import-clean** — no `requests`/`openai` at import; health/readiness use `urllib`/sockets
  (`bob_core.check_litellm`, `osenv.is_port_in_use`).
- Keep `scripts/check.py` green; add hermetic tests (mock servers/ports/subprocess; no real launches).
- **Don't break Windows** — every OS-specific branch goes through `scripts/osenv.py`.
- Commit per fix with a clear message; end messages with the required Co-Authored-By trailer.
- **Remind the user to `git push`** (main is ~28 commits ahead, unpushed) and that GPU/mic/TTY behaviour
  is best verified on their box, not the sandbox.

## Key files
`scripts/tools/stack.py` (SERVICES registry, ensure_inference, stack_up/stop/restart/status, the
per-service controls) · `scripts/bob/cli.py` (`_ensure_endpoint`, the handlers) · `scripts/bob/shell.py`
(the TUI: `/status`, `/voice`, `/stop`, `/logs`) · `scripts/osenv.py` (record_audio, is_port_in_use,
process seams, play_audio) · `scripts/bob_config.py` + `scripts/bob_models.py` + `config/defaults.json`
+ `config/bob.psd1` (config resolution) · `scripts/bob/kernel.py` (cold-start) · `scripts/bob_memory.py`
+ `scripts/bob_core.py` (memory/profile) · `docs/*.md` · `tests/` (esp. `test_slice2_stack.py`,
`test_entry_model.py`, `test_osenv.py`, `test_bob_shell.py`, `test_memory_profile.py`).
