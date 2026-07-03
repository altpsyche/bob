# Module P — Frontier Product (durable autonomy, multimodal, computer-use)

**Status:** draft / not implemented. **Depends on:** O (sub-agents, sandbox, permissions, tracing,
eval) + NB–ND (portable/reliable base) + NE (interface) + **NE6-MEM / MEM2** (the persisted-session +
typed/scoped-memory layer P1/P2 build directly on). **Read first:**
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) and MODULE-O's "Already-built seams" section.
This is the **last** module — the one that takes Bob from a frontier *harness* to a frontier *product*.

**Why this module exists.** After O, Bob matches frontier agent harnesses on the *harness* axes
(loop, context, sandbox, MCP, auth, observability, CI). The review of the roadmap flagged that this
still isn't the whole of "frontier": the top products (Devin, Claude Code, Operator-class tools) also
do three things O doesn't:
- **Durable, long-horizon autonomy** — a task that runs for *hours across restarts*, checkpointed and
  resumable, not a single in-memory loop that dies with the process.
- **Deep multimodal *in the loop*** — vision and voice as first-class inside the agent loop and the
  interface, not standalone `bob describe` / `bob voice` side-doors.
- **Computer-use** — driving the desktop (screen + input) as a sandboxed, permission-gated tool.

P closes those, deliberately last, because each is only *safe* on top of O (sandbox O5, permissions
O6, scopes O8, tracing O9) and only *reliable* on top of NB–ND. It is the most speculative module;
some of it (computer-use) is genuinely risky and is gated hard.

**Scope note.** P is opt-in and gated throughout — nothing here runs by default. Computer-use in
particular is off unless explicitly enabled, sandboxed (O5), and permission-gated (O6). P targets
Windows + Linux; macOS follows the NC/O deferral.

## Overview

| Sub | Name | Frontier-product gap | Impact | Effort |
|-----|------|----------------------|--------|--------|
| P1 | Durable & resumable runs (checkpoint/resume) | runs die with the process | HIGH | 8–12 h |
| P2 | Background / detached long-running tasks | no hours-long, disconnect-surviving jobs | HIGH | 6–8 h |
| P3 | Deep multimodal *in the loop* (vision + voice) | multimodal is side-doors, not in-loop | MED | 8–12 h |
| P4 | Computer-use / desktop automation (gated) | can't drive a GUI | MED | 12–18 h |
| P5 | Long-horizon eval + safety review | unproven + computer-use unreviewed | MED | 5–7 h |

**Total:** ~39–57 h.

---

## P1 — Durable & resumable runs (checkpoint / resume)

### Problem
`run_agent_events` holds all state in memory: `messages`, step count, tool results, the sub-agent
tree (O1). If the process dies (crash, restart, `bob update`, machine reboot) mid-task, the whole run
is lost. Frontier products checkpoint and resume — a task can span restarts.

### Change
> **Seam note (NE6-MEM / MEM2):** the "session store" is `bob_session.SessionStore`
> ([bob_session.py](../../scripts/bob_session.py), N2) — which since WI-6 also backs the NE shell's persisted sessions and
> carries `owner_id` + token budget/spend. Checkpoint run-state alongside it (a *run* ≠ a *session
> transcript* — keep them distinct rows/tables). Crucially, the checkpoint must persist and a resume
> must **restore `RunContext.owner`, `agent_depth`, and `scope`** (MEM-6/7), or a resumed run's memory
> recall/store lands under the wrong identity/project. Don't re-consolidate on resume — consolidation
> fires on real session end, not on a run checkpoint.
- **Checkpoint** the run state (messages, step, `exit_requested`, per-sub-agent state, metrics)
  to the session store (N2, already SQLite/WAL) at each step boundary — cheap, atomic, owner-scoped.
- **Resume**: `run_agent_events(..., resume=<run_id>)` rehydrates from the last checkpoint and
  continues; the SSE server and NE shell expose `resume`. Idempotent tool re-execution is avoided by
  recording tool results in the checkpoint (a resumed run replays results, doesn't re-run side
  effects).
- Sub-agent trees (O1) checkpoint recursively; a resumed parent resumes its children.
- Config: `agent.checkpoint = $true` (default on for long runs / `--deep`, off for one-shots).

### Effort: 8–12 h.
### Acceptance
Tests: a run killed mid-step resumes from the checkpoint and completes with identical final state; a
tool that already ran is *not* re-executed on resume (replayed from the checkpoint); a sub-agent tree
resumes. Live: `bob agent --deep "<long task>"`, kill the process mid-run, `bob task resume <id>`
finishes it.

---

## P2 — Background / detached long-running tasks

### Problem
Every run is foreground and tied to the invoking process/connection. N3 made a *disconnect cancel*
the run; frontier products let a task **detach and keep running** for hours, with status/resume, and
survive the client leaving. There is no task queue or worker.

### Change
- A lightweight **task runner**: `bob task start "<goal>"` enqueues a durable run (P1) executed by a
  background worker (a `bob agent serve` worker thread/process, or a systemd/scheduled worker via
  NC4); returns a `task_id` immediately.
- `bob task status|logs|resume|cancel <id>` and REST/SSE equivalents; the NE shell shows running
  tasks. Status/metrics from N5; owner-scoped (N1); resumable (P1).
- Disconnect no longer cancels a *detached* task (unlike an attached stream) — it keeps running; only
  an explicit cancel or the cancel token stops it.
- Memory (NE6-MEM/MEM2): a detached task is still owner-scoped (N1) — its recall/store use the task
  owner's memory, and end-of-run consolidation follows the same rule as any run (only a real session
  lifecycle consolidates, not each background task tick).

### Effort: 6–8 h.
### Acceptance
Tests: `task start` returns immediately; the task runs to completion after the client disconnects;
`task status` reports progress; `task resume` continues a checkpointed task; cancel stops it;
owner-scoping enforced. Live: start a multi-step task, close the terminal, reconnect later and see it
finished (or resume it).

---

## P3 — Deep multimodal *in the loop* (vision + voice)

### Problem
Bob has vision (`bob describe`) and voice (`bob voice`) as **standalone side-doors**, not inside the
agent loop or the NE interface. A frontier agent can *see* a screenshot mid-task and *speak/listen*
as a mode of the same session.

### Change
- **Vision in the loop:** tools may return images (e.g. a future screen-capture tool, P4; a chart
  generator); the loop passes them to the vision role (`get_role("vision")`) as image content in the
  messages, so the agent can reason over what it sees mid-run. Reuses the existing vision model
  routing (Module G) — the new part is threading image content through `run_agent_events` messages.
- **Voice as an NE shell mode:** `/voice` in the interactive shell (NE2) turns the current session
  into a spoken loop (Whisper STT in, Piper TTS out — the existing engines), so voice is a *mode of
  the unified interface*, not a separate command. Streamed (N3/N6), cancellable.
- Config-gated; degrades cleanly where a vision model or audio devices are absent (NC/NB `osenv`).

### Effort: 8–12 h.
### Acceptance
Tests: the loop accepts image content and routes a vision turn; a tool returning an image is consumed
by the next model turn; `/voice` mode round-trips STT→loop→TTS in the shell. Live: an agent task that
captures + reasons over a screenshot; a spoken conversation inside `bob`.

---

## P4 — Computer-use / desktop automation (gated)

### Problem
Bob cannot drive a GUI — the frontier "computer use" / Operator capability (screen capture + mouse/
keyboard control for apps without an API). This is powerful *and dangerous*, so it is built last, on
top of the sandbox (O5), permission model (O6), and scopes (O8).

### Change
- A **`computer` tool surface** (opt-in, `agent.computerUse = $true`, default off): `screenshot`,
  `click`, `type`, `key`, `scroll` — Windows via UI Automation / SendInput, Linux via the appropriate
  X11/Wayland tooling (`xdotool`/`ydotool`), behind NB3's `osenv`.
- **Hard gating:** every `computer` action is `ask` by default (O6), scoped per owner (O8), runs
  under the O5 sandbox where feasible, is traced (O9), and is rate-limited. Screenshots feed the
  vision-in-loop path (P3) so the agent can *see* what it's doing.
- **Kill switch + audit:** a global `bob computer stop`, an on-screen indicator while active, and an
  append-only audit of every action (O6 audit line). Never enabled in unattended/detached tasks (P2)
  without an explicit `--allow-computer` flag.

### Effort: 12–18 h (the OS input/automation layer is the bulk; Windows + Linux backends).
### Acceptance
Tests (headless/mocked input backend): `computer` actions are permission-gated (`ask`/`deny`
enforced), scoped, audited, and refused when `computerUse` is off. Live (attended): the agent takes a
screenshot, sees it (P3), clicks a target, types text — each action prompting per O6 — with a working
kill switch. Security-reviewed in P5.

---

## P5 — Long-horizon eval + safety review

### Problem
O10's eval covers bounded agent tasks; P adds *long-horizon* (checkpoint/resume, hours-long) and
*computer-use* — neither is proven, and computer-use especially needs a security review before it
ships enabled.

### Change
- **Extend the O10 eval** with long-horizon tasks: multi-hour / multi-restart scenarios scored for
  correctness + resumption integrity (does a resumed run reach the same result?); run on the release
  (GPU) tier of the CI matrix (C5).
- **Security review** (`docs/SECURITY.md` extension, test-backed like N9/O): the computer-use threat
  model (prompt-injection → GUI actions), the gating chain (off-by-default → `ask` → sandbox → scope
  → audit → kill switch), and the detached-task risk (P2 + P4 interaction). Each claim backed by a
  test, as in N9.
- Document the "autonomy dial": one-shot → `--deep` → detached task → computer-use, each a bigger
  grant requiring a bigger opt-in.

### Effort: 5–7 h.
### Acceptance
The long-horizon eval runs green and catches a resumption regression; the computer-use security
review is complete with every claim test-backed; the autonomy dial is documented.

---

## Traceability (frontier-product gap → sub-item)

| Gap (vs frontier products, not just harnesses) | Sub-item(s) |
|-----------------------------------------------|-------------|
| Runs die with the process; no long-horizon autonomy | **P1** (durable/resumable), **P2** (detached tasks) |
| Multimodal is side-doors, not in the loop/interface | **P3** |
| Can't drive a GUI (computer-use) | **P4** |
| Long-horizon + computer-use unproven/unreviewed | **P5** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| `scripts/bob_loop.py` (checkpoint/resume, image content), `scripts/bob_session.py` (checkpoint store) | P1, P3 |
| new `scripts/bob_tasks.py` (task runner); `scripts/bob_agent_server.py`, `scripts/bob/` (task verbs) | P2 |
| `scripts/bob/shell.py` (`/voice` mode), vision routing in the loop | P3 |
| new `scripts/tools/computer.py` (+ `osenv` input backends); `config/bob.psd1` (`computerUse`) | P4 |
| `tests/eval/` (long-horizon), `docs/SECURITY.md` | P5 |
| `tests/*` | every sub-item |

## Verification

- Python `py_compile` + unittest; `check.ps1` gate (N8); the CI matrix (C5) incl. the P5 long-horizon
  eval on the GPU tier.
- Live: kill+resume a `--deep` task (P1); detach+reconnect a task (P2); screenshot-reason-act with
  per-action approval + kill switch (P3/P4); spoken session in `bob` (P3).
- Cite `file:line` for every claim.

## Non-goals

A general RPA/automation platform (computer-use is an agent tool, opt-in and gated — not a macro
recorder). Unattended destructive computer-use without explicit opt-in. macOS (follows the NC/O
deferral). Cloud task orchestration (tasks run on the local worker; local-first stays). Replacing the
model or the OpenAI-compatible protocol.
