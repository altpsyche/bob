# Module N — Road to 10/10

**Priority:** N7→N2→N1→N3→N6→N5→N4→N9→N8→N10 (dependency order). Source: the 10/10 rubric
(R1–R6) applied to the multi-user / MCP seam that Module M's M12/M15/M18 only stubbed. Every
rubric item maps to a sub-item — see the [Traceability](#traceability-rubric--sub-item) table.

**Scope note:** the *core* (agent loop, `ToolRegistry`, three-layer plugin model) stays as-is.
This module makes the harness safe to bind to `0.0.0.0` for multiple clients: owner-scoped
sessions, cancellable in-flight work, robust streaming, observability that reconstructs any
run from the log, a test-backed security review, and a thin MCP server that inherits it all.

**Conventions:** all new code follows [CONTRIBUTING.md](../../CONTRIBUTING.md) (atomic writes,
boundary error handling, single-source-of-truth defaults, exactly one terminal stream event,
one transient retry, `bob.agent` logger with a run-id).

## Overview

| Sub | Name | Finding addressed | Sev | Effort |
|-----|------|-------------------|-----|--------|
| N7 | Finish M6/M8 to 100% | `bob_memory` hardcodes `:8081`; `web.py`/plugins inline port fallbacks | LOW | 1 h |
| N2 | Session store hardening | single conn + coarse lock, no WAL/indices, `append_turn` lost-update race | MED | 3 h |
| N1 | Identity + ownership | any valid token accesses any `session_id` (no owner check) | HIGH | 4 h |
| N3 | Real cooperative cancellation | 600s in-flight call uncancellable; SSE ignores disconnect, records bogus turn | MED | 3 h |
| N6 | Streaming robustness | substring suppression is O(n²), marker-split-blind, swallows real answers | MED | 3 h |
| N5 | Observability | no log rotation; run-id absent from tool errors; no per-run metrics | MED | 2 h |
| N4 | Cold-start: measure then decide | `_REGISTRY_CACHE` per-process → dead for CLI/voice | LOW-MED | 3 h |
| N9 | Security review + close gaps | `file_read` reads `data/config.json` secrets; `git_*` reads any repo | HIGH | 3 h |
| N8 | Coverage gaps + CI/pre-commit | no hermes/openai wire tests; nothing runs the suite automatically | MED | 3 h |
| N10 | MCP server seam | MCP is a commented hint only | LARGE | 6 h |

**Total:** ~31 h.

---

## N7 — Finish M6/M8 to 100%

### Problem
`bob_memory.py:29-30` hardcodes `EMBED_URL`/`LITELLM_BASE` to `http://localhost:8081`;
`web.py:17` inlines `8888`; `plugins/{draft,summarise}/invoke.py` inline `8081` as the
`config.get("litellmPort", 8081)` fallback. Re-inlined port literals violate
`CONTRIBUTING.md §8` (single source of truth). M8 role-resolution is already 100%
(`bob_core.get_role`, `_models.ps1 Get-RoleForTask`).

### Change
Route every port through `bob_core._port(config, name)` (`bob_core.py:32-36`). In
`bob_memory.py`, replace the module constants with a lazy `_litellm_base(config=None)` that
calls `load_config()` when needed and `_port(cfg, "litellmPort")`; `embed`/`store`/`recall`/
`cmd_summarize_session` call it. Drop the literal fallbacks in `web.py` and the two plugins.

### Effort: 1 h.
### Acceptance
`grep -nE '(\?\? *8[0-9]{3})|localhost:8[0-9]{3}|"8[0-9]{3}"'` over `scripts/` + `plugins/`
(excluding the `bob_core.py`/`_models.ps1` default tables and `external/`) returns **zero**;
`bob recall` works with a non-default `litellmPort`; suite green.

---

## N2 — Session store hardening

### Problem
`bob_session.py` uses one shared sqlite connection + a coarse `threading.Lock`
(`:28-29`), no WAL, no indices. `append_turn` (`:75-90`) reads via `get()` *outside* the lock
then writes inside it → lost-update race under concurrent appends. `get`/`list_ids`/
`over_budget` read without the lock on the shared connection.

### Change
- Introduce `_ensure_schema(conn)` — the single create+migrate site N1 extends.
- `PRAGMA journal_mode=WAL` once at init; `PRAGMA busy_timeout=5000` per connection.
- **Thread-local connections** (`threading.local()`, `check_same_thread=True` per conn) so
  FastAPI's threadpool handlers each own a connection — no cross-thread cursor sharing, no lock.
- `append_turn` atomic via `BEGIN IMMEDIATE … COMMIT` (write lock before the SELECT).
- `create`/`delete` = one-statement autocommit writes; reads on the thread's own connection.
- Add `idx_sessions_updated_at`. Add `close()` (tests reference `_sessions._conn.close()` at
  `test_server.py:27` / `test_session.py:17`, which goes away); update both teardowns.

### Effort: 3 h.
### Acceptance
New `tests/test_session_concurrency.py` hammers create/append/read from N threads: **0 errors**,
`len(history) == N*PER*2` (no lost turns), `tokens_spent == N*PER`; existing session tests pass.

---

## N1 — Identity + ownership

### Problem
Auth is a flat shared secret: `_accepted_tokens` (`bob_agent_server.py:62,76-77`). Any valid
token can GET/DELETE/POST **any** `session_id` — `_load_session_or_404` (`:103-112`) has no
owner check. The `sessions.client` column (`bob_session.py:39`) is written but never enforced.

### Change
- **Config shape.** `agent.apiTokens` becomes a list of `@{ token=..; owner=.. }`; add
  `agent.defaultOwner = 'local'` (the owner `litellmKey` maps to). `Get-BobConfig` already
  serializes hashtable arrays via `ConvertTo-Json -Depth 10` — no PS change. Legacy compat:
  a bare-string entry maps token→token-as-owner.
- **Startup.** Replace `_accepted_tokens: set` with `_token_owner: dict`; build
  `{litellmKey: defaultOwner, **apiTokens}`; 401 for unknown tokens. Add
  `_authed_owner(authorization) -> str`.
- **Schema.** Extend `_ensure_schema`: `ADD COLUMN owner_id` guarded by `PRAGMA table_info`;
  backfill `owner_id = COALESCE(NULLIF(client,''), defaultOwner)`; add
  `idx_sessions_owner(owner_id, updated_at DESC)`. `create(owner_id=..)` stamps it. Add
  `get_owned(sid, owner)` / `delete_owned(sid, owner)`.
- **Enforcement.** Thread `owner` from each route (`Header`); `_load_session_or_404(sid, owner)`
  uses `get_owned` → **404** (never 403) for another owner's id. Apply at `get_session`,
  `delete_session`, `agent_completions`, `agent_completions_stream`; stamp on `create_session`.
  Cross-owner and unknown ids are indistinguishable (no existence leak).

Revocation = remove the entry from config + restart (built once at startup; acceptable).

### Effort: 4 h.
### Acceptance
Tests: owner-B gets 404 on owner-A's session for get/delete/complete/stream; own-session
roundtrip works; unknown vs unowned return identical 404; migration backfills `owner_id` from
an old-schema DB. Live: two tokens, cross-owner = 404.

---

## N3 — Real cooperative cancellation

### Problem
SIGINT handler is a no-op off the main thread (`bob_loop.py:306`); the flag is checked only at
step boundaries (`:484-488`) — a 600s in-flight LLM call can't be aborted. The SSE generator
(`bob_agent_server.py:199-216`) never checks `is_disconnected()` and its `finally` records a
turn even when `final_result` is `None`.

### Change
Add `CancelToken` (wraps `threading.Event`). Thread it through
`run_agent_events(..., cancel=None)` into the LLM call + tool dispatch. **Unify the paths**:
always `create(stream=True)` internally and drive `_consume_stream` (non-stream mode just
doesn't yield tokens) so cancel is polled between chunks and the stream `.close()`s within ~1s;
reuse the existing delta-assembly (`:350-367`). Keep the one transient retry only for the
non-emit path (never mid-stream). Check `cancel` before each tool. Point SIGINT at the same
token. **Server:** make `agent_completions_stream` async, add `Request`, poll
`request.is_disconnected()` → `cancel.cancel()`, bridge the sync generator via `anyio.to_thread`,
guard `_record_turn` on `got_final and final_result is not None`.

### Effort: 3 h.
### Acceptance
Fake slow client: cancel mid-stream → run stops <~1s, exactly one `final reason="cancelled"`,
no bogus turn, SIGINT handler restored; server disconnect: stops early, no `final` emitted,
`_record_turn` **not** called; real-final still records once.

---

## N6 — Streaming robustness

### Problem
`_consume_stream` (`bob_loop.py:331-368`) suppresses tokens via
`"<tool_call>" in "".join(parts)` — O(n²), blind to a marker split across chunks, and it
silently swallows a real final answer that contains the literal `<tool_call>`.

### Change
Per-format boundary detector. **OpenAI**: stream all content deltas, accumulate structured
`tool_calls` deltas (marker irrelevant). **Hermes**: a prefix-buffer state machine — emit
everything except the minimal tail that could be a prefix of `"<tool_call>"`; on a confirmed
marker switch to suppress+accumulate; raw content still feeds `_parse_hermes_tool_calls`. In
`finish()`, if no well-formed block parses, **flush the withheld buffer** so a final answer
that merely mentions the literal streams in full. Exactly one terminal event.

### Effort: 3 h.
### Acceptance
Tests: final answer containing literal `<tool_call>` streams in full with `final.result`
intact; real tool step suppresses markup but dispatches; marker split across two chunks
detected; mid-stream error → exactly one `error`, zero `final`; existing streaming test passes.

---

## N5 — Observability

### Problem
Plain `FileHandler`, no rotation (`bob_loop.py:293`); the run-id reaches log lines but not
tool-error strings/stderr previews; the server assigns no request id that threads into the
loop; there is no per-run metrics line.

### Change
Swap `FileHandler` → `RotatingFileHandler` with `agent.logMaxBytes`/`agent.logBackupCount`
config keys. Add a `run_id` param to `run_agent_events` so the server passes its request id
down (one id spans client→server→loop). Prefix `rid` onto tool-error stderr previews and log
tool results/errors with it (the returned tool string stays model-facing). Emit a run-end
metrics line: `[{rid}] done steps=N tools=M tokens~=T ms=E` (time via `time.monotonic()`,
tokens via the chars/4 estimate) plus a `registry_build_ms`/`import_ms` split for N4.

### Effort: 2 h.
### Acceptance
A single `grep "<rid>" logs/bob-agent.log` reconstructs a whole run; the log rotates at the
configured size; concurrent runs are distinguishable by rid.

---

## N4 — Cold-start: measure, then decide

### Problem
`_REGISTRY_CACHE` (`bob_loop.py:260-280`) is a per-process singleton, but CLI/voice spawn a
fresh process per call — so it never hits across calls, and the server already builds once at
startup and passes the registry in. Dead code for its stated purpose (rubric R3).

### Change
Using N5's metrics, log `import_ms` vs `registry_build_ms` on a cold CLI run and a voice-loop
utterance; record the numbers below. **Then** implement the winner:
- registry-build dominates → **persistent-worker forward** (CLI/voice detect a running
  `bob agent serve` and POST the turn to the hardened HTTP path; in-process fallback if down);
- import/interpreter dominates → **delete** `_get_or_build_registry`/`_REGISTRY_CACHE` and
  document that cross-process cold-start is import-bound; only the long-lived server warms.

No dead code survives either way.

### Effort: 3 h.
### Acceptance
This doc records the numbers and the decision; the chosen path is implemented; the losing
option's dead code is gone; suite green.

### Measurement + decision (2026-07-02)
Measured on this machine (venv-litellm, 10 tools loaded):

| Component | Cold (fresh process) | Warm (in a live process) |
|-----------|----------------------|--------------------------|
| Python interpreter startup | ~31 ms | — |
| Import chain (`bob_core`+`tool_registry`+`bob_loop`) | ~32 ms | ~0 (cached in `sys.modules`) |
| **Registry build** (import + `configure()` all tools) | **~140 ms** | **~16 ms** |

The registry build (~140 ms) is the dominant amortizable cost. But the per-process
`_REGISTRY_CACHE` captured **none** of it: every real caller either passes a prebuilt registry
(the server, built once at startup — bypasses the cache) or runs in a fresh process (CLI/voice —
the cache is empty on entry and discarded on exit). No caller invokes `run_agent` twice in one
process (verified by grep). So the cache was dead code.

**Decision: delete the dead cache; document `bob agent serve` as the warm path.** The long-lived
server already amortizes the build (~140 ms → ~16 ms warm) across turns — that is the worker.
Rather than add a CLI→server forwarding layer (a new network dependency + fallback on every CLI
call) for a ~140 ms local one-shot win, we removed `_get_or_build_registry`/`_REGISTRY_CACHE`
([`bob_loop.py`](../../scripts/bob_loop.py)), inlined the build, and documented that voice /
high-frequency clients should route through `bob agent serve` (which builds once). No dead code
remains; the metrics line (N5) logs `registry_build_ms` per run so the cost stays visible.

---

## N9 — Security review + close the found gaps

### Problem
`file_read`'s allowlist defaults to the repo root (`_models.ps1:127-128`), which exposes
`data/config.json` (holds `litellmKey` + `apiTokens`), `data/sessions.db`, `*.psd1`, and
`logs/`. `git_*` (`git.py:28-43`) accepts **any path** on disk → read any git repo's
status/log/diff.

### Change
Write [docs/SECURITY.md](../SECURITY.md) covering the tool surface (`file_read` scope,
`file_write` disabled-by-default, `shell_run` fail-closed no-stdin, `git`/`fabric` constraints,
`web_fetch` SSRF guard, N1 auth + ownership, a `0.0.0.0` exposure checklist). Back every claim
with a test. Close the two real gaps:
- **Secrets denylist** in `file.py`: deny `data/config.json`, `data/*.db`, `*.psd1`, `logs/`,
  `.env*` even within an allowed root.
- **Path allow-listing** in `git.py`: default to the repo root; extra roots via
  `agent.gitAllowedRoots`.

`fabric` pattern names are validated by fabric itself (documented, no code change);
`shell_run` stays fail-closed (`shell.py:24-27`).

### Effort: 3 h.
### Acceptance
Tests: `file_read` denies `data/config.json` and `*.psd1` even when the repo root is allowed;
`git_status` outside allowed roots is refused; SSRF test stays green. Each SECURITY.md claim
maps to a named test.

---

## N8 — Coverage gaps + CI/pre-commit

### Problem
No test for the hermes `<tool_response>` wire format or the OpenAI (non-hermes) tool path;
nothing runs the suite automatically (no `.github/`, `.git/hooks` has only samples).

### Change
Add `tests/test_agent_loop.py` cases for the hermes wire format (`bob_loop.py:564-576`) and the
OpenAI path (`build_assistant_message`/`build_tool_message`, `:240-255`/`:232-237`) — cancel and
concurrency tests land with N3/N2. Add `scripts/check.ps1` (runs `py_compile` over
scripts+plugins+tests, the `unittest` suite, and `[Parser]::ParseFile` over every `*.ps1`;
non-zero on any failure). Version `scripts/hooks/pre-commit` (calls `check.ps1`) +
`scripts/install-hooks.ps1` (since `.git/hooks` isn't tracked). Wire `check.ps1` into
`test-dry-run.ps1` section [11].

### Effort: 3 h.
### Acceptance
A broken tool / failing test / PS syntax error makes `check.ps1` (and the installed hook) exit
non-zero and block; a clean tree passes; `test-dry-run.ps1` green.

---

## N10 — MCP server seam

### Problem
MCP is a commented hint (`bob.psd1:84-86`); the multi-user/MCP goal wants a real seam.

### Change
Add `scripts/bob_mcp_server.py` — a thin MCP server (stdio + optional SSE) exposing Bob's
registry tools over MCP, reusing `ToolRegistry` + the hardened agent (inherits N1 ownership and
N3 cancellation). Gate behind `agent.mcpEnabled = $false`; add a `bob agent mcp` CLI entry.
Smoke test: list-tools + one round-trip against a fake client.

### Effort: 6 h.
### Acceptance
Smoke test lists Bob's tools over MCP and completes one tool round-trip; disabled by default;
suite green.

---

## Traceability (rubric → sub-item)

| Rubric item | Sub-item(s) |
|-------------|-------------|
| **R1** No unauth / cross-tenant path, even at `0.0.0.0` | **N1** (ownership 404), **N9** (secrets denylist, git allow-list, `0.0.0.0` checklist) |
| **R2** Every public surface tested + CI/pre-commit | **N8** (+ per-item tests in N1/N2/N3/N6/N9) |
| **R3** Cold-start/cache/logging pays off on real hot paths; observability for 3 clients | **N4** (measure→decide), **N5** (rotation, run-id propagation, metrics) |
| **R4** No re-inlined literals / duplicated logic | **N7** |
| **R5** Streaming + sessions survive adversarial input, disconnect, concurrency | **N2** (concurrency), **N3** (disconnect/cancel), **N6** (adversarial streaming) |
| **R6** Security review, test-backed | **N9** |
| *Goal: MCP future* | **N10** |

## Files Modified (by sub-item)

| File | Sub-items |
|------|-----------|
| `scripts/bob_memory.py` | N7 |
| `scripts/tools/web.py` | N7 |
| `plugins/{draft,summarise}/invoke.py` | N7 |
| `scripts/bob_session.py` | N2, N1 |
| `scripts/bob_agent_server.py` | N1, N3, N5 |
| `config/bob.psd1` | N1, N5, N9, N10 |
| `scripts/bob_loop.py` | N3, N6, N5, N4 |
| `scripts/tools/file.py` | N9 |
| `scripts/tools/git.py` | N9 |
| `scripts/bob.ps1` | N10 |
| `scripts/check.ps1` (new), `scripts/hooks/pre-commit` (new), `scripts/install-hooks.ps1` (new) | N8 |
| `scripts/bob_mcp_server.py` (new) | N10 |
| `docs/SECURITY.md` (new) | N9 |
| `tests/*` | N1, N2, N3, N6, N5, N8, N9, N10 |

## Verification

```powershell
# Python
tools\venv-litellm\Scripts\python.exe -m py_compile <changed files>
tools\venv-litellm\Scripts\python.exe -m unittest discover -s tests

# PowerShell AST parse (changed *.ps1)
[System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errs)

# Regression + gate
.\scripts\test-dry-run.ps1        # section [11] runs the suite
.\scripts\check.ps1               # non-zero on any failure (N8)

# Live smoke (bob agent serve): 401 without bearer; 404 cross-owner (N1); SSE stream;
# disconnect stops the run and records no turn (N3); final answer with literal "<tool_call>"
# streams in full (N6); two concurrent clients distinguishable by run-id in the log (N5).
```
