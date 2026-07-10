# Plan Module P (frontier product) — cold-start planning handoff

**Paste this into a fresh Claude Code session at the repo root (`/home/siva/dev/bob`).** Your job is to
produce an **implementation plan** for Module P — the last module, taking Bob from a frontier *harness*
to a frontier *product*. Plan first; do not write feature code until the plan is signed off. Verify every
claim below against the repo before planning (line numbers and counts drift).

## What you're planning

Module P = durable autonomy, multimodal, computer-use, safety. Backlog exists and was refreshed against
the code:

- **Read first:** `docs/improvements/MODULE-P-frontier-product.md` — P1 durable/resumable runs
  (checkpoint/resume), P2 background/detached long-running tasks, **P3 deep multimodal in-loop (already
  SHIPPED via ONE-B — vision threads through `run_agent_events`, `/voice` is a shell mode)**, P4
  computer-use/desktop automation (gated), P5 long-horizon eval + safety review. So the OPEN work is
  **P1, P2, P4, P5.**

## Current state to respect (Q/R/UX all shipped since this plan was written — build on them, don't duplicate)

The substrate P was designed to need now largely exists:

- **Checkpoint store already exists** (`scripts/bob_checkpoint.py`): SQLite, owner-scoped snapshots keyed
  by **`(run_id, step)`**, mirrors `bob_session` discipline (WAL, per-thread conn), with an optional
  `git stash create` path. Today it snapshots the FILES a mutating step touches (for `task rewind`). **P1
  should EXTEND this store to also persist run STATE** (the message list, step index, and the RunContext
  identity — owner/scope/agent_depth at `bob_loop.py` ~305-336), not add a parallel store.
- **`task` verb namespace exists** (`scripts/bob/registry.py`): `task test`, `task rewind` (Q). **P2 should
  ADD to this namespace** (`task start|status|logs|resume|cancel`), not invent a new one.
- **Detach + schedule infra exists:** `osenv.start_detached` (`scripts/osenv.py` ~445, used by `stack.py`)
  and cron-scheduled agent goals in `scripts/tools/schedule.py`. P2's disconnect-surviving `task_id`
  model builds on these + P1's resume.
- **In-flight steering + a live-run registry exist:** `POST /v1/agent/steer` + `_live_runs` (owner-scoped)
  in `scripts/bob_agent_server.py`. Reuse for P2 status/steer.
- **Loop hooks exist** (`_fire_pre_hooks`/`_fire_post_hooks`, `registry.hooks` PreToolUse/PostToolUse in
  `bob_loop.py` ~365-400) and a permission policy (`bob_permissions.py`) + sandbox (`sandbox.py`). **P4
  computer-use must route through these** (default-off gate + PreToolUse approval + audit), not bypass them.
- **Screen capture exists:** `bob_vision.capture_screen` (`scripts/bob_vision.py` ~53), and vision content
  threads through the loop (P3). P4's `click/type/key/scroll` input side is the missing half; the
  see-the-screen half is ready.
- **Eval harness exists:** `tests/eval/run_eval.py` + `tests/eval/tasks.py` + `bob eval`. P5 extends it
  with long-horizon/resume/computer-use tasks; it has none today.
- **One Python engine, zero PowerShell; fully marker-decoupled; test files domain-named.** Config resolves
  from `config/defaults.json` + `config/user.json` on every OS.

## Follow the DRY playbook UX / R / Q set

- **Single source of truth, reuse the seams:** extend `bob_checkpoint` for P1 (don't add a second run-state
  store); extend the `task` namespace for P2; route P4 through the hooks + permission + sandbox seams; put
  P4's OS-specific input backend behind `scripts/osenv.py` (the ONE OS seam) exactly like the shell/screen
  seams — never branch OS in a tool.
- **New agent tools follow the three-layer model** (`.claude/CLAUDE.md`): importable core + thin
  `scripts/tools/<name>.py` + CLI/`--run` sharing it. **A mutating/side-effecting tool MUST declare itself
  in `MUTATING_TOOLS`** (as `file_edit`/`memory_store`/`gen` do) — a `computer` tool is the most
  side-effecting of all; default-off and approval-gated.
- **Tunables in `defaults.json` + `user.json` overlay, default-off** (e.g. `agent.checkpoint`,
  `agent.computerUse`) — no inlined literals; shared constants only in `defaults.json`.

## Hard conventions (enforce in the plan and any code)

1. **No development-phase / slice markers anywhere** — not in code, comments, docstrings, test names, or
   file names (Q leaked one `(Q4)` docstring tag that had to be removed — don't repeat it). `P1/P2/...`
   may index the backlog in the PLAN doc only. Comments say what the code does and why, never when.
2. **No phase/slice-based tests.** Behavior-named files and `Test…`/`test_…` names
   (`tests/test_durable_runs.py`, `tests/test_task_runner.py`, `tests/test_computer_use.py`,
   `tests/test_safety_eval.py`). Keep the `test_*.py` prefix. Hermetic: no live model, no real desktop —
   inject fakes / drive the seams (mock the OS input backend; never move a real mouse in a unit test).
3. **Clean, DRY, solid, cross-OS.** No emoji / em-dash in user-facing strings — including tool
   `description` fields and any agent-facing text (this bit both R and Q reviews). Computer-use is the
   most OS-divergent feature in the repo: all of it goes through `osenv`, and it must degrade gracefully
   where a backend is absent.

## P-specific decisions to surface in the plan (P is the risky one — be explicit)

- **P1 durable runs:** the run-state schema to add to `bob_checkpoint` (messages + step + RunContext), and
  the resume entry point on `run_agent_events` (a `resume=run_id` path that rehydrates and continues).
  Config `agent.checkpoint` default-off.
- **P2 detached tasks:** the `task start/status/logs/resume/cancel` verbs over `osenv.start_detached` +
  the `_live_runs`/steer registry + P1 resume; how a detached task's transcript/logs are captured and
  surfaced. Decide the shared store with P1.
- **P4 computer-use (gated, high-risk):** a `scripts/tools/computer.py` (`click/type/key/scroll/move`)
  with the OS input backend behind `osenv` (Linux xdotool/ydotool, Windows SendInput); `agent.computerUse`
  default-off; ALWAYS approval-gated and routed through the PreToolUse hooks + permission policy; reuse
  `capture_screen` for the see-act loop. Treat this as opt-in and sandbox-aware.
- **P5 long-horizon eval + safety:** add long-horizon/resume/computer-use tasks to `tests/eval`; add an
  autonomy dial + a computer-use threat model to `docs/SECURITY.md` (which has neither today). **P5 gates
  P4's exposure** — do the safety review alongside/ahead of shipping computer-use.

## What to produce (deliverable)

A concrete, reviewable plan of **small, independently landable increments**, each with: scope (one
behavior, gate-green alone), exact files/functions touched (verified), the DRY seam reused/introduced,
behavior-named tests to add (which file, what they pin), acceptance (observable behavior), and risk/cross-
OS/safety notes. Recommended order: **P1 → P2** first (durable + detached runs; high value, moderate
risk, and they reuse the checkpoint/task/steer/session seams), then **P5 safety foundations + P4
computer-use as a gated pair** (computer-use ships behind the safety review, default-off, hook-gated).
Start with a short **reality-audit** confirming what `bob_checkpoint`, the `task` namespace, the hooks,
the steer registry, and `capture_screen` already provide, so P1/P2/P4 EXTEND rather than rebuild.

## Constraints

- **Plan first, get sign-off before building.** If told to proceed, implement one increment at a time,
  gate green after each.
- **Do NOT `git commit` or `git push`** (also in `.claude/CLAUDE.md`).
- **Keep the gate green.** `tools/venv-litellm/bin/python scripts/check.py`; it exits 1 locally only on
  the known `versions.lock STALE` false-positive, so gate on the unittest `Ran N ... OK` line:
  `cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'` (~1126 tests
  today). Re-run discovery after any test change to confirm the count didn't silently drop.

Start by reading `docs/improvements/MODULE-P-frontier-product.md` and the checkpoint/session/loop/agent-
server/vision code, reconcile the plan against the current code, then propose the increment sequence for
sign-off.
