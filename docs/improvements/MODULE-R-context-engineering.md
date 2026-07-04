# Module R — Context Engineering (rerank, self-editing memory, conversation paging)

**Status:** draft / not scheduled — backlog from the Module O gap review. **Depends on:** **O — all
shipped**, especially **O3** compaction, **O13** prefix-cache-aware context, **O14** hybrid recall
(dense + BM25/FTS5 + RRF), and **O15** tool-result clearing (`read_result` retention seam), plus
NE6-MEM / MEM2 (the typed, owner/project-scoped memory + persisted-session layer). **Read first:**
[MODULE-O-frontier-class.md](MODULE-O-frontier-class.md)'s "Beyond O/P" section (Domain 4) and
[docs/MEMORY.md](../MEMORY.md).

**Why this module exists.** Bob's long-term memory *storage/ranking* is already strong — typed facts
with recency·importance·type·usage·salience weighting and per-type half-life decay
([scripts/bob_memory.py](../../scripts/bob_memory.py)), and `memory_store`/`memory_recall` are
model-callable, so Bob is already "MemGPT-lite." O14 added hybrid **retrieval** (dense + BM25 via RRF)
and O15 added **tool-result clearing**. What remains — the *heavy tail* of the context-engineering
frontier (Anthropic's context-editing work, MemGPT/Letta, Manus) — is the expensive, higher-risk part
that O deliberately deferred:
- **retrieval quality** past hybrid: a cross-encoder **rerank** and **contextual-chunk** embeddings
  (Anthropic reports hybrid alone cuts failed retrievals ~49%, **~67% with rerank**);
- **agent-managed context**: MemGPT/Letta **self-editing memory blocks** the model edits in its loop,
  and **conversation paging** (`conversation_search`) to pull back turns O3 dropped — the OS-inspired
  main / recall / archival hierarchy.

R turns Bob's context from *well-stored but statically-retrieved* into *agent-managed and
best-in-class-retrieved*. All config-gated, default-off, per the O discipline; retrieval upgrades reuse
the existing SQLite + embed-server infra (no new service).

## Overview

| Sub | Name | Gap addressed | Impact | Effort |
|-----|------|---------------|--------|--------|
| R1 | Cross-encoder rerank + contextual-chunk embeddings | hybrid without rerank/context | HIGH | 5–7 h |
| R2 | Self-editing memory blocks (MemGPT/Letta core memory) | no agent-editable core context | MED | 6–9 h |
| R3 | Conversation paging (`conversation_search` over dropped turns) | dropped turns are gone | MED | 5–7 h |

**Total:** ~16–23 h. **After R:** retrieval is at the Contextual-Retrieval frontier, the agent curates
its own always-in-context notes, and no compacted turn is ever truly lost — it can be paged back.

---

## R1 — Cross-encoder rerank + contextual-chunk embeddings

### Problem
O14 fused dense + BM25 via Reciprocal Rank Fusion — the "hybrid" tier. Anthropic's Contextual Retrieval
shows two more levers close most of the remaining gap: a **cross-encoder rerank** of the fused
candidates (hybrid → hybrid+rerank takes failed-retrieval reduction from ~49% to ~67%), and
**contextual-chunk embeddings** (prepend a short chunk-situating context before embedding so a chunk
stands alone). Bob's recall stops at RRF today.

### Change
- **Rerank pass** in the recall path ([scripts/bob_memory.py](../../scripts/bob_memory.py)): take the
  top-N RRF candidates and re-score with a cross-encoder (a small local reranker served alongside the
  embed model, or a scored LLM pass) → top-K. Gated by `memory.rerank` (default off; O14 already ships
  the config seam `retrieval` + `rrfK`).
- **Contextual-chunk embeddings**: when a stored memory is a chunk of a larger source (project docs,
  future code-RAG for **Module Q**), prepend a one-line context before embedding so retrieval doesn't
  depend on the chunk being self-contained. Atomic typed facts (today's memories) already stand alone,
  so this matters most once chunked sources exist — build the seam here, exploit it in Q1's code index.
- Reranker/model resolves via the existing role routing; absent → clean fallback to hybrid (loud-fail).
- Config: `memory.rerank` (default off), `memory.rerankTopN`.

### Effort: 5–7 h (reuses O14's fused candidate set + the embed server).
### Acceptance
Tests: rerank reorders a fused candidate set so a semantically-best-but-lexically-weak hit rises;
`rerank` off reproduces O14 hybrid exactly; a missing reranker falls back to hybrid with a logged
warning; a contextual-chunk embed round-trips. Live (GPU tier): a query that hybrid ranks 4th is
reranked to 1st.

---

## R2 — Self-editing memory blocks (MemGPT/Letta core memory)

### Problem
Bob's memory is *auto*-recalled (O14) and *auto*-consolidated at session end — the model doesn't
directly curate what stays in its context. MemGPT/Letta give the agent **editable core-memory blocks**
(e.g. a "persona" block and a "user/task" block) it rewrites *in its loop* via tool calls, always
present in context and bounded by a character budget. That's how a Letta agent decides what's worth
keeping without waiting for end-of-session consolidation.

### Change
- **Core-memory blocks**: a small set of named, size-capped blocks (e.g. `task`, `user`) persisted per
  owner/scope (reuse the memory store), **always injected** into context (via the existing injection
  path + O13 stable-prefix so an edited block re-freezes cleanly into the prefix).
- Layer-1 **`memory_block` tool** (`append` / `replace` / `read`): the model edits a block mid-run;
  edits are mutating → O6 `ask`/policy applies, and audited (O6 audit line). Bounded so a block can't
  grow the prefix unboundedly.
- Blocks are owner/scope-scoped (MEM-6/7) and survive across sessions; they complement — don't replace —
  auto-consolidation (which still fires on real session end only).
- Config: `memory.coreBlocks` (default off), block name → token cap.

### Effort: 6–9 h.
### Acceptance
Tests: the model appends to a block and it persists + re-injects next turn; a block edit is O6-gated +
audited; a block respects its token cap (oldest trimmed / rejected); off = no blocks injected
(byte-identical). Live: across two turns the agent writes a fact to its `task` block and uses it later
without re-deriving it.

---

## R3 — Conversation paging (`conversation_search` over dropped turns)

### Problem
O3 compaction summarizes the dropped span into one note, and O15 clears stale tool results to a
`read_result` stub — but the *original* older turns themselves are gone from context once compacted.
MemGPT/Letta keep an OS-style **recall/archival** tier the agent can search on demand
(`conversation_search`) to page a specific earlier exchange back in. Bob can't retrieve a compacted
turn verbatim.

### Change
- **Persist the full transcript** for a run/session (the N2 `bob_session` store already holds session
  history — [scripts/bob_session.py](../../scripts/bob_session.py); extend to retain the pre-compaction
  turns O3 drops, owner-scoped).
- Layer-1 **`conversation_search` tool**: semantic/keyword search over the run's *dropped/older* turns
  (reuse the O14 hybrid retriever over transcript rows), returning matching exchanges the model can pull
  back into context on demand — the read side mirrors O15's `read_result`
  ([scripts/tools/tool_registry.py](../../scripts/tools/tool_registry.py) retention seam), but over
  turns rather than tool outputs.
- Bounded: a paged-in result is itself subject to O3/O15 so it can't re-overflow; paging is opt-in and
  metered (N5) / traced (O9).
- Config: `agent.conversationPaging` (default off).

### Effort: 5–7 h.
### Acceptance
Tests: a compacted-away turn is retrievable by `conversation_search`; the retriever is owner-scoped
(no cross-owner leak, per N1); a paged-in result is re-subject to compaction; off = no persistence of
dropped turns beyond today. Live: a long run compacts an early decision, and the agent pages it back
verbatim when it becomes relevant again.

---

## Traceability (context-engineering frontier → sub-item)

| Gap (vs Anthropic context-eng / MemGPT / Letta) | Sub-item(s) |
|-------------------------------------------------|-------------|
| Hybrid retrieval without rerank / contextual chunks | **R1** |
| No agent-editable always-in-context memory | **R2** self-editing blocks |
| Compacted turns are unrecoverable | **R3** conversation paging |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| `scripts/bob_memory.py` (rerank pass, contextual-chunk embed) | R1 |
| new `scripts/tools/memory_block.py`; `scripts/bob_loop.py` (block injection + O13 prefix) | R2 |
| new `scripts/tools/conversation_search.py`; `scripts/bob_session.py` (retain dropped turns) | R3 |
| `config/defaults.json` (`runtime.memory.*` / `runtime.agent.*` keys), `tests/*` | all |

## Verification

- Python `py_compile` + unittest; `scripts\check.ps1` gate (N8); the CI matrix (C5); the O10 eval gains
  a compaction-recall / retrieval-quality task.
- New config keys under `config/defaults.json` `runtime.memory.*` / `runtime.agent.*`, read via
  `.get(default)`, defaulting to today's behavior (R off = O14/O3/O15 behavior unchanged).
- Live: rerank lift on a GPU-tier query (R1); a cross-turn core-memory edit (R2); page back a compacted
  decision (R3).
- Cite `file:line` for every claim.

## Non-goals

Replacing O14 hybrid recall (R1 *extends* it) or O3/O15 (R3 *complements* them — it doesn't stop
compaction, it makes it reversible). A separate vector database or memory service (everything stays on
the existing SQLite + embed-server infra, local-first). The prefix-cache mechanics themselves
(O13 owns them; R2 reuses the stable-prefix seam). Code-specific retrieval (repo map / code index is
**Module Q**; R1's contextual-chunk seam is what Q's index consumes).

## Sources

[Anthropic: contextual retrieval (hybrid + rerank)](https://www.anthropic.com/engineering/contextual-retrieval) ·
[Anthropic: effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Anthropic: context editing / tool clearing (cookbook)](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) ·
[MemGPT / Letta memory blocks](https://www.letta.com/blog/memory-blocks/) ·
[Manus context engineering](https://rlancemartin.github.io/2025/10/15/manus/)
