# POST-ONE-2 — Lifecycle DRY + UX coherence: planning prompt

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
