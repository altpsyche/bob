# Module MEM2 — Project-scoped memory + frontier-parity quality pass

> **Status:** ✅ COMPLETE (2026-07-03). All phases landed, gated (check.ps1 + test-dry-run.ps1 + unittests), and live-verified on branch `feat/memory-redesign`. Commits: MEM-7 `bb871fa`, MEM-8 `13cc87c`, MEM-9 `334a940`, MEM-10 `02a2711`, MEM-11 (this doc note + the `truncate_history`/`summarize_turns` seam comments). Follows MODULE-NE6-MEM (WI-6 + MEM-0..6). Phase numbering: **MEM-7 … MEM-11**.
>
> **Per-phase outcomes:**
> - **MEM-7** ✅ project scoping (`bob_core.project_key` + `RunContext.scope`) + `BOB.md` files; fixed A1 (autoRecall owner).
> - **MEM-8** ✅ conflict-aware consolidation (reconcile: NEW / `REPLACES:<id>` → `superseded_by`); fixed B6 (prose recap + deterministic fallback); `memory.reconcileTopK`.
> - **MEM-9** ✅ importance 1-10 → `salience`; `wSalience` recall term; recency off `max(created_at,last_used)`; `bob memory pin|unpin`; auto-pin high-importance profile. Bonus: revived dead `maxSummaryTokens` (256→512) — the `chat` role is a reasoning model that emptied the completion at 256.
> - **MEM-10** ✅ injection budget (`bob_core.budget_injection` + `memory.maxInjectedTokens`); provenance (`source_session` **v3 migration** + `forget --session`); timestamp fixes A3/A4.
> - **MEM-11** ✅ doc-only: the compaction seam is documented at `truncate_history` (bob_loop) and `summarize_turns` (bob_memory) — in-run context compaction is **Module O3**, no code here.

---

## Context

The memory redesign (MEM-0..6) shipped and was live-verified: typed/owner-scoped store, blended recall, once-per-session profile injection, consolidation, hygiene. Two things prompted this follow-on:

1. **The concrete question:** *"When I use the terminal in project 1's folder, does project 1 have its own memory?"* — Today **no**. The `scope` column, the `WHERE scope IS NULL OR scope=?` recall filter, and the `scopeByProject` flag all exist but are **inert** — no caller computes a project key or passes `scope` (`scopeByProject` in [config/defaults.json](../../config/defaults.json) has zero readers). Everything lands in one shared pool (owner=`local`).
2. **"Make sure we nail the memory system"** — a fresh audit + a frontier comparison (mem0, Letta/MemGPT, Zep/Graphiti, Stanford Generative Agents, Claude Code) surfaced parity gaps and two correctness bugs worth closing now.

**Decisions taken (clarifying Qs):**
- **Project memory = BOTH** — DB scoping (auto-learned project facts, mem0-style) **and** a human-editable per-repo `BOB.md` (Claude Code-style, git-committable).
- **All quality gaps in scope** — conflict resolution, importance scoring, injection budget + provenance, compaction boundary note.

**Intended outcome:** repo-specific facts stay isolated per project (identity/prefs stay global); contradictory facts get superseded not accumulated; importance is a live ranking signal; injected memory can't overflow the context window; every fact is traceable to its session; Bob reads a curated per-project `BOB.md`.

---

## Frontier comparison (grounding)

| Capability | mem0 | Letta/MemGPT | Zep/Graphiti | Gen Agents | Claude Code | **Bob today** |
|---|---|---|---|---|---|---|
| Long-term store | vector(+graph) | archival vector | temporal KG | memory stream | markdown files | SQLite+embed ✅ |
| Conflict handling | LLM **ADD/UPDATE/DELETE/NOOP** on top-k similar | self-edit replace | **bi-temporal invalidate** (supersede, keep) | — | manual review | ❌ both persist → MEM-8 |
| Importance | — | — | — | LLM **1-10 → /10** in retrieval score | — | ❌ salience frozen 1.0 → MEM-9 |
| Recall rank | cosine | — | — | **recency+importance+relevance** | — | recency+type+usage+cosine (+salience → MEM-9) |
| Scoping | user/agent/run/app IDs | blocks | graph groups | — | enterprise/user/**project**/local + path rules | owner ✅; project ❌ → MEM-7 |
| Provenance | created/updated_at | — | **episode-level** ✅ | — | per-repo dir | ❌ `source` tag only → MEM-10 |
| Context pressure | — | **recursive summary** | — | — | `/compact` re-reads files | ❌ truncate-only → MEM-11/O3 |
| Human-editable | — | blocks | — | — | **CLAUDE.md** hierarchy ✅ | ❌ DB only → MEM-7b |

**Sources:** mem0 update mechanism (Dwarves breakdown), MemGPT/Letta virtual context, Zep temporal KG (arXiv 2501.13956), Generative Agents (arXiv 2304.03442 — score = recency·`exp(-Δt/72h)` + importance/10 + relevance; reflection at a cumulative-importance threshold, boosted to `max(imp,6)`), Claude Code memory docs (walk-up dir tree, concat broad→specific, `@imports`, path-scoped rules, auto-memory as a separate machine-local `MEMORY.md`).

Bob's blended recall is already the Generative-Agents lineage — it's just **missing the importance term** (MEM-9) and the **scoping/conflict/provenance** layers the others have.

---

## Gap catalog (from the code audit — prioritized)

**Correctness bugs (fix early):**
- **A1** `autoRecall` calls `memory_recall(goal, config=config)` with no owner *and runs before* `owner` is resolved ([bob_loop.py:665](../../scripts/bob_loop.py#L665) vs the resolve just below) → multi-owner server recalls the wrong/empty set. **blocker** → MEM-7.
- **A2** contradictory facts both persist — `store` dedups by hash + near-cosine only; consolidation never supersedes ([bob_memory.py](../../scripts/bob_memory.py)). **blocker** → MEM-8.
- **A3** `_age_days` returns `0.0` for a corrupt/missing timestamp → `decay=exp(0)=1.0`, ranking garbage as *freshest* → MEM-10.
- **A4** `prune` string-compares `created_at < cutoff` across mixed timestamp formats (space vs `T`) → legacy rows over-pruned → MEM-10.

**Half-wired / decorative (the chosen gaps):**
- **B1** `scope` column + `scopeByProject` + `idx_mem_scope` inert (no prod writer/reader) → MEM-7.
- **B2** `salience` never varies from 1.0 and is **not a recall term** (only tie-breaks prune/profile) → decorative → MEM-9.
- **B3** no session/run provenance; `source` is coarse free-text → MEM-10.
- **B4** injected memory is unbounded and untrimmable — profile + autoRecall + tool schemas all concat into the one system message `truncate_history` always keeps ([bob_loop.py](../../scripts/bob_loop.py)); autoRecall has no size cap → MEM-10.
- **B5** `pinned` can never become 1 (no pin/unpin surface) → prune/profile pin logic is a no-op → MEM-9.
- **B6** consolidation episodic recap stores the raw `type: statement` blob; a down summarizer silently drops the whole session → MEM-8.

**Parked** (see Non-goals): `subject` constant, `tags` unused in ranking, `type_filter`/`--owner` not on the tool/recall CLI, exact-dedup ignores type, prune counts inactive rows, `maxSummaryTokens`/`autoSummarize` dead keys, tools don't swallow embed outage, procedural/reflection memory.

---

## Phased plan (each independently landable + unittest-tested, gate-green after each)

### MEM-7 — Project scoping (DB) + `BOB.md` file  *(Both)*

**7a — DB project scoping.** `type='project'` facts per-repo; identity/prefs stay global.
- `project_key(cwd) -> str|None` in `bob_core` (pure-Python upward `.git` walk → repo root, else cwd; `None` when `memory.scopeByProject` off). **No git subprocess** (CONTRIBUTING).
- Add **`scope`** to `RunContext` ([bob_loop.py:189](../../scripts/bob_loop.py#L189)) beside `owner`/`agent_depth` (additive) — mirrors MEM-6 owner threading.
- Thread `scope` from shell (launch cwd → project_key) and server (None) into `run_agent_events(scope=…)` → `RunContext.scope` → dispatch contextvar → memory tools read it (`_run_scope()` like `_run_owner()`, [tools/memory.py](../../scripts/tools/memory.py)).
- `bob_core.memory_store`/`memory_recall` gain `scope`; `store` stamps it on `type='project'` writes; `recall` passes it (filter already in [bob_memory.py](../../scripts/bob_memory.py)); consolidation stamps project-typed facts.
- **Wire `scopeByProject`**; off → global (today's behavior).
- **Fix A1**: move `owner` resolution above the autoRecall block and pass `owner`(+`scope`) into autoRecall's `memory_recall`.
- *Tests:* A-facts not recalled under B scope; `scope IS NULL` always recalled; flag off → global; autoRecall uses run owner (A1 regression).

**7b — `BOB.md` file** (Claude Code-style, human-curated).
- Walk up from project dir: `.bob/BOB.md` + `BOB.md` at each level, plus `~/.bob/BOB.md` (via `osenv`); also read `AGENTS.md` if present (cross-tool interop). Concatenate broad→specific.
- `project_memory_block(project_dir, config) -> str|None` in `bob_core`; inject at session start alongside the MEM-3 profile block (history-empty + `agent_depth==0`), own frame "Project instructions (from BOB.md):". Capped by `memory.bobMdMaxTokens` (~4000). Gated by `memory.projectFiles` (default true).
- Shell passes launch cwd; server passes None.
- *Tests:* precedence order; missing → None; cap enforced; injected once; disabled → skipped.

### MEM-8 — Conflict-aware consolidation (supersede, don't accumulate)  *(A2/B6)*
- Upgrade `consolidate_session` from dedup-only to **reconcile**, one LLM call: retrieve the owner's top-K existing active facts, pass into the extraction prompt, model emits each fact tagged `NEW` or `REPLACES:<id>`. `REPLACES` → insert new + set old row's **`superseded_by`** (invalidate-not-delete; column already used by `edit()`). Ambiguous → `NEW` (conservative).
- Tool-path `memory_store` stays dedup-only; reconciliation is the consolidation job.
- **Fix B6:** store a real prose recap (no `type:` prefixes); on summarizer failure store a deterministic recap (first user line + turn count), not silence.
- *Tests:* "uses vim"→"uses vscode" supersedes (only new recalled); genuinely-new is ADDed; re-run idempotent; summarizer-down still writes a recap.

### MEM-9 — Importance scoring + live salience/decay  *(B2/B5)*
- Extraction rates **importance 1-10** per fact (Gen Agents rubric); `salience = importance/10`; thread `salience` through `bob_core.memory_store`/consolidation (never passed today).
- Add **`wSalience`** term to the recall blend ([bob_memory.py](../../scripts/bob_memory.py) `recall`); config `memory.ranking.wSalience` (~0.3) → Gen-Agents parity.
- Recency uses `max(created_at, last_used)` (age since last *access*) so reinforced facts stop decaying (`last_used` written today, never read).
- **Fix B5:** `bob memory pin <id>`/`unpin <id>` CLI (+ bob.ps1 forward); consolidation may pin high-importance `profile` facts.
- *Tests:* high-importance outranks low at equal cosine/recency; salience persisted; recalled-recently old fact outranks never-touched same-age; pin/unpin + survives prune.

### MEM-10 — Injection budget + provenance + timestamp fixes  *(A3/A4/B3/B4)*
- **Budget:** cap profile + autoRecall + BOB.md blocks against `memory.maxInjectedTokens` (~1200) before concatenating into the system prompt; trim episodic/autoRecall before profile before BOB.md. Cap autoRecall length (B4).
- **Provenance (B3):** `source_session` column (v3 migration, same `user_version` gate as MEM-0, bump to 3); consolidation stamps the originating session id; surface in `show`/`export`; `bob memory forget --session <id>`.
- **A3:** unparseable timestamp → large age (rank oldest). **A4:** `prune` compares parsed datetimes; exclude superseded/expired from the cap count/victims.
- *Tests:* over-budget trims episodic first, keeps profile+BOB.md; provenance round-trips + `forget --session`; corrupt timestamp ranks last; prune leaves legacy-format rows within TTL.

### MEM-11 — Compaction boundary (doc only)  *(note only)*
- Document that in-run **context compaction** (summarize-the-dropped-span) is **Module O3**, reusing the `summarize_turns` core (MEM-4). MEM-10's injection budget is the memory-side complement; O3 owns the rolling transcript. No code.

---

## Sequenced roadmap (gate-green after each)

```
MEM-7 (scoping + BOB.md, fixes A1) → MEM-8 (conflict, fixes B6) → MEM-9 (importance + salience/decay, B5) → MEM-10 (budget + provenance, A3/A4/B4) → MEM-11 (doc)
```
MEM-7 lands `scope`/project-dir threading + the A1 fix (one edit to the loop's owner/scope resolution). MEM-8 then MEM-9 both edit the consolidation/extraction prompt — conflict first, importance layered on. MEM-10 is loop-injection + a v3 migration + the timestamp bugs. MEM-11 is docs.

**Gate rule:** `check.ps1` (0) · `test-dry-run.ps1` (green) · `unittest discover -s tests` (Win+Linux). Regenerate `config/verbs.json` if a `bob memory` subcommand is added (pin/unpin, forget --session). New config keys land in `config/defaults.json` `runtime.memory`, read via `.get()`.

---

## Non-goals
- Reflection / higher-order synthesis (revisit after O).
- Procedural / agent-action memory (O9 tracing territory).
- Vector DB / ANN / graph memory (Qdrant = Module F).
- In-run context compaction (Module O3; MEM-11 notes the seam).
- Changing the embed model.
- Parked audit items (fix opportunistically): `subject` beyond `'user'`, tag ranking, `type_filter`/`--owner` on the recall tool, type-aware exact-dedup, `maxSummaryTokens`/`autoSummarize` dead keys, tool-level embed-outage swallowing.

## Risks
- **git-root without subprocess** — pure-Python upward `.git` walk; fallback cwd.
- **BOB.md discovery** — cap count/size, read only within project root + user dir; no `@import` recursion in v1.
- **v3 migration** — additive `ALTER`, `user_version` gate, idempotent.
- **False supersede (MEM-8)** — keep the superseded row (recoverable via `list --all`/`export`); ambiguous → `NEW`.
- **Consolidation cost** — top-K retrieval + richer prompt, still one call, bounded by `consolidateTimeout`.

## Open questions (recommendations; non-blocking)
1. Project key = git root else cwd (recommended).
2. `BOB.md` at `.bob/BOB.md` + `~/.bob/BOB.md`, also read `AGENTS.md` (recommended).
3. Server stays global/owner-scoped for v1 (no user cwd); revisit if the API grows a per-request `project`.

## Verification (end to end)
1. Gates green after each phase.
2. **Live smoke:** project A vs B recall isolation (`bob memory list`); `BOB.md` injected at session start; contradiction supersedes (`bob memory show <id>`); high-importance outranks low (`bob recall`); `bob memory forget --session <id>` hides that session's facts.
3. Cite `file:line` for every change.
