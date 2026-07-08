# Module Q — Coding-Agent Core (repo intelligence, real edits, test-fix loop, rewind)

**Status:** draft / not scheduled — backlog from the Module O gap review. **Depends on:** the
generic-agent foundation, all shipped — sub-agents, parallel tool calls, compaction, the sandbox,
permissions, and tracing — plus the portable base and the memory + sessions layer. **Read first:**
[MODULE-O-frontier-class.md](MODULE-O-frontier-class.md)'s "Beyond O/P — harness gaps we're currently
ignoring" section (Domain 1), then [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md). Dispatch is
driven by `registry.COMMANDS` (the sole source; scripts/bob/registry.py) and secrets go through the
osenv seam.

**Why this module exists.** O closed the *generic-agent* gap — the agent loop, context, sandbox, MCP,
auth, tracing, and eval now match frontier harnesses. But measured against the tools people actually
code with (Aider, Cursor, Claude Code), Bob is still missing the things that make an agent good at
*software work specifically*. Bob's tools are `file / git / web / shell / memory` only: the file tool is
byte/path-level and `git_*` is read-only ([scripts/tools/git.py](../../scripts/tools/git.py)), so the
agent has no code-aware retrieval, no reliable partial-edit mechanism, no automatic test/lint feedback,
and no way to undo a bad edit. This is the single biggest capability domain O deliberately left open.

Q makes Bob a *coding* agent, building entirely on the generic-agent safety floor (edits gate through
the permission policy; test runs go through the sandbox; big-repo work fans out via sub-agents; every
phase is traced). Everything is config-gated and default-off, per the same discipline.

## Overview

| Sub | Name | Gap addressed | Impact | Effort |
|-----|------|---------------|--------|--------|
| Q1 | Repo map / symbol index (code-aware retrieval) | grep-only; no code map | HIGH | 8–12 h |
| Q2 | Structured edit formats (search/replace, diff, patch) | whole-file writes only | HIGH | 6–8 h |
| Q3 | Lint + run-tests-and-fix loop | `shell_run` only; no feedback loop | HIGH | 5–7 h |
| Q4 | Per-step checkpoint + rewind (code + conversation) | can't undo a bad edit | MED | 6–8 h |
| Q5 | Edit safety + control (diff-preview, loop hooks, steering) | Domain-3 ergonomics | MED | 4–6 h |

**Total:** ~29–41 h. **After Q:** Bob edits real codebases the way Aider/Cursor/Claude Code do —
navigate by symbol, apply precise diffs, run the suite and self-correct, and roll back a wrong turn.

---

## Q1 — Repo map / symbol index (code-aware retrieval)

### Problem
The agent finds code only via `web`/`shell` grep or a full `file_read`. There is no compact, ranked
picture of the codebase — so on a large repo it either floods context with whole files or misses the
relevant definitions entirely. Aider builds a tree-sitter **repo map** (symbol defs/refs ranked by a
PageRank over the symbol graph); Cursor builds a whole-repo **embeddings index**; even NousResearch's
hermes-agent is [adding a PageRank repo map](https://github.com/NousResearch/hermes-agent/issues/535).

### Change
- A **repo-map builder** (`scripts/bob_repomap.py`) using `tree-sitter` to extract symbol
  definitions/references per file, ranked (PageRank over the def/ref graph) to a token-bounded map of
  "the most important lines" — injected on demand, not every turn.
- A Layer-1 **`code_search` tool** (`scripts/tools/code_search.py`): symbol/def/ref lookup + a ranked
  snippet retriever, so the agent asks "where is `X` defined / who calls it" instead of grepping.
  Optionally reuse the embed server (bob_memory's embedding path) for a semantic code index as a second
  retrieval mode (composes with Module R rerank).
- Incremental: cache the map keyed by file mtime/hash; rebuild only changed files (Cursor-style).
- Config: `agent.repoMap` (default off), `repoMapTokens`. Respects the read allowlist + secrets
  denylist ([scripts/tools/file.py](../../scripts/tools/file.py)) — the map never indexes a denied path.

### Effort: 8–12 h (tree-sitter grammar wiring + ranking is the bulk).
### Acceptance
Tests: the map ranks a heavily-referenced symbol above a leaf; `code_search` resolves a def and its
callers; the map excludes denied/secret paths; incremental rebuild touches only changed files. Live: on
this repo, the agent locates `dispatch_call`'s callers via `code_search` without reading whole files.

---

## Q2 — Structured edit formats (search/replace, unified diff, patch)

### Problem
`file_write` writes a whole file ([scripts/tools/file.py](../../scripts/tools/file.py), gated behind
`allowedWritePaths`). For real edits that means the model must reproduce an entire file to change three
lines — slow, token-heavy, and error-prone. Aider's edge is *edit formats*: search/replace blocks,
unified diffs, and patch, auto-selected per model, applied with validation and retried on a failed
apply.

### Change
- An **edit engine** (`scripts/bob_edit.py`) supporting **search/replace blocks** (primary), **unified
  diff**, and **whole-file** (fallback), with **fuzzy anchor matching** (whitespace-tolerant) and a
  clear apply-or-reject result. A failed apply returns a structured error the model self-corrects from —
  reusing the existing `__parse_error__`-style correction path
  ([scripts/tools/tool_registry.py](../../scripts/tools/tool_registry.py):257-260) + the loop's
  self-repair ([scripts/bob_loop.py](../../scripts/bob_loop.py):424-431).
- Layer-1 **`file_edit` tool**: takes an edit in one of the formats; goes through the write allowlist
  + secrets denylist ([scripts/tools/file.py](../../scripts/tools/file.py)) and the permission policy
  ([scripts/bob_permissions.py](../../scripts/bob_permissions.py)), and (Q4) snapshots before applying.
  Note: to get the mutating → `ask` default, `file_edit` must declare itself in `MUTATING_TOOLS` (as
  `memory_store` / `gen` do) — `file_write` is *not* registered there today, so the "ask" default is not
  inherited automatically.
- Multi-file edits apply atomically-ish: validate all hunks first, then apply; on any failure, none
  land (report which hunk failed).
- Config: `agent.editFormat = 'search-replace' | 'diff' | 'whole'` (default `search-replace` when
  `file_edit` is enabled; `file_write` stays as-is so default behavior is unchanged).

### Effort: 6–8 h.
### Acceptance
Tests: a search/replace block edits a file; a stale/ambiguous anchor is rejected with a correctable
error (and self-repair retries once); a denied/secret path is refused; a multi-file edit with one bad hunk lands
nothing. Live: the agent fixes a bug across two files with search/replace, no whole-file rewrite.

---

## Q3 — Lint + run-tests-and-fix loop

### Problem
The agent can invoke `shell_run` to run tests, but there is no *loop*: run → parse failures → fix →
re-run until green. Aider auto-lints and auto-runs a `--test-cmd`, feeding failures back for
self-correction; Cursor's agent mode watches command output and iterates. This is arguably the single
highest-value agentic-coding capability.

### Change
- A **verify-and-fix loop** around edits: after an edit batch, optionally run a configured lint command
  and/or `agent.testCmd`, parse the failures, and feed a structured summary back into the loop so the
  model fixes and re-runs — bounded by `maxSteps` and the loop's verify/self-repair machinery.
- Test/lint commands run through the **sandbox** ([scripts/sandbox.py](../../scripts/sandbox.py),
  `agent.sandbox='on'`) so an agent-run test suite is confined, and are **permission-gated**
  ([scripts/bob_permissions.py](../../scripts/bob_permissions.py)) like any exec.
- Failure parsing is pluggable (pytest / unittest / tsc / eslint patterns); unknown formats fall back to
  "non-zero exit + tail of output".
- Config: `agent.testCmd`, `agent.lintCmd`, `agent.autoFix` (default off). Metered and traced like any
  other tool call.

### Effort: 5–7 h (reuses the loop self-repair + sandbox + metering).
### Acceptance
Tests: a failing test's output is parsed and fed back; a deliberately-broken edit is detected and the
loop continues (not returns); `autoFix` off reproduces today (a single `shell_run`). Live: the agent
makes a change, runs the suite, sees a failure, fixes it, and re-runs to green.

---

## Q4 — Per-step checkpoint + rewind (code + conversation)

### Problem
An agent edit that goes wrong can't be undone — there is no snapshot of the working tree or the
conversation before a step. Claude Code checkpoints file history (`/rewind`) and Cursor's Composer
checkpoints the codebase per apply. This is distinct from durable *resume* across process death
(Module P): Q4 is *revert* — going back to a known-good point within or after a run.

### Change
- **Snapshot before mutate:** before each `file_edit`/`file_write`/mutating step, snapshot the affected
  files (a content-addressed store under `data/`, or a shadow git stash) keyed by run-id + step. Cheap,
  owner-scoped (reuse the `bob_session` store discipline —
  [scripts/bob_session.py](../../scripts/bob_session.py)).
- **`bob task rewind <run-id> [<step>]`** and a shell `/rewind`: restore the working tree (and,
  optionally, the conversation state) to a chosen step. Composes with sub-agent trees
  ([scripts/tools/spawn_agent.py](../../scripts/tools/spawn_agent.py)) — a rewind can scope to a
  sub-run.
- Where the target is a git repo, prefer a git-native snapshot (`git stash create`) so rewind is a
  standard `git` operation the user can inspect; else the shadow store.
- Config: `agent.checkpointEdits` (default off). Shares Module P's checkpoint plumbing if that lands first.

### Effort: 6–8 h.
### Acceptance
Tests: an edit is snapshotted; `rewind` restores the pre-edit content exactly; rewinding to step N drops
steps > N; a git-repo target uses a stash. Live: agent makes three edits, user rewinds to step 1, tree
matches.

---

## Q5 — Edit safety + control (diff-preview, loop hooks, steering)

### Problem
The Module O gap review's **Domain 3** listed several small, high-leverage control features that no
single O item owned: approving a *diff* rather than a whole tool call, lifecycle **hooks** (Claude
Code exposes ~12: SessionStart / PreToolUse / PostToolUse / Stop …) for injecting context / blocking
commands / audit, and **mid-run steering** (redirect without killing the run — Bob has cancel only).
Typed/specialist sub-agents (reviewer/tester) are a small extension of `spawn_agent`
([scripts/tools/spawn_agent.py](../../scripts/tools/spawn_agent.py)).

### Change
- **Diff-preview-before-apply:** for `file_edit`, the permission `ask` surfaces the *rendered diff* (not
  just the raw args) so the operator approves the actual change. Extends the existing `approval_required`
  event ([scripts/bob_loop.py](../../scripts/bob_loop.py) `_dispatch_with_approval`).
- **Agent-loop hooks:** a small hook registry firing at PreToolUse / PostToolUse / Stop (mirrors the
  existing registry pattern); a hook can inject context, block a call, or audit. Config-driven, off by
  default.
- **Mid-run steering:** allow an injected user message to be *queued* into a running loop (via the
  server / shell) so the operator nudges without cancelling.
- **Typed sub-agents:** an optional role registry on top of `spawn_agent`
  ([scripts/tools/spawn_agent.py](../../scripts/tools/spawn_agent.py)) (e.g. `reviewer`, `tester`) with
  distinct prompts/tool views.

### Effort: 4–6 h.
### Acceptance
Tests: an `ask` on `file_edit` carries a rendered diff; a PreToolUse hook can block a call; a queued
steer message reaches the next step; a typed sub-agent runs with its restricted view. Live: approve an
edit by its diff; steer a running task mid-loop.

---

## Traceability (frontier coding-agent gap → sub-item)

| Gap (vs Aider / Cursor / Claude Code) | Sub-item(s) |
|---------------------------------------|-------------|
| No code-aware retrieval (grep only) | **Q1** repo map / symbol index |
| Whole-file writes, no precise edits | **Q2** structured edit formats |
| No test/lint feedback loop | **Q3** run-tests-and-fix |
| Can't undo a bad edit | **Q4** checkpoint + rewind |
| Missing control ergonomics (diff-preview, hooks, steering) | **Q5** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| new `scripts/bob_repomap.py`, `scripts/tools/code_search.py` | Q1 |
| new `scripts/bob_edit.py`, `scripts/tools/file_edit.py`; `scripts/tools/file.py` (share allowlist) | Q2 |
| `scripts/bob_loop.py` (verify-and-fix loop, hooks, steering, diff-preview), `scripts/tools/shell.py` | Q3, Q5 |
| new checkpoint/rewind store (shares `scripts/bob_session.py` / Module P); `scripts/bob/` (`rewind` verb) | Q4 |
| `config/defaults.json` (`runtime.agent.*` keys), `.github/workflows/ci.yml` (Q eval tasks) | all |
| `docs/SECURITY.md` (edit/exec surface), `tests/*` | all |

## Verification

- Python `py_compile` + unittest; `scripts/check.py` gate; the CI matrix.
- New config keys under `config/defaults.json` `runtime.agent.*`, read via `.get(default)`, defaulting
  to today's behavior (every Q capability off by default; `file_write`/`shell_run` unchanged).
- Live: `code_search` on this repo (Q1); a two-file search/replace fix (Q2); a run-tests-and-fix cycle
  under the sandbox (Q3); a rewind (Q4); a diff-approved edit + a mid-run steer (Q5).
- Cite `file:line` for every claim.

## Non-goals

Becoming an IDE or an LSP server (Bob stays CLI/HTTP + the NE interface). A bespoke fast-apply model
(Q2 uses deterministic diff application, not a learned apply model). Replacing `git` (Q4 prefers native
git snapshots where possible). The heavy *context* retrieval work — reranking, contextual-chunk
embeddings, self-editing memory — is **Module R**, not Q. Durable/resumable long-horizon runs and
computer-use are **Module P**.

## Sources

[Aider repo map (tree-sitter)](https://aider.chat/docs/repomap.html) ·
[Aider docs (edit formats, test loop)](https://aider.chat/docs/) ·
[Cursor 2.x agentic loop / index](https://sutopo.com/cursor-21-rewrites-the-agentic-coding-loop-2026-dev-tool/) ·
[Claude Code file checkpointing](https://platform.claude.com/docs/en/agent-sdk/file-checkpointing) ·
[Claude Code hooks](https://code.claude.com/docs/en/hooks) ·
[hermes-agent PageRank repo-map](https://github.com/NousResearch/hermes-agent/issues/535)
