# Plan Module R (context engineering) — cold-start planning handoff

**Paste this into a fresh Claude Code session at the repo root (`/home/siva/dev/bob`).** Your job is to
produce an **implementation plan** for Module R — smarter retrieval + memory on top of what already
ships. Plan first; do not write feature code until the plan is signed off. Verify every claim below
against the repo before planning (line numbers and counts drift).

## What you're planning

Module R = context engineering. The backlog exists and was just refreshed against the code:

- **Read first:** `docs/improvements/MODULE-R-context-engineering.md` — R1 cross-encoder rerank +
  contextual-chunk embeddings, R2 self-editing memory blocks (MemGPT/Letta-style agent-editable core
  context), R3 conversation paging (`conversation_search` over dropped/older turns). All three are
  unimplemented; the doc's dependency features all shipped (it cites them by feature + `file:line`).
- **Code you'll build on:** `scripts/bob_memory.py` (typed/owner-scoped store, recency·importance·type·
  usage·salience ranking + per-type half-life decay; hybrid dense+BM25/FTS5 recall via RRF at
  `_recall_hybrid` ~421-460, `recall(...)` ~463, FTS5 build `_ensure_fts` ~372), `scripts/bob_loop.py`
  (summarize-compaction ~809 + frame ~667-677; prefix-cache-aware layout `_truncate_stable_prefix`
  ~678; tool-result clearing `_clear_old_tool_results` ~780 + the `read_result` retention seam),
  `scripts/tools/memory.py` (the `memory_store`/`memory_recall` agent tools), `scripts/tools/read_result.py`
  + `scripts/tools/tool_registry.py` (~287 the retention/`clear_stub` seam), `scripts/bob_session.py`
  (SQLite session store: full owner-scoped turn history via `append_turn` ~137-164),
  `scripts/bob_core.py` (memory-injection helpers: `profile_block` / `project_memory_block`, wired into
  the loop ~1168/1321).
- **Config:** memory settings live under `config/defaults.json` -> `runtime.memory.*` (e.g. `retrieval`,
  `rrfK`), deep-merged with `config/user.json`. New knobs go there, default-off, read with `.get(default)`.

## Current state to respect (recent work — do not regress it)

- **One Python engine, zero PowerShell.** Everything is `python -m bob`. Config resolves from
  `defaults.json` + optional `user.json` on every OS via `scripts/bob_config.py` (no `data/config.json`,
  no `bob.psd1`).
- **The codebase is fully decoupled from development-phase markers** (`O#/N#/MEM-#/NE#/NB#/ONE-*/slice`).
  None remain in code, tests, comments, or docs. Test files are domain-named.
- **Memory storage/ranking is already strong** — R is additive on top, not a rewrite. Hybrid recall
  stops at RRF (no rerank yet). There is no agent-editable always-injected core block. There is no
  search/page-back over older turns.
- **R3 reality (important):** `bob_session` already persists the FULL final user/assistant turn history;
  compaction only trims the per-request in-context message list, it does not delete stored turns. The
  CLI path is stateless (no session store). So the real R3 work is (1) persist the full transcript
  including intermediate tool turns / CLI runs into an owner-scoped store, and (2) add a
  `conversation_search` + page-back-into-context tool — NOT "retain dropped turns." Note this store
  overlaps Module P1 (durable runs); decide whether they share one store.

## Follow the pattern the UX overhaul just set (clean / DRY / solid)

The shell UX work is the reference for quality — mirror it:
- **Single source of truth.** UX defined every slash command once in a typed `_COMMANDS` list and derived
  the completion tree, dispatch, help, and metadata from it. Do the same for R: one injection budget/seam
  (don't add a second memory-injection path next to `profile_block`/`project_memory_block` — route
  through one), one place for new config keys, one candidate-set producer reused by rerank.
- **Shipped, user-editable config file where it fits** (UX added `config/ui.json` layered over built-in
  defaults). If R introduces tunables, follow the `defaults.json` + `user.json` overlay; don't inline
  literals.
- **New agent tools follow the three-layer model** in `.claude/CLAUDE.md` (tool auto-discovers from
  `scripts/tools/*.py`; logic importable; CLI + tool + `--run` share one core). A mutating tool must
  declare itself in `MUTATING_TOOLS`.

## Hard conventions (enforce in the plan and any code it leads to)

1. **No development-phase / slice markers anywhere** — not in code, comments, docstrings, test names, or
   file names. The `R1/R2/R3` labels may index the backlog in the PLAN doc only; they must never appear
   in the codebase. Comments explain what the code does and why, never when or which plan item.
2. **No phase/slice-based tests.** Extend the behavior-named files (`tests/test_memory.py`,
   `tests/test_hybrid_recall.py`, `tests/test_context.py`, `tests/test_compaction.py`,
   `tests/test_context_clearing.py`) or add new behavior-named files (e.g. `tests/test_rerank.py`,
   `tests/test_conversation_search.py`). Test classes/methods named by behavior. Keep the `test_*.py`
   prefix. Tests hermetic: no live model, no network — inject fakes / drive the seams (the suite already
   uses a `_fake_embed`; reuse that discipline).
3. **Clean, DRY, solid, cross-OS.** Reuse the hybrid-recall fused candidate set for R1 rerank rather than
   re-querying; reuse the existing injection/fit path for R2; reuse `bob_session` for R3 persistence.
   No emoji / em-dash in user-facing strings (functional glyphs fine). Don't break Windows.

## R-specific decisions to surface in the plan

- **R1 rerank:** what runs the cross-encoder? A local model via the existing embed/inference stack, or a
  small in-process reranker? State the dependency, the default-off config (`memory.rerank`,
  `memory.rerankTopN`), and that it reranks the already-fused RRF candidates (`bob_memory.py:421-460`) —
  no second retrieval.
- **R2 self-editing memory blocks:** a new agent-editable, size-capped, always-injected block store +
  tool. Decide how it composes with the existing auto-injected `profile_block`/`project_memory_block`
  (one injection seam + one token budget, in the spirit of the existing fit-to-budget logic) so the model
  gets ONE coherent injected-memory section, not three competing ones.
- **R3:** the store/schema for full-transcript persistence (incl. tool turns + CLI), and the
  `conversation_search` tool contract (search -> page selected turns back into context). Flag the shared-
  store decision with P1.

## What to produce (deliverable)

A concrete, reviewable plan of **small, independently landable increments**, each with: scope (one
behavior, gate-green alone), exact files/functions touched (verified), the DRY seam reused/introduced,
behavior-named tests to add (which file, what they pin), acceptance (observable behavior), and risk/cross-
OS notes. Order high-value-first / low-risk-first (a sensible spine: R1 rerank first — cheapest, reuses
the fused candidate set; then R2; then R3, which needs the transcript-persistence groundwork). Start with
a short **reality-audit** section: read the current memory/loop/session code and confirm each R item's
true baseline before planning.

## Constraints

- **Plan first, get sign-off before building.** If told to proceed, implement one increment at a time,
  gate green after each.
- **Do NOT `git commit` or `git push`** (also in `.claude/CLAUDE.md`).
- **Keep the gate green.** `tools/venv-litellm/bin/python scripts/check.py`; it exits 1 locally only on
  the known `versions.lock STALE` false-positive, so gate on the unittest `Ran N ... OK` line:
  `cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'` (~991 tests
  today). Re-run discovery after any test change to confirm the count didn't silently drop.

Start by reading `docs/improvements/MODULE-R-context-engineering.md` and the memory/loop/session code,
reconcile the plan against the current code, then propose the increment sequence for sign-off.
