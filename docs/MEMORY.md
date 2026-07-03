# Memory & Sessions

Bob remembers what matters across sessions. Facts you state, preferences you express, and the
projects you work on are extracted, stored, ranked, and fed back into future conversations — locally,
with zero extra VRAM.

This page is the reference for the memory engine, persisted sessions, per-project memory, the
`bob memory` CLI, the agent tools, and every `memory.*` config key.

- **Store:** SQLite (`data/bob.db`) + BGE-M3 embeddings (the `embed` model, already pinned at
  `:8081` — memory costs 0 extra VRAM and one embed call per store/recall).
- **Local always:** even when `bob chat --pro` routes answers to the cloud, recall and embedding stay
  on BGE-M3 at `:8081`. Memory never leaves the machine.
- **On by default:** `memory.enabled = true`. Turn it off in `config/bob.psd1` (`memory = @{ enabled = $false }`).

---

## Quick start

```powershell
bob remember "I prefer explicit error messages over silent failures"
bob recall  "error handling preferences"     # blended-rank search, prints JSON
bob memory list                               # browse what Bob knows
bob memory status                             # DB path, size, counts by type
```

Inside the `bob` shell, memory works automatically — durable facts you share are consolidated when a
session ends, and your stable profile is injected at the next session's start. No meta-commands
needed (see [Sessions](#sessions)).

---

## Concepts

### Typed memory

Every memory has a **type**, which sets how it ranks and how long it lives:

| Type | What it holds | Half-life (recency decay) | Rank weight |
|------|---------------|---------------------------|-------------|
| `profile` | Core identity (name, role) | ~100 y (never decays) | 1.0 |
| `preference` | Stable preferences (tools, style) | ~100 y | 0.9 |
| `project` | What you're building | 90 days | 0.8 |
| `fact` | General durable facts | 365 days | 0.7 |
| `episodic` | Per-session recaps | 30 days | 0.5 |

`bob remember` stores type `fact`. Consolidation (below) assigns the richer types. Identity/prefs are
global; `project` facts can be scoped to a repo (see [Per-project scoping](#per-project-scoping)).

### Blended recall ranking

Recall is **not** plain semantic search. Each candidate is scored:

```
score = wSemantic·cosine + wRecency·decay + wType·typeWeight + wUsage·usage + wSalience·salience
```

- **cosine** — BGE-M3 semantic similarity to the query.
- **decay** — `exp(-age / halfLife[type])`, where age is measured from the **more recent** of
  `created_at` / `last_used`, so a fact you keep hitting stops decaying.
- **typeWeight** — the per-type weight above.
- **usage** — how often the fact has been recalled (capped).
- **salience** — importance, see below.

All weights are tunable (`memory.ranking.*`). Only results at or above `memory.recallThreshold` are
returned, top `memory.recallK` first.

### Importance & salience

When consolidation extracts a fact, the model rates its **importance 1–10** (mundane → core
identity). That becomes `salience = importance/10`, a live ranking term (`wSalience`). A profile fact
rated ≥ 9 is **auto-pinned**.

### Pinning

A **pinned** memory is never pruned and ranks first in the profile block.

```powershell
bob memory pin 42
bob memory unpin 42
```

### Conflict-aware consolidation (supersede, don't accumulate)

When a session ends, Bob extracts durable facts in **one LLM call** that is also shown your existing
facts. Each extracted fact is tagged `NEW` or `REPLACES:<id>`. A `REPLACES` **supersedes** the old
row (marks it `superseded_by`, keeps it for audit) instead of piling a contradiction on top. So "I
use vim" → later "I switched to vscode" leaves only the vscode fact active. Ambiguous cases default
to `NEW` (conservative). Tunable window: `memory.reconcileTopK`.

Every session also stores one prose **episodic** recap (with a deterministic fallback, so a session
is never silently dropped even if the summarizer fails).

### Dedup & third-person normalization

On store, content is normalized to third person ("I prefer X" → "User prefers X") so recalled notes
never read as Bob's own identity, then deduped — exact (content hash) and near (cosine ≥
`memory.dedupThreshold`), scoped to the same owner/type.

### Provenance

Each consolidated row records the **session that produced it** (`source_session`), visible in
`bob memory show <id>` / `export`. You can retract everything a session taught Bob:

```powershell
bob memory forget --session <session-id>
```

### Hygiene

At the end of consolidation Bob prunes: rows past their per-type TTL (`memory.forgetAfterDays`) and,
if the owner exceeds `memory.maxRows`, the lowest-salience/oldest rows. Pinned rows and
profile/preference identity are never pruned.

### Scoping

- **Owner** — every row belongs to an `owner_id`. On the single-user machine that's
  `agent.defaultOwner` (`local`); the [agent HTTP server](AGENT-SERVER.md) maps each Bearer token to
  an owner, and a recall only sees its owner's rows. `bob memory list/export/forget` take `--owner`.
- **Project** — see below.

### Per-project scoping

With `memory.scopeByProject = true` (default), `project`-type facts learned while you work in a repo
are **scoped to that repo** (keyed by the git root, else the cwd). Recall in project A returns A's
project facts plus all global facts, never project B's. Identity and preferences stay global. Turn it
off to put everything in one global pool.

---

## Sessions

The `bob` shell (run `bob` with no args) keeps **persisted, owner-scoped sessions** in
`data/sessions.db` (path: `agent.sessionDbPath`). History survives across restarts, and leaving a
session triggers memory consolidation.

| Command | Does |
|---------|------|
| `/session new` | Start a fresh session (consolidates the current one first) |
| `/session list` | List this owner's sessions (id prefix, turn count, age) |
| `/session resume <id>` | Reload a past session's history (id or unambiguous 8-char prefix) |
| `/session show [id]` | Show the current (or given) session's turns |
| `/clear` | Clear the on-screen transcript for this turn (history is retained; `/session new` for a real reset) |
| `/exit` | Leave the shell (consolidates the session) |

**Lifecycle of a fact:**

1. **Session start** (empty history, root run) — your stable **profile** block is injected once, plus
   any project [`BOB.md`](#project-instruction-files) for the repo you're in.
2. **During the session** — if `memory.autoRecall = true` (off by default), the top few relevant
   memories are recalled and injected **every turn**. The agent can also call the `memory_recall` /
   `memory_store` tools on demand.
3. **Session end** (`/exit`, `/session new`, `/session resume`, or the server deleting a session) —
   turns are **consolidated** into durable typed facts (gated on `memory.autoConsolidate`).

Injected memory (profile + autoRecall + `BOB.md`) is fit into `memory.maxInjectedTokens` before it
goes into the system prompt, trimming autoRecall first, then profile, then `BOB.md` — so injected
memory can't overflow the context window.

---

## Project instruction files

Alongside the learned DB facts, Bob reads **human-curated, git-committable** instruction files for
the repo you launch it in — the analogue of a project README for the agent. They're concatenated
broad → specific at session start (capped at `memory.bobMdMaxTokens`), gated on
`memory.projectFiles`:

1. `~/.bob/BOB.md` — applies to all your projects
2. `<repo>/AGENTS.md` — cross-tool agent-instructions standard, if present
3. `<repo>/.bob/BOB.md`
4. `<repo>/BOB.md` — most specific, read last

Use these for durable, reviewable project rules ("use pnpm, not npm"); use the DB for facts Bob
learns on its own.

---

## CLI reference

```
bob remember "<text>"                 Store a fact (type=fact)
bob recall "<query>" [--top N] [--threshold T] [--type <type>]
                                      Blended-rank search; prints JSON
bob clip <url> [--note "<text>"]      Fetch a page, summarise it, store to memory

bob memory status                     DB path, size, total + per-type counts
bob memory list  [--type <type>] [--owner <id>] [--limit N] [--all]
                                      Browse memories (--all includes forgotten/superseded)
bob memory show  <id>                 Full row incl. source_session / provenance
bob memory edit  <id> "<text>"        Replace a memory (re-embeds; supersedes the old row)
bob memory pin   <id>   /   unpin <id>   Protect from pruning / release
bob memory forget <id>                Soft-delete one memory (kept for audit)
bob memory forget --query "<q>"       Soft-delete the best match for a query
bob memory forget --session <id>      Soft-delete everything a session produced
bob memory export [--owner <id>]      Dump memories as JSON
bob memory migrate [--normalize]      Run schema migration (--normalize re-embeds to 3rd person; backs up first)
bob memory init-profile --name "<n>" --work "<w>"    Seed identity as profile rows
bob memory clear [--yes]              Wipe ALL memories
```

Types for `--type`: `profile`, `preference`, `project`, `fact`, `episodic`.

---

## Agent tools

When `memory.enabled`, two tools are available to the agent loop (and over MCP):

- **`memory_recall`** — recall the user's saved notes when the current request needs them.
- **`memory_store`** — save a note about the user for future sessions.

Both operate only on the local `bob.db` via the embed server, scoped to the run's owner (and project
scope). See [SECURITY.md](SECURITY.md).

---

## Configuration (`memory.*`)

All keys live in `config/defaults.json` under `runtime.memory` and can be overridden in
`config/bob.psd1` (`memory = @{ … }`) or `config/user.psd1`.

| Key | Default | Effect |
|-----|---------|--------|
| `enabled` | `true` | Master switch — the CLI, tools, injection, and consolidation. |
| `autoRecall` | `false` | Recall + inject relevant memories **every turn** (heavier). Off = the agent recalls only via the `memory_recall` tool when needed. |
| `injectProfileAtStart` | `true` | Inject the stable profile block once at session start. |
| `profileMaxTokens` | `200` | Cap on the profile block. |
| `maxInjectedTokens` | `1200` | Total budget for injected memory (profile + autoRecall + `BOB.md`); over budget trims autoRecall → profile → `BOB.md`. |
| `dbPath` | `data/bob.db` | Memory database (gitignored). |
| `embedModel` | `embed` | Embedding model role (BGE-M3 at `:8081`). |
| `recallK` | `5` | Max results returned by a recall. |
| `recallThreshold` | `0.35` | Minimum blended score to return. |
| `dedupThreshold` | `0.92` | Cosine at/above which a new store is treated as a duplicate. |
| `ranking.wSemantic` | `1.0` | Weight — semantic similarity. |
| `ranking.wRecency` | `0.3` | Weight — recency decay. |
| `ranking.wType` | `0.2` | Weight — per-type importance. |
| `ranking.wUsage` | `0.1` | Weight — recall frequency. |
| `ranking.wSalience` | `0.3` | Weight — importance/salience. |
| `ranking.halfLifeDays` | see [Typed memory](#typed-memory) | Per-type recency half-lives. |
| `typeWeights` | see table | Per-type rank weights. |
| `maxSummaryTokens` | `512` | Token budget for the consolidation/summary LLM call. Must clear the reasoning budget of a reasoning model (a tight cap yields an empty completion). |
| `autoSummarize` | `true` | Legacy `bob chat` REPL: summarise on exit. |
| `autoConsolidate` | `true` | Consolidate durable facts when a shell/server session ends. |
| `consolidateTimeout` | `30` | Seconds bounding the end-of-session consolidation call. |
| `reconcileTopK` | `20` | How many existing facts to show the extractor for supersede decisions. |
| `maxRows` | `2000` | Per-owner soft cap; excess lowest-salience/oldest rows are pruned. |
| `forgetAfterDays` | `{episodic: 180}` | Per-type TTL for hard pruning (profile/preference exempt). |
| `scopeByProject` | `true` | Scope `project`-type facts per repo; `false` = one global pool. |
| `projectFiles` | `true` | Read `BOB.md` / `AGENTS.md` project instruction files at session start. |
| `bobMdMaxTokens` | `4000` | Cap on the concatenated project instruction files. |

---

## Storage & privacy

- `data/bob.db` — the memory store (SQLite, gitignored). Schema is versioned; `bob memory migrate`
  applies additive migrations in place (a legacy DB is upgraded automatically on first open).
- `data/sessions.db` — persisted shell/server session transcripts (`agent.sessionDbPath`).
- Nothing is sent to the cloud for memory: BGE-M3 runs locally. `--pro` only affects the chat
  response, never recall or embedding.
- To wipe: `bob memory clear --yes` (memories) or delete `data/bob.db` (rebuilt empty on next use).

---

See also: [USAGE.md](USAGE.md) (full command reference), [TUNING.md](TUNING.md) (all config),
[AGENT-SERVER.md](AGENT-SERVER.md) (owner-scoped sessions over HTTP), [SECURITY.md](SECURITY.md)
(tool safety).
