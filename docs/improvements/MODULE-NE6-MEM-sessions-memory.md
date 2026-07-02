# Module NE-WI6 + MEM — In-shell sessions & memory redesign (reconciled implementation plan)

> **Status:** ready to implement. Supersedes the WI-6 stub in `~/.claude/plans/module-ne-unified-interface.md` (§ "### WI-6") and the phasing in `docs/improvements/MODULE-MEM-memory-redesign.md` (validated + adjusted below — the MEM *design* stands; only sequencing/seams are reconciled).

---

## Context

Two adjacent, overlapping pieces of work remain after Module NE landed WI-0..WI-5, WI-7, WI-8:

- **WI-6 (NE5)** — the interactive shell ([scripts/bob/shell.py](../../scripts/bob/shell.py)) today keeps conversation in memory only: `self.history = []` ([shell.py:268](../../scripts/bob/shell.py#L268)) and an ephemeral `self.session_id = uuid.uuid4().hex[:8]` ([shell.py:267](../../scripts/bob/shell.py#L267)) that resets on every launch. `/session` is a stub ([shell.py:519-528](../../scripts/bob/shell.py#L519)) and `_on_exit` just prints "bye" ([shell.py:714-715](../../scripts/bob/shell.py#L714)). Restarting `bob` loses everything. WI-6 makes the shell run against the existing `SessionStore` so conversations persist and resume, owner-scoped.
- **Module MEM** — the memory store has four concrete defects (first-person storage, one untyped table, semantic-only ranking with a magic threshold, single weak write path with no hygiene). The redesign in `MODULE-MEM-memory-redesign.md` fixes them (schema v2, typed rows, blended ranking, consolidation, hygiene, CLI surface).

**Why one plan.** The two overlap at exactly one seam: WI-6's spec bullet says "on start `memory_recall`; on exit `cmd_summarize_session`" (module-ne-unified-interface.md WI-6). That naive recall/summarize is precisely what Module MEM redesigns (MEM-3 profile injection, MEM-4 consolidation). If WI-6 ships its own throwaway recall/summarize, MEM rips it out weeks later. **The reconciliation:** WI-6 owns *session persistence* and builds the *lifecycle seams* (session-start = a real new-vs-resumed session; session-exit = an end-of-session hook). MEM plugs its memory behaviour **into those seams** — it does not build its own lifecycle. This plan draws that line first, then gives one sequenced roadmap.

**Intended outcome:** the shell persists/resumes owner-scoped conversations; memory is typed, third-person, blended-ranked, injected once per session, consolidated at session end, and inspectable via `bob memory`. Every work item lands independently, gate-green (`check.ps1` + `test-dry-run.ps1`) after each.

**Already shipped this session (do NOT re-plan):** memory made sporadic — `memory.enabled` gates whether the tools load ([tools/memory.py:8-12](../../scripts/tools/memory.py#L8)); `memory.autoRecall` (default `false`, config/defaults.json:32) separately gates the heavy per-turn injection ([bob_loop.py:655-665](../../scripts/bob_loop.py#L655)). Recall output framed "Saved notes about the user (context only; …do not recite)". System prompt shrunk + generalized. This plan **keeps that split** and builds on it.

---

## 1. Scope split + dependency graph

### 1.1 The line between WI-6 and MEM

| Concern | Owner | What it delivers |
|---|---|---|
| Persisting shell turns to `SessionStore` | **WI-6** | create-or-resume a session on start, `append_turn` per turn, owner-scoped |
| `/session new\|list\|resume\|show` | **WI-6** | drive the store; needs `SessionStore.list_owned` |
| Over-budget refusal in the shell | **WI-6** | mirror the server's `over_budget → 402` |
| `SessionStore.list_owned(owner_id)` | **WI-6** | the one engine gap (list_ids is cross-owner) |
| **Session-start / session-exit lifecycle hooks** | **WI-6** | empty seams — no memory behaviour, just "a session began / ended" |
| Memory schema v2, typed rows, migration | **MEM-0/1** | independent of the shell |
| Blended read path, owner/scope prefilter | **MEM-2** | independent of the shell |
| **Once-per-session profile injection** | **MEM-3** | plugs into the loop's history-empty seam that WI-6 makes meaningful |
| **End-of-session consolidation** | **MEM-4** | plugs into WI-6's session-exit hook (shell) + server session-end |
| Hygiene, TTL, `bob memory list/show/forget/edit` | **MEM-5** | independent |
| `owner` on `RunContext`, threaded to recall/store/consolidate | **MEM-6** | closes owner threading WI-6 opens at the session layer |

**One-sentence rule:** *WI-6 builds the pipes (persisted sessions + start/exit hooks); MEM builds what flows through them (profile-in at start, consolidation-out at exit).* Do not implement recall-on-start / summarize-on-exit inside WI-6 — leave the hooks empty and let MEM-3/MEM-4 fill them.

### 1.2 Dependency graph

```
                         WI-6  (sessions + start/exit hooks + list_owned)
                          │
        ┌─────────────────┼───────────────────────────────┐
        │                 │                                │
   (independent)   session-exit hook              history-empty = true
   MEM-0 → MEM-1     needed by                       session start
        │            MEM-4                          needed by MEM-3
        ▼                 │                                │
   MEM-2  ──────────┐     │                                │
   (blended read)   │     │                                │
        │           ▼     ▼                                ▼
        │        MEM-3 (profile inject)  ◄── needs owner ── MEM-6
        │        MEM-4 (consolidation)   ◄── needs owner ── MEM-6
        ▼                 │
   MEM-5 (hygiene/CLI)    │
        └────────► MEM-6 (owner on RunContext, thread everywhere) ◄── aligns with WI-6 owner
```

Reading the edges that matter:
- **MEM-0 → MEM-1 → MEM-2** is a hard chain (typed schema → typed writes → typed reads). MEM-5 depends on the v2 schema (soft-delete columns) from MEM-0.
- **MEM-4 (consolidation) needs WI-6's session-exit hook** — that hook is where consolidation fires in the shell; the server analog is `_record_turn`/session-delete ([bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)).
- **MEM-3 (profile injection) needs WI-6's persisted-session semantics** — MEM-3 injects in the loop keyed on *history-empty* ([bob_loop.py:677](../../scripts/bob_loop.py#L677)); WI-6 is what makes "history empty" mean "genuinely a new session" (resume repopulates history → injection correctly fires *once at true session start*, not on every process launch). Without WI-6, "once per session" is unstable across restarts.
- **MEM-6 (owner on RunContext)** is the terminal integration: it threads the real owner into recall/store/consolidate. MEM-2 defaults `owner='local'`; MEM-6 makes it dynamic and aligns it with WI-6's `agent.defaultOwner` session owner.

---

## 2. WI-6 work items (each independently landable + unittest-tested)

### WI-6a — `SessionStore.list_owned(owner_id)` + tests

**Gap (confirmed):** `list_ids()` is cross-owner — `SELECT id FROM sessions ORDER BY updated_at DESC` with no `WHERE` ([bob_session.py:182-188](../../scripts/bob_session.py#L182)). No `list_owned` exists anywhere in the file.

**Change:** add one method mirroring the existing owner-scoped SQL (`delete_owned` at [bob_session.py:177-180](../../scripts/bob_session.py#L177) is the shape to copy). The index `idx_sessions_owner ON sessions(owner_id, updated_at DESC)` already exists (bob_session.py:87) so this is index-covered:

```python
def list_owned(self, owner_id: str) -> list:
    return [
        r[0] for r in self._conn()
        .execute(
            "SELECT id FROM sessions WHERE owner_id=? ORDER BY updated_at DESC",
            [owner_id],
        ).fetchall()
    ]
```

Optionally return richer rows (`id, updated_at, turns`) for `/session list` display — decide in WI-6b; a plain id list is enough to start.

**Tests** (`tests/test_bob_session.py`, extend existing): create sessions under owners `A` and `B`; assert `list_owned("A")` returns only A's ids, `list_owned("B")` only B's, ordered by `updated_at DESC`; empty owner → `[]`. Owner isolation is the core assertion.

### WI-6b — Shell session lifecycle (persist + resume, owner-scoped)

Mirror the server's session handling ([bob_agent_server.py:85-146](../../scripts/bob_agent_server.py#L85)) — do **not** invent a new pattern.

1. **Construct a store in `BobShell.__init__`** ([shell.py:261-274](../../scripts/bob/shell.py#L261)), exactly as the server does ([bob_agent_server.py:102-103](../../scripts/bob_agent_server.py#L102)):
   ```python
   agent = self.config.get("agent", {})
   self.owner = agent.get("defaultOwner", "local")
   repo = Path(__file__).resolve().parents[2]   # scripts/bob/shell.py -> repo root; mirror the server's REPO
   session_db = repo / agent.get("sessionDbPath", "data/sessions.db")
   self.sessions = SessionStore(session_db, default_owner=self.owner)
   self._max_tokens = int(agent.get("maxSessionTokens", 0))
   ```
   (`SessionStore` is **not** imported into shell.py — confirmed; add the import. The server resolves the path against its own `REPO` constant ([bob_agent_server.py:102](../../scripts/bob_agent_server.py#L102)); the shell has no such symbol today, so compute the repo root the same way as above or reuse an existing path helper if one is present.)

2. **Create-or-resume on start — but create *lazily*.** Replace the ephemeral `self.session_id = uuid.uuid4().hex[:8]` / `self.history = []` ([shell.py:267-268](../../scripts/bob/shell.py#L267)). **Do not create a DB row on start** — that would leave an empty session behind on every `bob` launch the user exits immediately. Instead: start with `self.session_id = None` and `self.history = []`; on the **first turn** (`_run_turn`), if `self.session_id is None`, call `self.sessions.create(token_budget=self._max_tokens, owner_id=self.owner)` → [bob_session.py:102](../../scripts/bob_session.py#L102), set `self.session_id` from the returned dict, then `append_turn`. `/session resume` sets `self.session_id` + `self.history = session["history"]` directly. This keeps the store's history and the in-memory working buffer identical without accumulating empty rows. (Alternative if lazy proves fiddly: create eagerly and let MEM-5's `maxRows`/TTL hygiene prune empties — but lazy is cleaner and preferred.)

3. **Persist each turn.** In `_run_turn` after a successful turn ([shell.py:583-585](../../scripts/bob/shell.py#L583)), in addition to the local `self.history.append(...)`, call a `_record_turn` mirroring the server ([bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)):
   ```python
   from bob_loop import _estimate_tokens
   used = _estimate_tokens(goal) + _estimate_tokens(result or "")
   self.sessions.append_turn(self.session_id, goal, result, tokens_used=used)
   ```
   `append_turn` ([bob_session.py:137-164](../../scripts/bob_session.py#L137)) is already WAL + atomic (`BEGIN IMMEDIATE` … `COMMIT`) and appends both user + assistant messages — matching the two local appends, so the store and `self.history` stay in lockstep.

4. **Over-budget refusal.** Before running a turn, mirror `_load_session_or_404`'s 402 branch ([bob_agent_server.py:136-137](../../scripts/bob_agent_server.py#L136)): `if self.session_id and self.sessions.over_budget(self.session_id): <print "session token budget exhausted — /session new to continue"; skip the turn>` (the `self.session_id and` guard is required because of lazy creation — there is no session before the first turn). **Honest caveat:** `agent.maxSessionTokens` defaults to `0` (config/defaults.json:64, [bob_agent_server.py:125](../../scripts/bob_agent_server.py#L125)) and `over_budget` returns `False` when the budget is falsy ([bob_session.py:169](../../scripts/bob_session.py#L169)) — so this guard is a **no-op unless the user configures a positive budget**. Wire it correctly (so it works when configured) but do not claim it enforces anything by default.

5. **`/session new|list|resume [id]|show [id]`** — replace the stub `_cmd_session` ([shell.py:519-528](../../scripts/bob/shell.py#L519)):
   - `new` — run the session-exit hook (WI-6c) for the *current* session (so consolidation fires when abandoning it later), then `create(...)` a fresh one, reset `self.history`.
   - `list` — `self.sessions.list_owned(self.owner)` → themed table (reuse `render.py` view style; shell.py already imports render helpers).
   - `resume <id>` — `self.sessions.get_owned(id, self.owner)` (**note arg order `(sid, owner_id)`** — [bob_session.py:132](../../scripts/bob_session.py#L132)); `None` → print "no such session for this owner" (mirrors the server's 404-without-existence-leak, [bob_agent_server.py:133-135](../../scripts/bob_agent_server.py#L133)); on success set `self.session_id` + `self.history = session["history"]`.
   - `show [id]` — print the turn count / last-updated / first user line (id defaults to current; if `self.session_id is None`, say "no active session yet — starts on your first message").
   Update the `NestedCompleter` `_SLASH` tree ([shell.py:56-70](../../scripts/bob/shell.py#L56)) `"/session": {"new": None, "list": None, "resume": None, "show": None}`.
   Resume correctness is free: `_run_turn`'s `factory` already passes `history=self.history` into `run_agent_events` ([shell.py:574-579](../../scripts/bob/shell.py#L574)) exactly as the server does ([bob_agent_server.py:236-247](../../scripts/bob_agent_server.py#L236)).

6. **`/clear`** ([shell.py:530-532](../../scripts/bob/shell.py#L530)) — decide semantics: keep it as "clear the on-screen buffer only" but do **not** wipe the persisted session (or make it `= /session new`). Recommend: `/clear` clears screen+working history for display but the persisted session is unchanged; document it. (Small, note in the WI-6b PR.)

**Tests** (`tests/test_bob_shell.py`, extend the 16 existing): construct a `BobShell` against a temp `SessionStore` (monkeypatch `sessionDbPath` to a tmp dir), feed a scripted turn via the existing fake-`run_agent_events` harness, assert a row lands in the store; construct a second shell, `/session resume <id>`, assert `self.history` restored and the next `run_agent_events` call receives the restored history; assert another owner's id 404-equivalents; assert a positive `maxSessionTokens` triggers the refusal path.

### WI-6c — Session-start / session-exit lifecycle hooks (empty seams)

These are the seams MEM-3/MEM-4 fill. **They ship empty (no-ops) in WI-6** so the shell lifecycle is complete and testable before MEM lands.

- **Session-exit hook** — add `self._on_session_end(session_id)` called from: (a) `_on_exit` ([shell.py:714-715](../../scripts/bob/shell.py#L714)) before the "bye" print; (b) `/session new` and `/session resume` *before* switching away from the current session (so the abandoned session gets consolidated). Body in WI-6: a single `pass` + a comment `# MEM-4 wires cmd_consolidate_session here`. **Early-return if `session_id is None`** (lazy creation means an exit before any turn has no session) and **guard it so it never raises into the exit path** (a failing consolidation must not stop the shell from exiting).
- **Session-start signal** — WI-6 does **not** add a shell-side start callback; MEM-3 injects in the loop keyed on history-empty ([bob_loop.py:677](../../scripts/bob_loop.py#L677)). WI-6's contribution is making that signal correct: a resumed session repopulates `self.history`, so the loop sees non-empty history and MEM-3 will *not* re-inject. Document this coupling in the WI-6c PR (one comment at the create-or-resume site) so the MEM-3 implementer knows the seam is "history-empty in `run_agent_events`," not a shell hook.
- **Server parity** — note (do not build here) that the server's session-end seam for MEM-4 is `_record_turn` / session-delete ([bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)); MEM-4 wires both surfaces.

**Tests:** assert `_on_session_end` is invoked on `/exit`, on `/session new`, and on `/session resume` (patch it, count calls); assert it swallows exceptions (a raising hook must not break exit).

---

## 3. MEM phases — validated against what shipped, restated as landable + tested

The MEM design (`MODULE-MEM-memory-redesign.md` §§2-10) **holds**. Adjustments below reflect this session's shipped state and the WI-6 seams.

### MEM-0 — Schema v2 + migration foundation
As designed (§7, §9). **Adjustment:** the design says migration "mirrors `SessionStore._ensure_schema`" — note that `_ensure_schema` actually uses `PRAGMA table_info(sessions)` + additive `ALTER` ([bob_session.py:61-88](../../scripts/bob_session.py#L61)), **not** `PRAGMA user_version`. Both patterns are valid; recommend MEM-0 use the cleaner `PRAGMA user_version` gate it proposes (there is none in `bob_memory.py` today — `get_db` at [bob_memory.py:78-101](../../scripts/bob_memory.py#L78) has no version gate) and cite the `table_info` precedent as the additive-column model. `bob memory migrate --normalize` is a new subparser in `main()` ([bob_memory.py:285-330](../../scripts/bob_memory.py#L285)). *Tests:* fresh DB → v2 columns + `user_version=2`; seeded legacy 4-row DB migrates idempotently; the §7 normalization table (id 1-4) is reproduced by the deterministic path.

### MEM-1 — Typed write path
As designed (§5.1). `store()` ([bob_memory.py:130-151](../../scripts/bob_memory.py#L130)) gains `type/owner/scope/tags/salience`, §2.3 leading-pronoun normalization before `embed`, `content_hash` exact-dedup (O(1) short-circuit before the O(n) cosine scan at [bob_memory.py:139-141](../../scripts/bob_memory.py#L139)), and `(owner,type)`-scoped near-dedup at `memory.dedupThreshold` (replacing the hardcoded `0.95` at [bob_memory.py:141](../../scripts/bob_memory.py#L141)). **Adjustment (bug found):** the `bob_core.memory_store` wrapper currently **drops `tags`** — signature takes `tags` but never passes it to `bob_memory.store` ([bob_core.py:145-153](../../scripts/bob_core.py#L145)). Fix while threading the new args. *Tests:* pronoun rewrite (all 4 legacy strings); exact-dedup short-circuit; cosine near-dedup respects `(owner,type)` scope; `type` persisted.

### MEM-2 — Blended read path
As designed (§3). Rewrite `recall()` scoring ([bob_memory.py:154-182](../../scripts/bob_memory.py#L154)): SQL prefilter `WHERE owner_id=? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > now)` (closes the current all-rows scan at [bob_memory.py:161](../../scripts/bob_memory.py#L161)), blended `score = wSemantic·cosine + wRecency·decay + wType·typeWeight + wUsage·usage`, threshold on the blended score via `memory.recallThreshold` (default `0.35`, replacing the hardcoded `0.3` at [bob_memory.py:154](../../scripts/bob_memory.py#L154)). Recency reuses `created_at`; usage reuses `use_count` (written at [bob_memory.py:176-181](../../scripts/bob_memory.py#L176) but never read today). Add optional `owner`/`scope` params to `bob_core.memory_recall` ([bob_core.py:156-166](../../scripts/bob_core.py#L156)) defaulting `owner='local'`. **Adjustment:** `memory.recallThreshold` does **not** exist in `defaults.json` today (the `0.3` is only the function default arg) — add it in this phase's config change (§8 of the MEM doc). *Tests:* newer row outranks older at equal cosine; owner B cannot recall owner A's rows; below-blended-threshold filtered; type-weight tie-break.

### MEM-3 — Session-start profile injection
As designed (§4). `memory_profile_block(owner, config) -> str|None` (top-N `type IN ('profile','preference')`, capped at `memory.profileMaxTokens`, third-person frame), prepended to the system prompt at the history-empty seam in `run_agent_events` ([bob_loop.py:646-648 / 677](../../scripts/bob_loop.py#L646)). New key `memory.injectProfileAtStart` (default `true`), distinct from `autoRecall`. **Adjustment (framing consolidation):** there are currently **three** divergent recall frames — loop autoRecall "…use only if relevant, do not recite" ([bob_loop.py:660-661](../../scripts/bob_loop.py#L660)), tool recall "…do not recite" ([tools/memory.py:30](../../scripts/tools/memory.py#L30)), and MEM-3 will add a third. Define **one** frame constant and reuse it across all three sites so the model never sees three phrasings of the same idea. **Dependency note:** MEM-3 selects rows by `owner`; until MEM-6 threads the real owner, use `agent.defaultOwner` ('local') — same value WI-6 uses, so it is correct in the single-user case and MEM-6 only generalizes it. **O-forward-compat (§7 item 1) — deferred gate, not needed yet:** gate on **history-empty** now — pre-O1 there are no sub-agents, so history-empty *is* a true root session start and this is correct. When MEM-6 adds `agent_depth` to `RunContext`, it **tightens** this gate to `history-empty AND agent_depth == 0` so O1 sub-agents (which start with empty isolated history by design) never inherit the profile block. Do not reference `agent_depth` in MEM-3 — the slot doesn't exist until MEM-6. *Tests:* injected once (history-empty, not per turn); only `profile`/`preference` types; capped at `profileMaxTokens`; frame present; `autoRecall` still independently off. (The depth-1-suppression test lands with MEM-6, where the field exists.)

### MEM-4 — Consolidation (wires into WI-6c's exit hook)
As designed (§5.2, §5.3) with **one structural adjustment the MEM doc understates:** `cmd_summarize_session` reads turns from a **file** — `def cmd_summarize_session(messages_file, model, db_path)` ([bob_memory.py:232](../../scripts/bob_memory.py#L232)) — because the legacy PS REPL writes a temp file then calls it (bob.ps1:629-637). The shell and server hold turns **in memory** and must not write a temp file / shell out (CONTRIBUTING §2 — logic in importable core, no subprocess). **So MEM-4 must extract an in-process core** `consolidate_session(turns: list, model, db_path, config, owner) -> None` and have (a) the file-based CLI `cmd_summarize_session`/`cmd_consolidate_session` read the file then call it, (b) the shell exit hook (WI-6c) call it directly with `self.history`, (c) the server call it from its session-end seam ([bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)). The core: reuse the LLM plumbing ([bob_memory.py:253-265](../../scripts/bob_memory.py#L253)), change the prompt ([bob_memory.py:242-251](../../scripts/bob_memory.py#L242)) to emit 0-5 typed third-person bullets, `store(..., source='consolidation')` each (dedups via MEM-1), and store the raw summary as one `type='episodic'` row (preserving [bob_memory.py:267](../../scripts/bob_memory.py#L267)). Gated by new `memory.autoConsolidate` (default `true`). **O-forward-compat (§7 items 1-2):** (a) consolidation is naturally **root-only** — it fires only from the surfaces that call it (shell `/exit`, server session delete), and O1 sub-runs return a summary to their parent (MODULE-O O1:186) rather than calling `consolidate_session`, so no explicit depth gate is required in MEM-4. (If O1 ever wires a sub-run into a session-end surface, add the `agent_depth == 0` guard then — the field exists after MEM-6.) (b) Expose the LLM-summarizer as a standalone importable helper (`summarize_turns(turns, model, config) -> str`) that both `consolidate_session` and **O3 context compaction reuse** — O3 explicitly plans to call this summarizer (MODULE-O O3:151-152); building one shared core here avoids O3 duplicating it. *Tests:* extraction returns typed bullets (mock the LLM HTTP call); re-running the same turns adds no duplicates; empty/short session is a no-op; the shell exit hook invokes the core with the session's turns.

### MEM-5 — Hygiene + user surface
As designed (§6). TTL prune (`forgetAfterDays[type]`), `maxRows` per-owner cap, soft-update via `superseded_by` (read path filters it in MEM-2). New `main()` subparsers `list/show/forget/edit/export` ([bob_memory.py:285-330](../../scripts/bob_memory.py#L285)) + `--type` on `remember`/`recall`; the `bob memory` verb (bob.ps1:906-914) forwards args unchanged. **No adjustment** — independent of the shell. *Tests:* prune keeps pinned + profile; forget soft-deletes (hidden from recall, still in DB); edit re-embeds + supersedes; list filters by type.

### MEM-6 — Owner threading + NE5 integration
As designed (§10) — the terminal integration. Add `owner` **and `agent_depth`** to `RunContext.__slots__` (currently 5 fields, no owner — [bob_loop.py:189-201](../../scripts/bob_loop.py#L189)); thread `owner` from `run_agent_events` (add `owner` param, defaulting to `agent.defaultOwner`) into every `RunContext(...)` construction and out through `dispatch_call` to the memory tools; pass the server's `_authed_owner` result and the shell's `self.owner` in. `agent_depth` defaults to `0` (root). **This step also tightens MEM-3's injection gate** from `history-empty` to `history-empty AND agent_depth == 0` (the deferred gate MEM-3 flagged) — now that the field exists, O1 sub-agents won't inherit the profile block. This makes MEM-2/MEM-3/MEM-4's `owner` dynamic instead of the `'local'` default. **Alignment:** WI-6 already scopes sessions to `agent.defaultOwner`; MEM-6 scopes memory to the same axis — one owner concept end-to-end. **O-forward-compat (§7 item 4):** `owner`/`agent_depth` on `RunContext` is precisely the seam O1 (sub-agents), O6 (per-owner/per-depth permission policy, MODULE-O O6:63-64), and O8 (RBAC by owner) need — O1 will propagate them parent→child exactly as NE0's `CancelToken.child()` ([bob_loop.py:411-413](../../scripts/bob_loop.py#L411)) already does. Building the slots here pre-pays that integration; keep the change additive so O1 only *sets* `agent_depth`, never re-plumbs. Also flag **`memory_store` as a mutating tool** (a `mutating=True` marker on its tool def) so O2 serializes it and O6 can default it to `ask` — see §7 item 3. *Tests:* end-to-end owner scoping through tool dispatch; server path unaffected when memory disabled; a non-'local' owner recalls only its own rows; a run with `agent_depth=1` suppresses MEM-3 injection (the depth-1 test deferred from MEM-3); every `RunContext(...)` construction site updated (grep-verified).

---

## 4. One sequenced roadmap (gate-green after each)

**Recommended order (honors the dependency graph):**

```
WI-6a  →  WI-6b  →  WI-6c  →  MEM-0  →  MEM-1  →  MEM-2  →  MEM-3  →  MEM-4  →  MEM-5  →  MEM-6
```

Rationale for this exact order:
1. **WI-6a/b/c first** — sessions + the exit hook must exist before MEM-4 can wire into them, and persisted-session semantics must exist before MEM-3's "once per session" is stable. WI-6 also touches only `bob_session.py` + `shell.py` (+ tests) — no memory files — so it lands cleanly in isolation.
2. **MEM-0 → MEM-1 → MEM-2** — the hard schema→write→read chain. Nothing downstream (injection, consolidation, hygiene) is correct without typed rows and the owner/scope prefilter.
3. **MEM-3, MEM-4** — plug into the seams (loop history-empty; WI-6c exit hook). MEM-4 does the `consolidate_session` core extraction.
4. **MEM-5** — hygiene/CLI, independent; placed after the read/write paths it inspects exist.
5. **MEM-6 last** — flips every `owner='local'` default to dynamic once all consumers exist; smallest-blast-radius change made once, not repeatedly.

**Gate-green rule (every item):**
```
pwsh -File scripts\check.ps1                                        # py_compile + AST + platform + verbs + versions.lock + unittest
.\scripts\test-dry-run.ps1                                          # all sections green
tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests # Windows + Linux (NB6 CI)
```
No item merges until all three are green. WI-6a/6b/6c and each MEM phase are separate landable commits; if a phase grows large, split its tests-first commit from its wiring commit but keep the gate green at each.

---

## 5. Integration points

- **Shell `/exit` → consolidation.** `_on_exit` ([shell.py:714-715](../../scripts/bob/shell.py#L714)) → WI-6c `_on_session_end` (empty) → MEM-4 fills with `consolidate_session(self.history, model, db_path, config, self.owner)` gated on `memory.enabled && memory.autoConsolidate`. Also fires on `/session new` and `/session resume` for the abandoned session.
- **Agent-server session-end → consolidation.** MEM-4 wires the same core at the server's token-accounting seam / session delete ([bob_agent_server.py:141-146](../../scripts/bob_agent_server.py#L141)), owner = `_authed_owner(...)` ([bob_agent_server.py:110-116](../../scripts/bob_agent_server.py#L110)).
- **Loop history-empty → profile injection.** MEM-3 prepends `memory_profile_block(owner, config)` to the system prompt when `history` is empty ([bob_loop.py:677](../../scripts/bob_loop.py#L677)), owner from RunContext (MEM-6) or `agent.defaultOwner` until then.
- **Owner threading (MEM-6).** Path: `shell.self.owner` / server `_authed_owner` → `run_agent_events(owner=…)` → `RunContext.owner` ([bob_loop.py:189-201](../../scripts/bob_loop.py#L189)) → `dispatch_call` → memory tools ([tools/memory.py](../../scripts/tools/memory.py)) → `bob_core.memory_recall/store(owner=…)` ([bob_core.py:145-166](../../scripts/bob_core.py#L145)) → `bob_memory.recall/store`. One owner axis shared with WI-6's session owner (`agent.defaultOwner`).
- **Config** — all new keys land in `config/defaults.json` `runtime.memory` (§8 of MEM doc: `injectProfileAtStart`, `profileMaxTokens`, `recallThreshold`, `dedupThreshold`, `autoConsolidate`, `ranking`, `typeWeights`, `maxRows`, `forgetAfterDays`, `scopeByProject`), read via `.get()` with defaults so an older `data/config.json` ([bob_core.py:75-89](../../scripts/bob_core.py#L75)) still boots. WI-6 adds no new config (reuses `agent.sessionDbPath`/`defaultOwner`/`maxSessionTokens`).

---

## 6. Test strategy

Stdlib `unittest`; **mock the embed model** per the existing pattern in [tests/test_memory.py](../../tests/test_memory.py); mock the consolidation LLM HTTP POST ([bob_memory.py:253-265](../../scripts/bob_memory.py#L253)). Coverage targets per item:

| Item | Test file | Assertions |
|---|---|---|
| WI-6a | `test_bob_session.py` | **owner isolation** — `list_owned(A)` excludes B; order by `updated_at DESC`; empty owner → `[]` |
| WI-6b | `test_bob_shell.py` | **persist/resume** — turn lands in store; second shell resumes history; restored history reaches `run_agent_events`; **budget** — positive `maxSessionTokens` refuses; other-owner id not resumable |
| WI-6c | `test_bob_shell.py` | exit hook invoked on `/exit`, `/session new`, `/session resume`; hook swallows exceptions |
| MEM-0 | `test_memory.py` | v2 columns + `user_version=2`; legacy 4-row DB migrates **idempotently**; §7 normalization table reproduced |
| MEM-1 | `test_memory.py` | pronoun rewrite; **exact-dedup** short-circuit; **near-dedup** respects `(owner,type)`; type persisted; tags no longer dropped |
| MEM-2 | `test_memory.py` | recency tie-break; **owner isolation** (B can't recall A); blended-threshold filter; type-weight tie-break |
| MEM-3 | `test_memory.py` / `test_bob_loop.py` | **injection-once** (history-empty only); only profile/preference; token cap; single frame constant; `autoRecall` still off |
| MEM-4 | `test_memory.py` | typed bullets extracted (LLM mocked); **dedup** — re-run adds no duplicates; empty session no-op; shell hook calls core with turns |
| MEM-5 | `test_memory.py` | prune keeps pinned+profile; forget soft-deletes (hidden, still in DB); edit re-embeds+supersedes; list filters by type |
| MEM-6 | `test_memory.py` / integration | end-to-end owner scoping through dispatch; server unaffected when memory disabled |

The two isolation properties to prove hardest: **session owner isolation** (WI-6a/6b) and **memory owner isolation** (MEM-2/MEM-6) — both are the same "one owner can't see another's rows" invariant at two layers.

---

## 7. Module O forward-compatibility (does this memory system serve O?)

**Yes — the design is aligned and complementary to [MODULE-O-frontier-class.md](MODULE-O-frontier-class.md), not just compatible.** Memory owner-scoping, the `RunContext` owner/depth slots, and the shared summarizer are seams O reuses rather than reworks. Four things must be built *correctly now* so O plugs in without a protocol break — all folded into MEM-3/MEM-4/MEM-6 above:

| # | O item | Interaction | What we build now |
|---|--------|-------------|-------------------|
| 1 | **O1 sub-agents** (MODULE-O O1:167-193) — isolated empty context per sub-run | MEM-3's "history-empty = session start" proxy is **false** for sub-agents (they always start empty) → profile block would leak into every sub-run | MEM-3 ships gated on history-empty (correct pre-O1); **MEM-6 tightens it** to `history-empty AND agent_depth == 0` once it adds `agent_depth` to `RunContext`. No forward-reference to a field before it exists. |
| 2 | **O3 context compaction** (MODULE-O O3:141-163) — "reuse the existing summarizer rather than adding a model dependency" (O3:151-152) | O3 and MEM-4 both summarize turns; two implementations = drift | MEM-4 extracts `summarize_turns(...)` as the single importable summarizer core; O3 calls the same one. Also fire consolidation only at root. |
| 3 | **O2 parallel tools** (MODULE-O O2:128-130) — "serialize mutating tools" & **O6** (O6:63-66) | `memory_store` writes SQLite with a best-effort dedup race (MEM §12); concurrent writes worsen it | Mark `memory_store` `mutating=True` (MEM-6) so O2 serializes it and O6 can default it to `ask`. `memory_recall` stays read-only/parallel-safe. |
| 4 | **O6 policy / O8 RBAC** (MODULE-O O6:63-64, O8:248-264) — per-owner/per-depth authz keyed on N1 `owner_id` | Both need the owner+depth axis present on the run context and threaded through `dispatch_call` | MEM-6's `RunContext.{owner, agent_depth}` + the `dispatch_call` threading *is* that axis. Additive so O1 only sets `agent_depth`, O6/O8 only read it. |

**Net:** the shared summarizer (O3), the owner/depth run-context (O1/O6/O8), and the mutating-tool marker (O2/O6) are all things O would otherwise have to retrofit. Building them in MEM-4/MEM-6 means O composes on top, consistent with O's own promise that "O reuses N's identity, cancellation, streaming" (MODULE-O intro). **No conflicts found** — the single-table brute-force store stays (vector DB is Module F, an O non-goal too), and O adds no memory-model requirement that this redesign violates.

---

## 8. Risks, non-goals, open questions

**Risks**
- **`consolidate_session` extraction (MEM-4)** — the file-vs-in-memory refactor is the highest-surface change; reuses the existing LLM call so no new failure mode, but the core must be pure/importable and callable from three surfaces. Land it tests-first.
- **`RunContext` owner + agent_depth (MEM-6)** — additive `__slots__` change, but *every* `RunContext(...)` construction must pass the new fields or Python raises; grep all constructions before landing. Give both keyword defaults (`owner='local'`, `agent_depth=0`) so O1 later only *sets* `agent_depth` on child runs — never re-plumbs the signature.
- **Over-budget guard is inert by default** — `maxSessionTokens=0` (config/defaults.json:64) → `over_budget` always False. Wire it correctly but don't claim enforcement without a configured budget.
- **Re-embed on migration (MEM-0/`--normalize`)** — 4 rows today; backup-first (`bob.db.bak.<ts>`), needs the embed server up, fails loudly (CONTRIBUTING §2).
- **`/clear` vs persisted session** — ambiguous semantics; decide in WI-6b (recommend: display-only clear, session untouched) and document.

**Non-goals**
- Module O sub-agents / the permission *policy* engine (NE0 built the pipe; O6 owns policy).
- Vector DB / ANN index (Qdrant = Module F) — brute-force cosine over one owner's prefiltered rows stays.
- Changing the embed model (`embed`/BGE-M3).
- RAG over documents; cross-owner shared memory / ACLs beyond owner scoping.
- Rewriting the `autoRecall` per-turn mechanism (stays, default off).
- A GUI/web memory browser — `bob memory …` CLI only.
- Full-screen TUI (WI-5 already decided against it).

**Open questions (surface to the user before/at implementation, not blockers)**
1. **Consolidation on `/exit` — synchronous or skip?** MEM-4 fires an LLM call at exit. Block the exit ~1-2s while it runs, run it fire-and-forget, or only on explicit `/session end`? Recommend: synchronous with a spinner + a short timeout, skipped for sessions under N turns.
2. **`/session list` display richness** — plain id list, or `id · updated · turns · first-line`? Recommend the richer row (needs `list_owned` to return dicts, trivial extension of WI-6a).
3. **Should the shell reuse the *same* `sessions.db` as the agent server, or a shell-scoped file?** Recommend the same file (`agent.sessionDbPath`) so a session started in one surface is resumable in the other — but confirm that cross-surface resume is desired.
