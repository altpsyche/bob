# POST-ONE-3 — Consistency + tech-debt cleanup: continuation prompt

**Read this first, then verify every claim against the repo before you touch code** (files and line
numbers move). This is a cold-start handoff written at the end of the POST-ONE-2 session. The gate is
`tools/venv-litellm/bin/python scripts/check.py`; keep it green; commit per fix; don't break Windows.

## Where things stand (ground truth)

- **POST-ONE-2 is COMPLETE** (lifecycle DRY + the TUI cockpit; see
  `docs/improvements/POST-ONE-2-lifecycle-dry-ux-plan.md`). A run of live testing after it produced a
  batch of reactive fixes (Ctrl-C crash, unclosed `</tool_call>`, `os.startfile`, voice phases,
  recording silence, emojis/dashes, on-demand SearXNG, docker-free YouTube). This prompt cleans up the
  **consistency gaps and tech debt those introduced or exposed** — mostly root-causing symptoms that
  were patched, and generalizing one bespoke helper.
- **The gate:** `tools/venv-litellm/bin/python scripts/check.py`. It exits 1 LOCALLY only on the known
  `versions.lock STALE` false-positive (green in CI) — gate on the unittest "Ran N … OK" line instead
  (`cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'`). Suite is
  ~883 tests, all green.
- **Tests** are stdlib `unittest` in `tests/`, run from that dir (they import `_common`, which puts
  `scripts/` + `scripts/tools/` on `sys.path`). Everything is hermetic — mock servers/ports/subprocess;
  never launch a real daemon, container, or hit the network in a test.
- **Environment reality:** this box has **NO Docker** (no `docker`, no `podman`). So SearXNG (a
  compose service) cannot start here, and anything that depends on it (`web_search`, the SearXNG path of
  `music_play`) can only be exercised on a box with Docker. GPU inference + interactive TTY (shell,
  `/voice`, mic, audio) are best verified on the user's box, not the sandbox.
- **UI style rule (the user is firm on this):** NO emojis and NO em-dashes (`—`) in user-facing output
  (shell / voice / dashboard / status / tool result strings). Use plain punctuation. Functional theme
  glyphs (`● ○ ✓ ✗`) and middle-dot separators (`·`) are fine. See memory `no-emojis-no-dashes`.
- **`main` was pushed mid-session.** Confirm with `git log origin/main..HEAD` before assuming;
  remind the user to push when you finish.

## The issues to fix (ranked; verify each, then propose scope before large changes)

### 1. Generalize `ensure_searxng` → `ensure_service(config, name)`  [consistency — the main ask]
- **What:** the cockpit `/services start|stop <name>` is already GENERIC and registry-driven — it works
  for searxng/n8n/langfuse identically via `services_control(config, action, service=<name>)`
  (`scripts/bob/shell.py` `_toggle_service`, ~L635-653). Good, leave it.
  The ONE bespoke, searxng-hardcoded piece is the on-demand auto-start `ensure_searxng`
  (`scripts/tools/stack.py:645`) — it inlines `"searxng"` + `searxngPort` and `up -d searxng`. If a
  future tool ever needed on-demand n8n, you'd duplicate the whole function.
- **Fix:** add a generic `ensure_service(config, name) -> (ok, msg)` that starts a single docker
  container (from the `SERVICES` registry entry, which already carries `port`) and polls that entry's
  port. Make `ensure_searxng` a thin alias (`return ensure_service(config, "searxng")`) or drop it and
  update callers. `ensure_deps(config, search=True)` (`stack.py:451`) and the tool auto-starts
  (`scripts/tools/web.py` `_ensure_searxng`, `plugins/play/tool.py`) call `ensure_service(cfg, "searxng")`.
  Net: "auto-start any docker service on demand" becomes a registry property, not hand-written code.
- **Verify:** searxng behavior byte-identical; a second docker service (n8n) works through the same path
  with no new code. Existing `TestSearxngOnDemand` tests still pass (extend to assert genericity).

### 2. Root-cause the `memory_recall` spin (the loop guard is a band-aid)  [correctness]
- **What:** with `memory.autoRecall` on, a ROOT run injects recalled memory into the system prompt
  (`scripts/bob_loop.py` ~L1197) AND still offers the `memory_recall` TOOL
  (`scripts/tools/memory.py`). Small models double-dip and re-call `memory_recall(query=user_context)`
  endlessly. POST-ONE-2 added `agent.maxDuplicateToolCalls` (`bob_loop.py:508`) which BOUNDS the spin
  but does not remove the cause.
- **Fix (propose, confirm):** when `autoRecall` is on AND `agent_depth == 0`, drop `memory_recall` from
  the offered toolset for that run (the memory is already injected, so the tool is redundant that turn);
  OR have the tool return a clear "memory for this turn was already injected" note that the model treats
  as terminal. KEEP the duplicate-call guard as defense-in-depth (don't remove it). Consider whether a
  sub-agent (depth>0) should keep the tool (it has no autoRecall — yes, keep it there).
- **Verify:** on the real small model (user's box), `/voice` + chat no longer loop; a run WITHOUT
  autoRecall still has the tool. Add a hermetic test: autoRecall on → `memory_recall` not in the
  schemas passed to the model.

### 3. YouTube resolver fragility  [maintenance debt — decide scope with user]
- **What:** `plugins/play/tool.py:73` `_youtube_first_video` scrapes `"videoId":"…"` from YouTube's
  HTML. It works today but is brittle — a markup change breaks it silently, and the test mocks the HTML
  so there's no drift signal.
- **Options (pick with the user):** (a) prefer a more stable resolver when available — `yt-dlp`
  (staged via `osenv.bin_exe`) or the YouTube oEmbed endpoint to validate a candidate; (b) keep the
  scrape but add a doctor/self-test canary (opt-in, network) that flags when a known query yields no
  id; (c) accept it as best-effort and just document the fragility (the search-page fallback already
  exists). At minimum, leave a comment noting the HTML dependency.

### 4. `bob_vision` sidesteps the osenv seam  [consistency — small]
- **What:** `scripts/bob_vision.py:57` branches on `sys.platform in ("win32","darwin")` directly. It's
  cross-platform and correct, but it's the one door that bypasses "OS-specific behavior goes through
  `scripts/osenv.py`" (CLAUDE.md NB3).
- **Fix:** move the capture into `osenv` (e.g. `osenv.capture_screen(out_path)`), or at least branch on
  `osenv.os_name()` so `BOB_FORCE_OS` drives it in tests like every other seam. Keep the PIL /
  grim-spectacle-scrot-import backends.

### 5. Docker-absent UX  [propose scope]
- **What:** `web_search` needs SearXNG needs Docker, which isn't installed on the user's box.
  `ensure_searxng`/`_ensure_searxng` now surface the real "Docker not found" reason (good), but nothing
  proactively signals it and there's no non-docker web-search fallback.
- **Options:** (a) cheap — `bob status` / `bob doctor` show SearXNG as "unavailable (needs Docker, not
  installed)" with an install hint for the OS; (b) feature — a non-docker `web_search` fallback (a
  direct provider, e.g. the DuckDuckGo HTML endpoint) so search degrades gracefully instead of failing;
  (c) an optional docker-install helper through `osenv`/kernel. (a) is a quick win; (b)/(c) need a scope
  call.

### 6. Deferred POST-ONE-2 items (still open)
- **Voice robustness:** the `rmsSilence` floor is now configurable and silence is peak-relative
  (`scripts/osenv.py` `record_audio`), so this is partly done. Remaining: a `bob listen`-based mic
  self-test / calibration command so a user can check their input device + tune the threshold.
- **Onboarding reach:** offer to seed a profile on a fresh interactive `bob` (not only via `setup`);
  verify E2E that a seeded profile shows up in the very next `bob chat "what's my name?"`.

### Also do a quick sweep
- Re-scan this session's reactive commits (`git log 392da63..HEAD`) for any other one-off / special-cased
  code that should be generalized, and confirm no NEW user-facing string reintroduced an emoji or em-dash.

## Method
Reproduce → find the ROOT cause (not the symptom) → for anything that changes a contract, config shape,
or adds a feature, PROPOSE and get the user's scope confirmation first → fix in small per-commit changes,
each with hermetic tests and the gate green. Prefer generalizing over special-casing; prefer deleting a
bespoke path over adding a parallel one.

## Ground rules
- Keep `scripts/check.py`'s unittest suite green; add hermetic tests (mock servers/ports/subprocess/
  network — never launch real daemons, containers, or fetch live).
- No emojis, no em-dashes in user-facing output (memory `no-emojis-no-dashes`).
- OS-specific behavior goes through `scripts/osenv.py`; don't break Windows.
- Orchestration/kernel-path code (`stack.py`, `cli.py`, `kernel.py`, `provision.py`) stays
  stdlib-import-clean (no `requests`/`openai` at import; health via `urllib`/sockets).
- Commit per fix with a clear message ending in the required Co-Authored-By trailer. Remind the user to
  `git push` when done.

## Key files
`scripts/tools/stack.py` (ensure_searxng/ensure_deps/services_control/SERVICES) · `scripts/bob/shell.py`
(`_toggle_service`, cockpit) · `scripts/bob_loop.py` (autoRecall inject ~L1197, dup-guard ~L508,
tool-schema assembly) · `scripts/tools/memory.py` (memory_recall tool) · `plugins/play/tool.py`
(YouTube resolvers) · `scripts/bob_vision.py` (capture_screen) · `scripts/osenv.py` (record_audio, the
OS seam) · `scripts/tools/web.py` (web_search + `_ensure_searxng`) · `tests/` (esp. `test_slice2_stack`,
`test_agent_loop`, `test_play`, `test_bob_shell`, `test_osenv`).
