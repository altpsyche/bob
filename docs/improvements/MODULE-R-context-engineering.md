# Module R — Context Engineering (rerank, self-editing memory, conversation paging)

> **Status:** ✅ COMPLETE (2026-07-09). All three sub-items shipped as small, gate-green increments
> (suite 1024 green; `scripts/check.py` exit 0) and **live-verified** on the local stack. User-facing
> docs: [docs/MEMORY.md](../MEMORY.md). All features are config-gated, default-off (R off ==
> pre-R behavior, byte-identical). No development-phase markers in code/tests per the hard conventions.
>
> **Per-item outcomes:**
> - **R1 — rerank + contextual-chunk embeddings.** `bob_memory._rerank_scores` + a rerank pass inside
>   `_recall_hybrid` (both the fused and dense-fallback branches), reusing the fused candidate set —
>   no second retrieval; the cross-encoder score is min-max-normalized and re-blended so recency/type/
>   salience still apply. `rerank` implies the hybrid path. The reranker is a local `reranking: true`
>   model served by llama-swap (LiteLLM's `/rerank` wants a cloud provider), reached at the endpoint's
>   `/v1/rerank`; `memory.rerankBaseUrl` overrides. Absent reranker → one warning, fall back to hybrid.
>   Contextual-chunk seam: `store(..., context=)` situates the embedding without changing stored text
>   (consumed later by Module Q's code index). Generator gained `reranking → --reranking`. Config:
>   `memory.rerank` / `rerankTopN` / `rerankBaseUrl`. Live: under a recall threshold, hybrid injects
>   noise (coffee-machine/standup at ~1.7) while hybrid+rerank returns only the real answer.
>   **Packaging:** the reranker (`bge-reranker-v2-m3`) **ships in every GPU profile** in
>   `config/models.json` + `versions.lock`, but as an **on-demand** model — `ttl`-based, NOT pinned and
>   NOT in the swap group — so it loads only when a recall actually reranks and releases when idle (zero
>   VRAM + no startup dependency by default; `memory.rerank` is off by default). Enable = one flag
>   (`memory.rerank: true`); an existing install runs `bob fetch` once to pull it. First cut wrongly
>   shipped it **pinned**, which force-loaded it at startup + forced the download for a default-off
>   feature — fixed to on-demand. Also fixed a latent resolver bug found along the way: `user.json` is one
>   file shared with the model-registry resolver, so its catalog-only sections (`profiles`/`peers`/
>   `defaults`/…) were leaking into the runtime config; `resolve_runtime_config` now drops them (guarded
>   by `test_config_resolver`).
> - **R2 — self-editing memory blocks.** Dedicated `core_blocks` table + `block_get/set/list`
>   (kept out of the decaying `memories` table); the Layer-1 `memory_block` tool (append/replace,
>   `MUTATING_TOOLS`); injected every root turn through the **existing** `inject_blocks` +
>   `budget_injection` seam at top priority (deterministic/sorted → prefix-cache-stable). Config:
>   `memory.coreBlocks` (name → char cap). Live: the agent wrote a fact to its `task` block; a fresh
>   process answered from the injected block with no tool call.
> - **R3 — conversation paging.** Dedicated `transcript` table (lazy FTS floor + best-effort
>   embedding) with `transcript_append`/`transcript_search`; a capture seam in `bob_loop` (gated by
>   `agent.conversationPaging`) persists user/assistant/**tool** turns as they happen — including the
>   stateless CLI path — before compaction can drop them; the read-only Layer-1 `conversation_search`
>   tool pages matches back (bounded by the tool-result retention seam). P1 overlap resolved: transcript
>   in the memory DB (needs retrieval); P1's run-checkpoint stays a separate future `sessions.db` table.
>   Live: a paraphrase with no keyword overlap paged the earlier exchange back; owner isolation held.
>
> **Behavior-named tests added:** `tests/test_rerank.py`, `tests/test_core_blocks.py`,
> `tests/test_conversation_search.py`, plus cases in `test_memory.py` / `test_agent_loop.py` /
> `test_generate.py`. Every increment asserts "feature off == byte-identical to pre-R".

**Original plan (below) — depended on features that have all shipped:** summarize-compaction (`scripts/bob_loop.py:809`, frame at `667-677`),
prefix-cache-aware layout (`scripts/bob_loop.py:678`), hybrid dense+BM25/RRF recall
(`scripts/bob_memory.py:463`, RRF fusion in `_recall_hybrid` `bob_memory.py:421-460`, FTS5 in
`_ensure_fts` `bob_memory.py:372`), the tool-result retention seam (`scripts/bob_loop.py:780`,
`scripts/tools/tool_registry.py:287`, `scripts/tools/read_result.py`), and the typed,
owner/project-scoped memory + persisted-session layer. **Read first:**
[MODULE-O-frontier-class.md](MODULE-O-frontier-class.md)'s "Beyond O/P" section (Domain 4) and
[docs/MEMORY.md](../MEMORY.md).

**Why this module exists.** Bob's long-term memory *storage/ranking* is already strong — typed facts
with recency·importance·type·usage·salience weighting and per-type half-life decay
([scripts/bob_memory.py](../../scripts/bob_memory.py)), and `memory_store`/`memory_recall` are
model-callable, so Bob is already "MemGPT-lite." Hybrid **retrieval** (dense + BM25 via RRF) and the
**tool-result retention seam** already shipped. What remains — the *heavy tail* of the context-engineering
frontier (Anthropic's context-editing work, MemGPT/Letta, Manus) — is the expensive, higher-risk part
that O deliberately deferred:
- **retrieval quality** past hybrid: a cross-encoder **rerank** and **contextual-chunk** embeddings
  (Anthropic reports hybrid alone cuts failed retrievals ~49%, **~67% with rerank**);
- **agent-managed context**: MemGPT/Letta **self-editing memory blocks** the model edits in its loop,
  and **conversation paging** (`conversation_search`) to pull back turns compaction dropped from context — the OS-inspired
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
Recall fuses dense + BM25 via Reciprocal Rank Fusion — the "hybrid" tier
(`_recall_hybrid` `scripts/bob_memory.py:421-460`). Anthropic's Contextual Retrieval
shows two more levers close most of the remaining gap: a **cross-encoder rerank** of the fused
candidates (hybrid → hybrid+rerank takes failed-retrieval reduction from ~49% to ~67%), and
**contextual-chunk embeddings** (prepend a short chunk-situating context before embedding so a chunk
stands alone). Bob's recall stops at RRF today.

### Change
- **Rerank pass** in the recall path ([scripts/bob_memory.py](../../scripts/bob_memory.py)): take the
  top-N RRF candidates and re-score with a cross-encoder (a small local reranker served alongside the
  embed model, or a scored LLM pass) → top-K. Gated by a new `memory.rerank` key (default off; the
  `memory.retrieval` + `memory.rrfK` config seams already ship in `config/defaults.json`, the new
  `memory.rerank` key does not exist yet).
- **Contextual-chunk embeddings**: when a stored memory is a chunk of a larger source (project docs,
  future code-RAG for **Module Q**), prepend a one-line context before embedding so retrieval doesn't
  depend on the chunk being self-contained. Atomic typed facts (today's memories) already stand alone,
  so this matters most once chunked sources exist — build the seam here, exploit it in Module Q's code index.
- Reranker/model resolves via the existing role routing; absent → clean fallback to hybrid (loud-fail).
- Config: `memory.rerank` (default off), `memory.rerankTopN`.

### Effort: 5–7 h (reuses the hybrid recall fused candidate set + the embed server).
### Acceptance
Tests: rerank reorders a fused candidate set so a semantically-best-but-lexically-weak hit rises;
`rerank` off reproduces today's hybrid recall exactly; a missing reranker falls back to hybrid with a logged
warning; a contextual-chunk embed round-trips. Live (GPU tier): a query that hybrid ranks 4th is
reranked to 1st.

---

## R2 — Self-editing memory blocks (MemGPT/Letta core memory)

### Problem
Bob's memory is *auto*-recalled (hybrid recall) and *auto*-consolidated at session end — the model doesn't
directly curate what stays in its context. MemGPT/Letta give the agent **editable core-memory blocks**
(e.g. a "persona" block and a "user/task" block) it rewrites *in its loop* via tool calls, always
present in context and bounded by a character budget. That's how a Letta agent decides what's worth
keeping without waiting for end-of-session consolidation.

### Change
- **Core-memory blocks**: a small set of named, size-capped blocks (e.g. `task`, `user`) persisted per
  owner/scope (reuse the memory store), **always injected** into context (via the existing injection
  path + the prefix-cache-aware layout `scripts/bob_loop.py:678` so an edited block re-freezes cleanly
  into the prefix).
- Layer-1 **`memory_block` tool** (`append` / `replace` / `read`): the model edits a block mid-run;
  edits are mutating → the mutating-tool `ask`/policy applies, and audited. Bounded so a block can't
  grow the prefix unboundedly.
- Blocks are owner/scope-scoped and survive across sessions; they complement — don't replace —
  auto-consolidation (which still fires on real session end only).
- Config: `memory.coreBlocks` (default off), block name → token cap.

### Effort: 6–9 h.
### Acceptance
Tests: the model appends to a block and it persists + re-injects next turn; a block edit is policy-gated +
audited; a block respects its token cap (oldest trimmed / rejected); off = no blocks injected
(byte-identical). Live: across two turns the agent writes a fact to its `task` block and uses it later
without re-deriving it.

---

## R3 — Conversation paging (`conversation_search` over dropped turns)

### Problem
Summarize-compaction (`scripts/bob_loop.py:809`) trims the older span out of the *in-context* message
list per request, and the tool-result retention seam clears stale tool results to a `read_result` stub
— so the model can't see those older turns in its current context window. Two gaps remain: (1) the
compaction only trims what a single request carries, but the loop's intermediate tool turns (and every
CLI run — the CLI path is **stateless**, it has no session store at all) are never persisted as a
searchable transcript; and (2) even where `bob_session` persists the full final user/assistant history
(`append_turn` `scripts/bob_session.py:137-164`), there's no way for the agent to search it and page a
specific earlier exchange back into context. MemGPT/Letta keep an OS-style **recall/archival** tier the
agent searches on demand (`conversation_search`). Bob has no such retrieval path.

### Change
- **Persist the full transcript** for a run/session including the intermediate tool turns and CLI runs.
  `bob_session` (`scripts/bob_session.py:137-164`, `append_turn`) already persists the full final
  user/assistant turn history — but compaction only trims the per-request message list, it doesn't
  delete stored turns, and the CLI path is stateless. So the work is to capture the complete
  transcript (intermediate tool turns + stateless CLI runs) into an owner-scoped store.
- Layer-1 **`conversation_search` tool**: semantic/keyword search over the persisted transcript
  (reuse the hybrid retriever over transcript rows), returning matching exchanges the model can page
  back into context on demand — the read side mirrors the `read_result` retention seam
  ([scripts/tools/tool_registry.py](../../scripts/tools/tool_registry.py):287, `scripts/tools/read_result.py`),
  but over turns rather than tool outputs.
- Bounded: a paged-in result is itself subject to compaction / tool-result clearing so it can't
  re-overflow; paging is opt-in and metered / traced.
- Config: `agent.conversationPaging` (default off).

### Effort: 5–7 h.
### Acceptance
Tests: a compacted-away turn is retrievable by `conversation_search`; the retriever is owner-scoped
(no cross-owner leak); a paged-in result is re-subject to compaction; off = no transcript persistence
beyond today. Live: a long run compacts an early decision, and the agent pages it back
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
| new `scripts/tools/memory_block.py`; `scripts/bob_loop.py` (block injection + stable prefix `bob_loop.py:678`) | R2 |
| new `scripts/tools/conversation_search.py`; `scripts/bob_session.py` (persist full transcript incl. tool turns + CLI runs) | R3 |
| `config/defaults.json` (`runtime.memory.*` / `runtime.agent.*` keys), `tests/*` | all |

## Verification

- Python `py_compile` + unittest; the `scripts/check.py` gate; the CI matrix; the eval suite gains
  a compaction-recall / retrieval-quality task.
- New config keys under `config/defaults.json` `runtime.memory.*` / `runtime.agent.*`, read via
  `.get(default)`, defaulting to today's behavior (R off = current hybrid-recall / compaction /
  tool-result-clearing behavior unchanged).
- Live: rerank lift on a GPU-tier query (R1); a cross-turn core-memory edit (R2); page back a compacted
  decision (R3).
- Cite `file:line` for every claim.

## Non-goals

Replacing hybrid recall (R1 *extends* it) or compaction / tool-result clearing (R3 *complements* them —
it doesn't stop compaction, it makes it reversible). A separate vector database or memory service
(everything stays on the existing SQLite + embed-server infra, local-first). The prefix-cache mechanics
themselves (`scripts/bob_loop.py:678` owns them; R2 reuses the stable-prefix seam). Code-specific
retrieval (repo map / code index is **Module Q**; R1's contextual-chunk seam is what Q's index consumes).

## Sources

[Anthropic: contextual retrieval (hybrid + rerank)](https://www.anthropic.com/engineering/contextual-retrieval) ·
[Anthropic: effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) ·
[Anthropic: context editing / tool clearing (cookbook)](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) ·
[MemGPT / Letta memory blocks](https://www.letta.com/blog/memory-blocks/) ·
[Manus context engineering](https://rlancemartin.github.io/2025/10/15/manus/)
