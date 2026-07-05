# POST-ONE — Entry model, docs, and memory: planning prompt

**Read this first, then PLAN before you touch code.** This is a cold-start handoff: the prior session ran
out of context. Everything you need is below; verify claims against the repo (files move).

## Where things stand (ground truth)

- **Module ONE is COMPLETE**: all PowerShell is retired. Entry = `./install_prereqs.sh` + `./setup.sh`
  (Windows: `install_prereqs.bat`/`setup.bat`) → thin shell stubs → `python -m bob.kernel`. The CLI is
  Python-only (`scripts/bob/` package; the `./bob` POSIX shim + a `bob.cmd` on Windows both run
  `python -m bob`). `git ls-files '*.ps1'` returns only the sample `plugins/play/invoke.ps1`.
- **A real fresh install works** on CachyOS + Blackwell (native CUDA build, kernel, all 12 setup steps).
- **Local `main` is ~9 commits ahead of the remote and NOT pushed.** Push is pending the user's call.
- **The gate is `python scripts/check.py`** (py_compile + versions.lock sync + exec-bits + full unittest
  suite; run it via `tools/venv-litellm/bin/python scripts/check.py`). The suite is ~816 tests. A local
  `versions.lock STALE` line is a known false-positive on a machine with a gitignored model manifest —
  it is green in CI. Commit per logical fix; keep the suite green.
- **Environment reality:** this box is already provisioned; you can run the CLI/tests but treat GPU
  inference + interactive TTY behavior as things to reproduce carefully, not assume. The shell is
  zsh/fish-flavored — the Bash tool does NOT word-split unquoted vars and globs can error; use explicit
  loops/arrays.

## The mission — do these THREE, IN ORDER

Each is its own plan → implement → `check.py` green → commit. Do not batch them into one mega-change.

---

### 1. Define and fix the Bob entry model (terminal + outside-terminal)

**The complaint:** the entry surface is confusing. `bob up`, `bob stop`, `bob chat`, `bob agent`
(planner), and bare `bob` all "work" but there's no clear mental model of *the* way to use Bob. And
critically — **inference did NOT start automatically** even though the prior session wired an
auto-start (`cli._ensure_endpoint`). So either the auto-start doesn't fire, or it fires but doesn't
actually make inference usable.

**What exists now (verify):**
- `scripts/bob/cli.py`: `main()` → `_resolve()` → registry dispatch. `_ensure_endpoint(config)` probes
  the LiteLLM proxy (`litellmPort`, what the loop talks to) via stdlib urllib and, if down, calls
  `stack.stack_up(config, open_browser=False)`. It's wired into `_chat` (chat/code/think + interactive),
  `_handle_agent_run`, and `_handle_shell` (gated on `is_interactive`).
- `scripts/tools/stack.py`: `stack_up`, `stack_stop`, `serve_foreground`, `litellm/whisper/piper_control`,
  `services_control` (docker compose: langfuse/searxng/n8n). Health probes are stdlib urllib now.
- Lifecycle verbs in `scripts/bob/registry.py`: `up serve restart stop status ps logs services webui …`.

**Investigate FIRST (reproduce, don't guess):**
- Run `bob` (bare, on a TTY) and `bob chat "hi"` with nothing running. Does `Starting local inference…`
  print? Does `stack_up` actually bring llama-swap (`:8080`) + litellm (`:8081`) up and load a model?
  Watch `logs/llama-swap.log`, `logs/litellm.log`. Likely failure modes to check: (a) `_ensure_endpoint`
  probes `litellmPort` but the loop actually calls a *different* base — confirm what `bob_core.client()`
  targets vs what stack starts; (b) `stack_up` returns an error string that's swallowed; (c) llama-swap
  lazy-loads the model on first request so `/v1/models` answers "up" while the first real turn still
  cold-loads (slow, looks broken); (d) the probe raced ahead / the interactive shell path didn't call it.
- Map every entry verb and decide the CANONICAL model. Suggested direction (confirm with the user's
  intent, already leaning "one word"):
  - **`bob`** (bare, TTY) = the assistant REPL; **`bob "text"` / `bob chat`** = talk; **`bob agent
    <goal>`** = agentic task. All auto-ensure inference. This is the human entry.
  - **`bob up` / `bob stop`** = manage the *persistent* stack for **outside-terminal** consumers
    (n8n, Open WebUI `:3000`, Continue.dev/aider, external OpenAI-compatible clients on `:8081`, the
    agent HTTP server `:8084` via `bob agent serve`). `up` = "keep services running for other tools";
    the interactive path just borrows/starts them on demand.
  - Reconcile overlap: `serve` (foreground) vs `up` (background) vs auto-start — document the difference
    or collapse. Decide whether redundant verbs get hidden from `help` (registry supports `hidden`).

**Deliverable:** a working, predictable entry model where (a) a human runs `bob`/`bob chat`/`bob agent`
and inference just works, and (b) "outside terminal" use (n8n, WebUI, API, agent server, editors) has a
clear "keep it running" story. Add tests that don't launch real servers (mock `_ensure_endpoint` /
`stack_up`, as `test_chat_mode` already does).

---

### 2. Rewrite ALL main docs — correct, complete, and NOT Windows-broken

**The complaint + a real regression:** the prior session committed a docs refresh (README, SETUP,
CHANGELOG) that made **bash the primary** and, in doing so, risks leaving **Windows users with
copy-paste commands that don't run** (bash blocks on a cmd/PowerShell shell). Docs must serve **both
OSes first-class** — separate, correct, copy-pasteable blocks per OS — with **no omission**.

**What's done vs pending:**
- DONE (front door): `README.md`, `docs/SETUP.md`, `CHANGELOG.md` — but re-audit them for the
  Windows-parity bug (every Windows user needs a block that actually works in `cmd`/PowerShell, e.g.
  `install_prereqs.bat` / `setup.bat`, not a bash line with a `# Windows:` comment).
- STALE (still pwsh/`.psd1`/`verbs.json`/`setup.bat -Flag`): `docs/MANUAL-INSTALL.md` (near-total
  rewrite — it's the old scoop/winget/pwsh guide), `docs/PORTABILITY.md` (describes the removed pwsh
  provisioner + `verbs.json`), `docs/USAGE.md` (~40 hits), `docs/TUNING.md`, `docs/DAY-IN-THE-LIFE.md`,
  `docs/FALLBACKS.md`, `docs/AGENT-SERVER.md`, `CONTRIBUTING.md`.

**Ground truth for the rewrite (verify against the repo):**
- Entry: `./install_prereqs.sh` + `./setup.sh` (Linux/macOS-ish bash) · `install_prereqs.bat` +
  `setup.bat` (Windows). Both → `python -m bob.kernel`. Flags are lowercase double-dash:
  `--skip-models --skip-voice --profile <p> --cpu --with-webui --launch`.
- Config is JSON now: `config/models.json`, `config/user.json`, `config/defaults.json` (and
  `config/bob.psd1` is a Windows *authoring* source that still exists). There is **no `verbs.json`**,
  no `runtime` field, no `scripts/*.ps1`.
- Package managers: apt/dnf/pacman/zypper **+ rpm-ostree** (atomic Fedora / Bazzite — recommend a Fedora
  distrobox). One batched `sudo` prompt. `setup` needs no root.
- Runtime: `bob` auto-starts inference; `bob up`/`stop`/`services`; ports llama-swap `:8080`, litellm
  `:8081`, WebUI `:3000`, agent server `:8084`, langfuse `:3001`, searxng `:8888`, n8n `:5678`.
- There's a precise line-by-line stale inventory the prior session generated — regenerate it fresh:
  `git grep -nE '\.ps1|setup\.bat|install_prereqs\.bat|pwsh|PowerShell|-SkipModels|-Profile |scoop|verbs\.json|\.psd1' README.md docs/ CONTRIBUTING.md`.

**Deliverable:** every main doc accurate for the current Python-only, cross-OS, auto-start reality;
Windows and Linux both have working copy-paste paths; the feature/command tables match the real
registry (`scripts/bob/registry.py`) — e.g. don't list `bob summarise`/`bob draft`/`bob search` as
verbs if they're actually agent *tools*/plugins. Prefer per-OS tabs/blocks over a single ambiguous one.

---

### 3. Fix memory + system prompt (Bob "doesn't know me")

**The complaint:** after Bob asks the user's name and the user answers, a later turn says "I don't know
you." So the identity/profile is not being persisted and/or not injected into the system prompt.

**What exists (verify):**
- `scripts/bob_memory.py`: `cmd_init_profile(name, work, db_path)` seeds `type='profile'` rows;
  `profile_block(owner, db_path, …)` renders the durable-identity block; `store()`/`recall()`;
  `_require_deps()` (needs `sqlite-utils` + `requests` — venv-litellm only).
- Onboarding: `scripts/bob/kernel.py` `onboard()` (+ `_needs_onboard()`) — now shells the profile save
  through venv-litellm python. **But onboarding is SKIPPED when `config/user.json` already has a `bob`
  section** — so on many machines the profile was never seeded. Confirm whether it actually ran/saved.
- The loop: `scripts/bob_loop.py` assembles the system prompt and (per `docs/MEMORY.md`) is supposed to
  inject a profile/memory context frame (autoRecall, `MEMORY_CONTEXT_FRAME`, consolidation). `bob_core.py`
  has `profile_block`/memory-context helpers and `memory.*` config keys.

**Investigate FIRST:**
- Does the system prompt actually include the profile block? Trace `bob_loop` system-prompt assembly →
  does it call `bob_memory.profile_block(...)` / inject recalled memories? Is `memory.autoRecall` /
  `memory.injectProfile` (or whatever the keys are — read `docs/MEMORY.md` + `config/defaults.json`)
  enabled by default?
- When the user says "my name is X" in a turn, is it *stored*? Check whether the loop has a
  store-on-mention path or relies on explicit `bob remember`. If Bob asks the name conversationally, the
  answer must be captured (either the loop stores it, or onboarding must run and seed it).
- Owner scoping: `profile_block(owner=…)` — is the interactive session's owner the same key the profile
  was stored under? A mismatch (e.g. "local" vs a username) would make recall miss.
- DB path: `data/bob.db` — confirm onboarding wrote there and the loop reads the same path
  (`memory.dbPath` in config).

**Deliverable:** after onboarding (or after telling Bob your name once), a *new* session's system prompt
carries the identity, and Bob answers "what's my name" correctly. Add a test that seeds a profile and
asserts the assembled system prompt / `profile_block` contains it. Fix the onboarding-skip logic if the
profile was never seeded (e.g. seed even when a `bob` section exists but no profile rows do).

---

## Ground rules
- Plan each item before coding; reproduce the bug before fixing it.
- Orchestration/kernel code must stay **stdlib-import-clean** (no `requests`/`openai` at import in
  `stack.py`/`provision.py`/`kernel.py`/`cli.py` paths that run under the bare kernel python) — the last
  three bugs were exactly venv-only imports on the kernel path. Health polls use `urllib` (see
  `stack._http_json`/`_http_ok`, `smoke.py`, `check.py`).
- Keep `python scripts/check.py` green; add hermetic tests (mock servers/endpoints, no real launches).
- **Do not break Windows** when editing docs or entry code — every OS needs a working path.
- Commit per fix with a clear message; end messages with the required Co-Authored-By trailer.
- Remind the user to `git push` (main is ahead) and that GPU/interactive behavior is best verified on
  their box, not in the sandbox.

## Key files
`scripts/bob/cli.py` · `scripts/bob/registry.py` · `scripts/bob/kernel.py` · `scripts/tools/stack.py` ·
`scripts/tools/provision.py` · `scripts/bob_loop.py` · `scripts/bob_core.py` · `scripts/bob_memory.py` ·
`config/defaults.json` · `config/models.json` · `docs/MEMORY.md` · `README.md` · `docs/*.md` ·
`scripts/check.py` · `the ./bob shim`.
