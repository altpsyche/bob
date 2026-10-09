# Token meter — spec/plan

Small, additive display surface: a `/usage` command in the cockpit plus a one-line
post-turn meter. **No new plumbing** — every number is already captured end to end.

## 1. Why

Bob tracks token usage completely today, but only *spends* it on the backend:

- `bob_loop.py` accumulates per-run usage (`prompt_tokens`, `completion_tokens`,
  `estimated`, `total_tokens`) from the backend's
  `stream_options={"include_usage": True}`; falls back to a local estimate and sets
  `estimated: True`. The final stream event carries `usage`.
- The shell stores it per turn (`shell.py`: `_last_usage` set from the final event,
  reset at turn start) and persists it via `record_turn()` → session DB
  (`bob_session.py`): `tokens_spent` (cumulative per session, incremented per turn)
  and a `token_budget` column (0 = unlimited; the agent server rejects turns once
  `tokens_spent >= token_budget`).
- The prompt toolbar already shows a live fill label: `_context_label()` renders
  e.g. `quick ~12.3k/56.0k tok (22%)` — last turn's `prompt_tokens` (or a live
  `est_tokens` of the growing history) over the active mode's effective window for
  the current role (`bob_context.resolve(...).window(config, role)`).

What is **not** visible: the session's running total, the turn count, whether usage
is backend-reported or estimated, how close the session is to its budget cap, and
when context compaction will next fire. `bob budget` covers *all-time* LiteLLM spend —
a different ledger, not this session's fill.

Compaction is also silent: it happens inside `bob_loop`'s budget fitting
(truncate in Quick mode, summarize in Deep), with no stream event announcing it.

## 2. Scope

Two small, display-only additions, both inside `scripts/bob/shell.py`:

### 2.1 `/usage` command

One read-only command, added to the existing `_COMMANDS` table (one entry;
completion, dispatch, and `/help` all derive from that list, so no other changes):

```
_Cmd("/usage", "session token usage and context fill", "_cmd_usage"),
```

Handler `_cmd_usage(self, arg)` prints a compact block, all values already in hand:

```
usage       chat · quick
  context   23.4k / 56.0k tok (42%)
  session   96.1k tok · 7 turns
  source    backend-reported        (or: ~estimated)
  budget    none                    (or: cap 100k — 96% used / cap reached)
  compaction  OK
```

Values and sources:

| line | source |
|---|---|
| `context` | same basis as the toolbar label: last turn's `prompt_tokens` (or live `est_tokens` fallback) over the current role+mode window — reuse, don't recompute from scratch |
| `session` | `tokens_spent` from the session row (`self.sessions.get_owned(self.sid, self.owner)`), turn count = `len(history)//2` |
| `source` | `_last_usage.get("estimated")` flag from the final event |
| `budget` | `token_budget` from the same row; 0 → "none" |
| `compaction` | threshold hint derived live from the context fill ratio (§3) |

Before the first turn: `context` shows the live estimate, `session` shows
`0 tok · 0 turns`, `source` shows `none yet`. No error paths.

### 2.2 Post-turn meter line

After each completed turn (including via `/voice`, which wraps the same
`_run_turn`), print one dim line, e.g.:

```
ctx 23.4k/56.0k (42%) · session 96.1k tok · 7 turns · compaction soon (quick)
```

- Hook point: end of `_run_turn`, after `renderer.close()` and only when the turn
  was not cancelled.
- Suppressed under one-shot `bob chat --raw` (the meter is cockpit-only; the one-shot
  path is untouched).
- Voice mode: the line still prints (it's the cheapest place a voice user sees token
  health).

### 2.3 Compaction-imminent hint

Compaction has no event, so the hint is computed from the same fill ratio the meter
already uses:

- fill < 80% → no hint
- 80% ≤ fill < 95% → `compaction soon (<mode>)`
- fill ≥ 95% → `compaction imminent (<mode>)`

The mode in parentheses matters because Quick compaction truncates (lossy, cheap)
while Deep summarizes — the user should know which behavior is coming. Thresholds
are display constants, not changes to the real trigger (which stays in `bob_loop`'s
budget fitting).

## 3. Data flow (no new capture)

```
backend stream ──(usage in final event)──▶ shell._last_usage
                                             │
        record_turn() ──▶ session DB tokens_spent / token_budget  ──▶ /usage
                                             │
        context policy resolve(config, role, mode).window() ────▶ fill ratio ──▶ meter + hint
```

Everything above exists. The only new code is formatting and one read of the session
row.

## 4. Non-goals

- **No cost estimate.** There is no per-token rate table anywhere in the repo
  (`bob budget` reads LiteLLM's all-time `/global/spend`; locally that is $0 by
  design). Cost display stays the job of `bob budget`.
- **No compaction behaviour change** — thresholds here are display-only.
- **No session DB schema change** — reuse `tokens_spent` / `token_budget`.
- **No CLI verb** (`bob usage`): the CLI is one-shot with no session to report.
- **No new deps, config keys, or flags.**
- **One-shot `bob chat` untouched** (no meter line there; `--raw` semantics unchanged).

## 5. Implementation plan

Single file: `scripts/bob/shell.py`.

1. `_COMMANDS`: add the `_Cmd("/usage", …)` entry (§2.1).
2. Extract one helper `_ctx_fill()` returning `(used, window, pct, source_flag)` —
   shared by `_context_label()` (refactor its body to call it, preserving its
   existing format) and the new display paths, so the fill number can never
   disagree between the toolbar and `/usage`.
3. `_cmd_usage(self, arg)` — read the session row via
   `self.sessions.get_owned(self.sid, self.owner)`, call `_ctx_fill()`, print the
   block (§2.1) with theme colours.
4. `_meter_line()` — one-line format (§2.2); print at the end of `_run_turn`
   (post `renderer.close()`, `not cancelled`), reusing `_ctx_fill()`.
5. Token formatting helper: `23.4k` / `96.1k` style, one decimal below 1M, raw ints
   otherwise — reused by both displays.

Estimated size: ~80–120 lines in `shell.py`, one line in the `_COMMANDS` table.

## 6. Edge cases

| case | behaviour |
|---|---|
| First turn / no `_last_usage` yet | meter uses live `est_tokens` estimate (as the toolbar already does); `/usage` shows `0 tok · 0 turns`, `source: none yet` |
| Backend doesn't report usage (`estimated: True`) | both displays mark `~estimated` / `~` prefix, consistent with the toolbar's existing `~` |
| Mode switch mid-session (`/mode quick|deep`) | denominator changes with the policy; cumulative `tokens_spent` unaffected; next meter reflects the new window |
| Role switch (`/model`) | same: window is role+mode-scoped |
| Session resume (`/session resume`) | `tokens_spent` is persisted → meter and `/usage` correct immediately |
| Fill crosses a threshold then compaction fires | next turn's `prompt_tokens` drops (context trimmed); meter reflects post-compaction fill — the hint self-clears, no stored compaction state needed |
| `token_budget` reached | `over_budget` already blocks new turns (existing); `/usage` shows `cap reached`; meter prints the hint line regardless |
| Cancelled turn (Ctrl-C) | no meter line (turn produced no usage) |
| Voice turn | meter prints after speech, as in §2.2 |

## 7. Test plan

1. **Unit (pure functions):** `_ctx_fill` against a stub `_last_usage` / history
   (backend-reported, estimated fallback, empty history); token formatting
   (`1234 → 1.2k`, `999 → 999`, `1050000 → 1,050,000`); threshold hints at 79/80/94/95%.
   Run standalone via `python` (no pytest harness exists for the shell — follow the
   `smoke.py` convention: a short script asserting the helpers).
2. **Session round-trip:** temp DB, `create()` + `append_turn()` twice, `get_owned`
   returns cumulative `tokens_spent`; `/usage` output matches.
3. **Manual acceptance:** cockpit against the real local endpoint — one turn, run
   `/usage`; switch `/mode deep`, verify denominator; run `/session resume` and
   verify the meter; Ctrl-C a turn and verify no meter line; `--raw` one-shot stays
   bare.
4. **Regression:** existing `/help` listing, completion (`_slash_tree`), and
   `_context_label` output unchanged (the refactor keeps the toolbar's exact
   format string).

## 8. Open questions

- None blocking. If a cost line is ever wanted, it needs per-token rates
  (endpoint-derived or a rate table) — deliberately deferred as out of scope.
