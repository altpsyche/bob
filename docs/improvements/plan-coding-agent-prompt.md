# Plan Module Q (coding-agent core) — cold-start planning handoff

**Paste this into a fresh Claude Code session at the repo root (`/home/siva/dev/bob`).** Your job is to
produce an **implementation plan** for Module Q — the things that make Bob good at *coding* specifically.
Plan first; do not write feature code until the plan is signed off. Verify every claim below against the
repo before planning (line numbers and counts drift).

## What you're planning

Module Q = coding-agent core. Backlog exists and was refreshed against the code:

- **Read first:** `docs/improvements/MODULE-Q-coding-agent-core.md` — Q1 repo map / symbol index
  (code-aware retrieval), Q2 structured edit formats (search/replace, unified diff, patch), Q3 lint +
  run-tests-and-fix loop, Q4 per-step checkpoint + rewind, Q5 edit safety/control (diff-preview, loop
  hooks, mid-run steering, typed sub-agents). All five are unimplemented.
- **Code you'll build on:** `scripts/tools/file.py` (`file_read`/`file_list`/`file_write` — whole-file
  only today, ~159-176; secrets denylist), `scripts/tools/git.py` (read-only `git_status`/`log`/`diff`),
  `scripts/tools/shell.py` (`shell_run`, single-shot, ≤4000 chars, sandbox-aware), `scripts/sandbox.py`,
  `scripts/bob_permissions.py` (allow|ask|deny policy), `scripts/bob_loop.py` (self-repair retry
  ~424-431 gated by `_self_repair_on` ~660-664; the parse-error correction path
  `scripts/tools/tool_registry.py` ~257-260 injected at `bob_loop.py` ~184-200; the plan/verify critic
  ~610-621/1524-1533), `scripts/tools/spawn_agent.py` (sub-agents; `role` is a model-role override
  only), `scripts/bob_session.py` (SQLite session store), and `scripts/bob_memory.py`
  (`store(..., context=)` contextual-chunk seam + hybrid dense+BM25/RRF recall + optional cross-encoder
  rerank — all shipped in Module R).
- **Config:** feature flags live under `config/defaults.json` -> `runtime.agent.*`, deep-merged with
  `config/user.json`, default-off, read with `.get(default)`.

## Current state to respect (recent work — do not regress it)

- **One Python engine, zero PowerShell**; config resolves from `defaults.json` + `user.json` on every OS.
- **Fully decoupled from development-phase markers** (`O#/N#/MEM-#/NE#/NB#/ONE-*/slice`) — none in code,
  tests, comments, or docs; test files are domain-named.
- **Modules O, R, and the UX overhaul all shipped.** The generic-agent substrate Q builds on is present:
  sub-agents, parallel tools, compaction, sandbox, permissions, tracing, self-repair, the parse-error
  correction path, the session store. R added a code-friendly retrieval substrate: `store(..., context=)`
  contextual-chunk embeddings + hybrid recall + optional rerank — **Q1's code index should consume this,
  not build a parallel retrieval stack.**
- **Confirmed still-open baselines (the audit verified these):** `file_write` is whole-file only (no
  search/replace/diff); code retrieval is grep + `file_read` (no symbol map); `shell_run` is single-shot
  (no failure-parse/re-run loop); `git_*` is read-only (no snapshot/rewind); the `approval_required`
  event carries raw `arguments`, not a rendered diff; `spawn_agent`'s `role` is a model-role override,
  not a typed prompt/tool-view.

## Follow the DRY playbook UX and R just set

- **Single source of truth.** UX defined every slash command once in a typed `_COMMANDS` list; R routed
  every injected block through one `inject_blocks` seam + one token budget. Match that discipline: one
  edit engine reused by search/replace + diff + patch; one code-index producer; reuse R's retrieval and
  the loop's self-repair rather than adding parallel machinery.
- **New agent tools follow the three-layer model** (`.claude/CLAUDE.md`): logic in an importable core
  (e.g. `scripts/bob_edit.py`), a thin `scripts/tools/<name>.py` that imports it, and the CLI/`--run`
  sharing the same core. **A mutating tool MUST declare itself in `MUTATING_TOOLS`** — the audit flagged
  that a new `file_edit` will NOT inherit "ask" from `file_write` (which is not registered as mutating);
  it must add itself, like `memory_store`/`gen`/`memory_block` do.
- **Shipped, user-editable config where tunables belong** (`defaults.json` + `user.json` overlay,
  default-off). No inlined literals; shared constants only in `defaults.json`.

## Hard conventions (enforce in the plan and any code)

1. **No development-phase / slice markers anywhere** — not in code, comments, docstrings, test names, or
   file names. `Q1/Q2/...` may index the backlog in the PLAN doc only; never in the codebase. Comments
   explain what the code does and why, never when or which plan item.
2. **No phase/slice-based tests.** Extend behavior-named files (`tests/test_file.py`, `tests/test_git.py`,
   `tests/test_shell.py`, `tests/test_sandbox.py`, `tests/test_permissions.py`, `tests/test_subagents.py`)
   or add new behavior-named files (e.g. `tests/test_file_edit.py`, `tests/test_repomap.py`,
   `tests/test_test_fix_loop.py`). Behavior-named classes/methods. Keep the `test_*.py` prefix. Hermetic:
   no live model / network; drive the seams and inject fakes.
3. **Clean, DRY, solid, cross-OS.** No emoji / em-dash in user-facing strings (functional glyphs fine;
   watch tool `description` fields and any agent-facing text — those ARE user-facing). Don't break
   Windows (the tool shell is `pwsh` on Windows via `osenv`; a test-fix loop that shells out must go
   through the same seam, not hardcode bash).

## Q-specific decisions to surface in the plan

- **Q2 (recommended first — highest daily leverage):** a `file_edit` tool with search/replace + unified
  diff (and/or patch) editing, sharing one edit-apply core; register in `MUTATING_TOOLS`; define the
  diff-preview seam so Q5 and any UX diff renderer reuse it (don't duplicate diff rendering).
- **Q3:** a lint/test-fix loop that reuses the loop's self-repair + the sandbox + `shell_run` (through the
  osenv shell seam), parses failure output, and re-edits; config `agent.testCmd`/`agent.lintCmd`/
  `agent.autoFix`, default-off. Distinguish this from the existing goal-satisfaction critic (that checks
  "does the answer satisfy the goal", it does not parse pytest/tsc/eslint).
- **Q1:** repo map / symbol index — state the tree-sitter (or lighter) dependency and how it ranks; make
  it consume R's `store(..., context=)` contextual-chunk embeddings + hybrid recall for code retrieval
  rather than a new index.
- **Q4:** per-step checkpoint + rewind — reuse `bob_session` and prefer `git stash create` for the code
  side; flag the overlap with Module P1 (durable runs) and decide on a shared checkpoint store.
- **Q5:** render a diff in the `approval_required` path (today it shows raw args); loop hooks
  (Pre/PostToolUse/Stop); mid-run steering (today only `CancelToken`); typed sub-agents (extend
  `spawn_agent` beyond a model-role override).

## What to produce (deliverable)

A concrete, reviewable plan of **small, independently landable increments**, each with: scope (one
behavior, gate-green alone), exact files/functions touched (verified), the DRY seam reused/introduced,
behavior-named tests to add (which file, what they pin), acceptance (observable behavior), and risk/cross-
OS notes. Order high-value-first / low-risk-first (recommended spine: **Q2 -> Q3** as the keystones,
then Q1, then Q5, then Q4). Start with a short **reality-audit** section confirming each Q item's true
baseline against the current code before planning.

## Constraints

- **Plan first, get sign-off before building.** If told to proceed, implement one increment at a time,
  gate green after each.
- **Do NOT `git commit` or `git push`** (also in `.claude/CLAUDE.md`).
- **Keep the gate green.** `tools/venv-litellm/bin/python scripts/check.py`; it exits 1 locally only on
  the known `versions.lock STALE` false-positive, so gate on the unittest `Ran N ... OK` line:
  `cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'` (~1025 tests
  today). Re-run discovery after any test change to confirm the count didn't silently drop.

Start by reading `docs/improvements/MODULE-Q-coding-agent-core.md` and the file/git/shell/loop/sandbox
code, reconcile the plan against the current code, then propose the increment sequence for sign-off.
