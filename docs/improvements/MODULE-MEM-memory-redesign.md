# Bob Memory System Redesign

Status: draft (2026-07-03). Depends on: N (owner-scoped sessions), NB (config/defaults.json, osenv seams). Integrates with: NE5 (in-shell sessions + memory continuity) and WI-6 session persistence.

---

## 1. Problem statement

Bob's memory works but has four concrete defects, all visible in the current code:

1. **Notes are stored first-person and surfaced ambiguously.** The four rows actually in `data/bob.db` are:
   - `1 | "I prefer dark mode in all editors"`
   - `2 | "I work on game dev and AI tooling"`
   - `3 | "My preferred shell is PowerShell 7"`
   - `4 | "I use Claude Code as my primary coding assistant"`

   `recall()` returns the raw `content` ([scripts/bob_memory.py:172](../../scripts/bob_memory.py#L172)), `bob_core.memory_recall` newline-joins it ([scripts/bob_core.py:166](../../scripts/bob_core.py#L166)), and the model reads first-person text as *its own* identity. The tool layer papers over this with a runtime frame — `"Saved notes about the user (context only; do not recite)"` ([scripts/tools/memory.py](../../scripts/tools/memory.py)) — but the underlying data is still wrong, and the `autoRecall` injection path re-applies its own frame ([scripts/bob_loop.py](../../scripts/bob_loop.py)). Framing is a band-aid over a normalization bug.

2. **One flat, untyped store.** The `memories` table ([scripts/bob_memory.py:83-93](../../scripts/bob_memory.py#L83)) has no notion of *kind* of memory. A stable identity fact ("works in game dev") and a throwaway session summary get identical retention and identical ranking. There is a separate `profile` key/value table ([scripts/bob_memory.py:94-100](../../scripts/bob_memory.py#L94), written only by `cmd_init_profile` at :273-282) that **no read path ever queries** — dead weight.

3. **Ranking is semantic-only with a magic threshold.** `recall()` scores purely by cosine ([scripts/bob_memory.py:168](../../scripts/bob_memory.py#L168)), filters at a hardcoded `threshold=0.3` (:154), sorts, and truncates to `k` (:173-174). No recency, no type weighting, no salience, no owner scope. `last_used`/`use_count` are *written* (:178-180) but never *read* back into ranking.

4. **Weak, single-surface write path and no hygiene.** Writes come from the `memory_store` tool ([scripts/tools/memory.py](../../scripts/tools/memory.py)) and one end-of-session summarizer, `cmd_summarize_session` ([scripts/bob_memory.py:232-270](../../scripts/bob_memory.py#L232)), which is only invoked from the **legacy PowerShell REPL** `finally` block ([scripts/bob.ps1:629-637](../../scripts/bob.ps1#L629)) — **not** the agent server and **not** the new NE shell ([scripts/bob/shell.py](../../scripts/bob/shell.py) keeps history in-memory, defers persistence to WI-6). There is no decay, no size cap, no forget, and no user-visible inspect/edit — `bob memory` only exposes `status|clear` ([scripts/bob.ps1:906-914](../../scripts/bob.ps1#L906)).

The good news: the **sporadic-by-default architecture is already correct**. `enabled` gates whether the tools load ([scripts/tools/memory.py](../../scripts/tools/memory.py), honored by the registry at [scripts/tools/tool_registry.py](../../scripts/tools/tool_registry.py)); `autoRecall` (default `false`, [config/defaults.json](../../config/defaults.json)) separately gates the heavy per-turn injection ([scripts/bob_loop.py](../../scripts/bob_loop.py)). This redesign **keeps that split** and builds the rest of the system to match it.

---

## 2. Proposed model

### 2.1 Decision: single table, metadata-typed (not multiple stores)

**Recommendation: one `memories` table with a `type` column**, not per-type tables and not a document/graph store.

Justification, grounded in the code:
- The existing read path is a single full-table scan with an in-Python cosine ([scripts/bob_memory.py:161-173](../../scripts/bob_memory.py#L161)). A `type` column lets ranking apply per-type weights and lets injection select `WHERE type IN (...)` — with **zero join complexity** and a trivial `ALTER TABLE` migration (§7).
- The DB is personal and small (4 rows today; capped at a few thousand — §6). Brute-force cosine over one owner's rows is fine; a vector index (Qdrant, Module F) is a non-goal here.
- The dead `profile` table ([scripts/bob_memory.py:94-100](../../scripts/bob_memory.py#L94)) collapses into `type='profile'` rows, removing a table that nothing reads.

### 2.2 Memory types (closed set)

| `type` | Meaning | Retention | Injection policy |
|--------|---------|-----------|------------------|
| `profile` | Durable identity ("User works in game dev") | Never decays | Injected **once** at session start |
| `preference` | Stable preferences ("User prefers dark mode") | Never decays | Injected once at session start |
| `project` | Context tied to a repo/cwd | Decays slowly, scoped | Tool-recall only, filtered by `scope` |
| `fact` | General durable fact | Decays slowly | Tool-recall only |
| `episodic` | Session summaries | Decays fast, prunable | Tool-recall only |

`profile` + `preference` are the small, stable "user profile" (§4). Everything else is tool-driven/sporadic.

### 2.3 Third-person normalization (write-time, not read-time)

Every write normalizes content to third person and stores a `subject` (default `'user'`), so recalled text is never mistakable for Bob's identity — the read-time frame in the tool and `autoRecall` path becomes belt-and-suspenders instead of the only defense.

Two-tier normalization (cheap-first, no new hard dependency):
- **Deterministic fast path** for the common leading-pronoun cases (covers all four existing rows): `^I'm ` -> `User is `; `^I ` -> `User `; `^My ` -> `User's `; `^I've ` -> `User has `; standalone ` my ` -> ` the user's `. Applied in `store()` before embedding.
- **LLM path only where a call already happens**: the consolidation/extraction prompt (§5) is instructed to emit third-person bullets directly, so no extra round-trip.

This keeps writes dependency-light per CONTRIBUTING §2 (boundary functions stay cheap; no new network call on the hot `memory_store` path).

---

## 3. Read path

Replace the semantic-only scorer in `recall()` ([scripts/bob_memory.py:154-182](../../scripts/bob_memory.py#L154)) with a blended score, an owner/scope prefilter, and a single relevance threshold.

**Prefilter (SQL, before scoring):** `WHERE owner_id = ? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > now)` and, when a project scope is supplied, `AND (scope IS NULL OR scope = ?)`. This closes the current cross-owner leak (today `recall()` scans *all* rows unconditionally, :161) and skips soft-deleted/expired rows.

**Blended score per candidate:**

```
score = wSemantic * cosine
      + wRecency  * exp(-age_days / halfLifeDays[type])
      + wType     * typeWeights[type]
      + wUsage    * min(use_count / 10, 1.0)
```

- `cosine` unchanged ([scripts/bob_memory.py:116-122](../../scripts/bob_memory.py#L116)).
- `age_days` from `created_at`; `halfLifeDays` per type (profile ~never; episodic ~30). Recency uses data already written but never read (:178-180).
- Weights and half-lives come from config (§8), defaulting to `wSemantic=1.0, wRecency=0.3, wType=0.2, wUsage=0.1`.

**Threshold:** filter on the **blended** score against `memory.recallThreshold` (default `0.35`), replacing the hardcoded semantic-only `0.3` (:154). Sort desc, take `k`, then bump `last_used`/`use_count` on the hits exactly as today (:175-181).

`bob_core.memory_recall` ([scripts/bob_core.py:156-166](../../scripts/bob_core.py#L156)) gains optional `owner` and `scope` params (default `owner='local'` from `agent.defaultOwner`) and passes them through. The tool wrapper's frame stays.

---

## 4. Session-start profile injection — evaluated, recommend YES (minimal)

**Question posed:** a small stable profile injected once at session start, vs pure tool-driven recall.

**Recommendation: inject once, minimally, behind a new key `memory.injectProfileAtStart` (default `true`), kept distinct from `autoRecall`.**

- **Why worth it:** identity facts (name, role, shell, primary tools) are relevant to nearly every session but the model shouldn't have to spend a `memory_recall` tool call to get them. A one-time injection of `type IN ('profile','preference')` — capped at `memory.profileMaxTokens` (default 200) and top ~5 by salience — costs one embedding-free SQL read per *session*, not per turn. This is strictly cheaper than `autoRecall` ([scripts/bob_loop.py](../../scripts/bob_loop.py) runs an embed on **every** turn).
- **Why not more:** everything non-identity stays sporadic/tool-driven, preserving design goal #1.
- **Where:** the injection belongs at **session construction**, not in the per-turn loop. In the server/NE path a session is created once ([scripts/bob_agent_server.py](../../scripts/bob_agent_server.py), [scripts/bob/shell.py](../../scripts/bob/shell.py)), so inject when history is empty. Concretely: a helper `memory_profile_block(owner, config) -> str|None` prepended to the system prompt on the first turn of a session (history empty in `run_agent_events`), framed third-person: `"Stable facts about the user (context; not your identity):\n- ..."`.
- Keep `autoRecall` as-is (default off) — the two mechanisms are orthogonal and both should exist.

---

## 5. Write / consolidation path

### 5.1 Explicit `memory_store` (typed)

`store()` ([scripts/bob_memory.py:130-151](../../scripts/bob_memory.py#L130)) gains `type`, `owner`, `scope`, `tags`, `salience`, and applies §2.3 normalization before embedding. Dedup gets two tiers:
- **Exact:** a `content_hash` (sha256 of normalized content) lookup — an O(1) short-circuit before the current O(n) embedding scan.
- **Near:** the existing cosine >= threshold scan (:139-141), but scoped to the same `(owner, type)` and using `memory.dedupThreshold` (default `0.92`) instead of the hardcoded `0.95` (:141). The best-effort race note (:136) still applies.

The `memory_store` tool schema ([scripts/tools/memory.py](../../scripts/tools/memory.py)) adds an optional `type` param (enum) and keeps the "note ABOUT THE USER" description. `bob_core.memory_store` ([scripts/bob_core.py:145-153](../../scripts/bob_core.py#L145)) threads the new args.

### 5.2 End-of-session consolidation (build on `cmd_summarize_session`)

Generalize `cmd_summarize_session` ([scripts/bob_memory.py:232-270](../../scripts/bob_memory.py#L232)) into `cmd_consolidate_session`:
- Reuse the LLM-call plumbing (:253-265) but change the prompt (:242-251) to **extract durable, third-person, typed facts** ("Return 0-5 bullets of durable facts about the user, each as `type: third-person statement`; omit ephemeral chatter"), rather than a free-text summary.
- For each extracted fact: normalize (§2.3, mostly a no-op since the LLM emits third-person), then `store(..., source='consolidation')` which **dedups against existing** via §5.1 — so repeated sessions don't accumulate duplicates.
- Store the raw summary itself as one `type='episodic'` row (preserving today's behavior at :267), which will decay/prune.

Gated by a new `memory.autoConsolidate` (default `true`), alongside the existing `autoSummarize` ([config/defaults.json](../../config/defaults.json)).

### 5.3 Firing it everywhere (not just the PS REPL)

Today consolidation fires **only** in [scripts/bob.ps1:629-637](../../scripts/bob.ps1#L629). Wire the same hook into the two modern surfaces:
- **Agent server:** on session delete/idle, and reuse the token-accounting seam near `_record_turn` ([scripts/bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)).
- **NE shell:** on `/exit` (NE5, [docs/improvements/MODULE-NE-unified-interface.md](MODULE-NE-unified-interface.md) already specifies this), where `shell.py` currently keeps history in-memory only.

Both call the importable core (`cmd_consolidate_session`'s underlying function) directly per CONTRIBUTING §2 — no subprocess.

---

## 6. Hygiene / forgetting

- **Decay** is applied at *read* time via the recency term (§3) — no background job needed.
- **Forget (hard TTL):** `episodic`/`fact` rows past `memory.forgetAfterDays[type]` are pruned; `pinned=1` and `type IN ('profile','preference')` are exempt.
- **Size cap:** `memory.maxRows` (default 2000, per owner). When exceeded, prune lowest `salience`, then oldest `episodic`, never pinned. Run opportunistically at end of consolidation.
- **Soft update:** editing a fact inserts a new row and sets the old row's `superseded_by` (read path already filters `superseded_by IS NULL`, §3) — auditable, non-destructive.
- **User-visible surface** — extend the `bob memory` verb ([scripts/bob.ps1:906-914](../../scripts/bob.ps1#L906)) and CLI ([scripts/bob_memory.py:285-330](../../scripts/bob_memory.py#L285)) with:
  - `bob memory list [--type T]` / `show <id>` — inspect (currently impossible).
  - `bob memory forget <id>` / `bob memory forget --query "..."` — soft-delete.
  - `bob memory edit <id> "new text"` — soft-update (re-embeds).
  - `bob memory export` — dump JSON.
  - `bob remember`/`bob recall` gain `--type`.
  The PowerShell wrapper forwards args unchanged, so only new subparsers in `main()` ([scripts/bob_memory.py:285-330](../../scripts/bob_memory.py#L285)) are needed.

---

## 7. Migration of existing data

Two-step, idempotent, backup-first:

**Step A — schema (automatic, in `get_db`).** Add a `PRAGMA user_version` gate to `get_db` ([scripts/bob_memory.py:78-101](../../scripts/bob_memory.py#L78)). If `user_version < 2`: `ALTER TABLE memories ADD COLUMN` for `type, content_hash, subject, owner_id, scope, tags, salience, pinned, superseded_by, updated_at, expires_at`; backfill `owner_id = agent.defaultOwner`, `subject='user'`, `type='preference'` for legacy `source='user'` rows and `type='episodic'` for `source='session'` rows; create the new indexes; set `user_version=2`. Safe on a fresh DB and re-runnable (mirrors the pattern already used in `SessionStore._ensure_schema`, [scripts/bob_session.py:61-88](../../scripts/bob_session.py#L61)).

**Step B — content normalization (explicit `bob memory migrate --normalize`).** Because rewriting content requires **re-embedding**, this is an explicit command (needs the embed server up, [scripts/bob_memory.py:104-113](../../scripts/bob_memory.py#L104)), and it copies `bob.db` to `bob.db.bak.<ts>` first (CONTRIBUTING §5 atomic-write posture). It applies §2.3 to each row and re-embeds. For the four known rows the deterministic path yields:

| id | before | after | type |
|----|--------|-------|------|
| 1 | I prefer dark mode in all editors | User prefers dark mode in all editors | preference |
| 2 | I work on game dev and AI tooling | User works on game dev and AI tooling | profile |
| 3 | My preferred shell is PowerShell 7 | User's preferred shell is PowerShell 7 | preference |
| 4 | I use Claude Code as my primary coding assistant | User uses Claude Code as their primary coding assistant | preference |

`cmd_init_profile` ([scripts/bob_memory.py:273-282](../../scripts/bob_memory.py#L273)) is redirected to write `type='profile'` rows; the legacy `profile` table is left for one release then dropped.

---

## 8. Config keys (extend the `memory` block)

Extend `config/defaults.json` (both languages read this per CONTRIBUTING §8). New keys marked NEW:

```jsonc
"memory": {
  "enabled": true,
  "autoRecall": false,               // per-turn recall (heavy) — stays OFF
  "injectProfileAtStart": true,      // NEW: inject type in {profile,preference} ONCE per session
  "profileMaxTokens": 200,           // NEW: cap on the once-per-session profile block
  "dbPath": "data/bob.db",
  "embedModel": "embed",
  "recallK": 5,
  "recallThreshold": 0.35,           // NEW: threshold on the BLENDED score (was hardcoded 0.3 semantic-only)
  "dedupThreshold": 0.92,            // NEW: was hardcoded 0.95 in store()
  "maxSummaryTokens": 256,
  "autoSummarize": true,
  "autoConsolidate": true,           // NEW: end-of-session typed fact extraction
  "ranking": {                       // NEW
    "wSemantic": 1.0, "wRecency": 0.3, "wType": 0.2, "wUsage": 0.1,
    "halfLifeDays": { "profile": 36500, "preference": 36500, "project": 90, "fact": 365, "episodic": 30 }
  },
  "typeWeights": {                   // NEW
    "profile": 1.0, "preference": 0.9, "project": 0.8, "fact": 0.7, "episodic": 0.5
  },
  "maxRows": 2000,                   // NEW: per-owner size cap
  "forgetAfterDays": { "episodic": 180 },   // NEW: hard TTL by type
  "scopeByProject": true             // NEW: tag project rows with cwd/repo key; filter recall by scope
}
```

All read via `.get()` with the defaults above so an older `config.json` ([scripts/bob_core.py:75-89](../../scripts/bob_core.py#L75)) still works.

---

## 9. Proposed SQLite schema (v2)

```sql
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY,
    content       TEXT NOT NULL,                 -- third-person normalized
    content_hash  TEXT,                          -- sha256(normalized content) — exact-dedup fast path
    embedding     TEXT NOT NULL,                 -- JSON float array (BGE-M3, model="embed")
    type          TEXT NOT NULL DEFAULT 'fact',  -- profile|preference|project|fact|episodic
    subject       TEXT NOT NULL DEFAULT 'user',
    owner_id      TEXT NOT NULL DEFAULT 'local', -- scope (N1); default = agent.defaultOwner
    scope         TEXT,                          -- optional project/cwd key for type='project'
    source        TEXT DEFAULT 'user',           -- user|session|consolidation|import
    tags          TEXT,
    salience      REAL NOT NULL DEFAULT 1.0,
    pinned        INTEGER NOT NULL DEFAULT 0,
    superseded_by INTEGER,                        -- id of the row that replaces this (soft update)
    created_at    TEXT DEFAULT (datetime('now')),
    updated_at    TEXT,
    last_used     TEXT,
    use_count     INTEGER DEFAULT 0,
    expires_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_owner_type ON memories(owner_id, type);
CREATE INDEX IF NOT EXISTS idx_mem_hash       ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_mem_scope      ON memories(owner_id, scope);
CREATE INDEX IF NOT EXISTS idx_mem_active     ON memories(owner_id, superseded_by);
-- PRAGMA user_version = 2;   -- migration gate (see §7)
```

Embedding stays JSON `TEXT` scanned in Python ([scripts/bob_memory.py:161-173](../../scripts/bob_memory.py#L161)) — the `owner_id`/`superseded_by` prefilter bounds the scan; no vector-index dependency added.

---

## 10. Tool / description changes

- `scripts/tools/memory.py`:
  - `memory_store` schema: add optional `type` enum param; keep the "ABOUT THE USER" wording.
  - `memory_recall`: pass `owner`/`scope`; keep the third-person frame — now redundant with write-time normalization but retained as defense-in-depth. Keep the "don't call for greetings/small talk" guidance.
  - `enabled()`/`configure()` unchanged.
- `scripts/bob_loop.py`: add the once-per-session profile injection (§4) near the history-seed point; leave the `autoRecall` block untouched. Thread `owner` for recall — note `RunContext` ([scripts/bob_loop.py:189-201](../../scripts/bob_loop.py#L189)) currently carries no owner; add an `owner` slot so a future recall-with-owner and server owner ([scripts/bob_agent_server.py](../../scripts/bob_agent_server.py)) align.

---

## 11. Phased work-item breakdown

Each phase is independently landable with tests (stdlib `unittest`, mocked embed — the existing pattern in [tests/test_memory.py](../../tests/test_memory.py)).

**MEM-0 — Schema v2 + migration foundation.** `user_version` gate + `ALTER TABLE` backfill in `get_db`; `bob memory migrate --normalize` (backup + deterministic rewrite + re-embed) for content (§7). *Tests:* fresh DB has v2 columns + `user_version=2`; a seeded legacy DB (the 4 first-person rows) migrates idempotently; normalization produces the §7 table.

**MEM-1 — Typed write path.** `store()` gains `type/owner/scope/tags/salience`, §2.3 normalization, `content_hash` exact-dedup, `(owner,type)`-scoped near-dedup at `dedupThreshold`. *Tests:* pronoun rewrite; exact-dedup short-circuit; cosine near-dedup respects scope; type persisted.

**MEM-2 — Blended read path.** Rewrite `recall()` scoring (§3) with owner/scope prefilter, recency/type/usage terms, blended threshold. Thread `owner`/`scope` through `bob_core.memory_recall`. *Tests:* newer row outranks older at equal cosine; owner isolation (owner B can't recall owner A); below-threshold filtered; type weight tie-break.

**MEM-3 — Session-start profile injection.** `memory_profile_block()` + wire into first-turn system prompt (§4); new `injectProfileAtStart` key. *Tests:* injected once (not per turn); only `profile`/`preference` types; capped at `profileMaxTokens`; third-person frame present; `autoRecall` still independently off.

**MEM-4 — Consolidation.** `cmd_consolidate_session` extends `cmd_summarize_session`: typed third-person extraction, dedup vs existing, `source='consolidation'` + one `episodic` summary. Wire into agent-server session-end and NE-shell `/exit`; keep PS REPL. *Tests:* extraction returns typed bullets; re-running same session adds no duplicates; empty/short session is a no-op.

**MEM-5 — Hygiene + user surface.** TTL prune, `maxRows` cap, soft-update/`superseded_by`; new CLI subparsers `list/show/forget/edit/export` + `bob memory` verb + `--type` on `remember`/`recall`. *Tests:* prune keeps pinned + profile; forget soft-deletes (row hidden from recall, still in DB); edit re-embeds and supersedes; list filters by type.

**MEM-6 — Owner threading + NE5 integration.** Add `owner` to `RunContext`; pass server owner into recall/store/consolidate; align with WI-6/NE5. *Tests:* end-to-end owner scoping through the tool dispatch path; server path unaffected when memory disabled.

---

## 12. Risks

- **Re-embed cost/quality on migration (MEM-0).** Only 4 rows today; backup-first mitigates. Requires embed server up — fails loudly per CONTRIBUTING §2.
- **Consolidation LLM cost/quality (MEM-4).** Reuses the existing summarizer call, so no new failure mode; a bad extraction just yields a low-salience row that decays.
- **Owner threading touches `RunContext` (MEM-6).** Currently no owner slot; additive `__slots__` change, but every `RunContext(...)` construction must pass it — small blast radius.
- **Brute-force scan growth.** Mitigated by `maxRows` cap (§6) + `owner_id`/`superseded_by` prefilter (§3). If it ever matters, Qdrant (Module F) is the escape hatch — explicitly out of scope here.
- **Normalization false rewrites.** Deterministic rules are leading-pronoun-anchored and conservative; anything not matched is stored as-is with `subject='user'`, still framed at read time.
- **Dedup race** unchanged from today's best-effort note — benign for a personal DB.

---

## 13. Non-goals

- Migrating to a vector DB / ANN index (Qdrant is Module F).
- Cross-owner shared memory or memory-level ACLs beyond owner scoping.
- RAG over documents/files (this is user-fact memory, not a knowledge base).
- Changing the embedding model (`embed`/BGE-M3).
- A GUI/web memory browser — CLI (`bob memory ...`) only.
- Rewriting the `autoRecall` per-turn mechanism (it stays, default off).
