# Module M — Agent Harness Quality & Reliability

**Priority:** Highest-leverage items are M1–M5 (Do Now). Source: architectural + quality
audit of the agent harness (2026-07-01), both review passes. Every audit finding maps to
a sub-item here — see the [Traceability](#traceability-finding--sub-item) table.

**Scope note:** the *core* (agent loop, ToolRegistry, three-layer plugin model) is sound —
this module fixes the drifting periphery: config/doc truth, reliability edges, and the
seams needed to scale past a single local user. It does **not** rearchitect the core.

## Overview

| Sub | Name | Finding addressed | Sev | Effort |
|-----|------|-------------------|-----|--------|
| **Do Now** | | | | |
| M1 | Kill config/doc drift | `agent.tools` dead check + stale CLAUDE.md | HIGH | 1 h |
| M2 | Atomic `config.json` write | non-atomic rewrite in empty `catch{}` | HIGH | 30 m |
| M3 | Guard LLM response + timeouts + 1 retry | `resp.choices[0]` IndexError; no timeout/retry | MED | 2 h |
| M4 | Wrap `bob_memory` HTTP calls | `embed()`/summarize traceback on server-down | MED | 45 m |
| M5 | Harden agent server | `0.0.0.0` no-auth → info-disclosure + SSRF | MED | 1 h |
| **Do Next** | | | | |
| M6 | Single source of truth for ports/defaults | literal `?? <port>` duplicated ~dozen sites | MED | 2–3 h |
| M7 | Token-aware context + schema budget | msg-count truncation + per-turn all-tool schema bloat | MED | 4–5 h |
| M8 | Shared routing helper | role-resolution duplicated 6+ places | MED | 1.5 h |
| M9 | Loud-fail edges | XML arg drop, contract mismatch, web_fetch SSRF, voice/vision crash | MED | 2.5 h |
| M10 | Retire `llm.ps1`, de-dup CLI | legacy twin + copy-pasted PID/port logic | MED | 2 h |
| M11 | Real startup pre-flight (`bob doctor`) | validation optional + incomplete | MED | 2–3 h |
| M17 | Regenerate `config.json` only when stale | full re-parse+rewrite on every call | MED | 1.5 h |
| **Do Later** | | | | |
| M12 | Session + auth abstraction | no multi-user / MCP seam | LARGE | 8–12 h |
| M13 | Test harness | manual `test()` only | MED | 4–5 h |
| M14 | Wire memory into agent loop (RAG) | persona claims context it never injects | MED | 3 h |
| M15 | Streaming in agent loop/server | `stream=False` hardcoded | MED | 3 h |
| M16 | Low-severity cleanup batch | empty catches, races, cosmetics | LOW | 2 h |
| M18 | Cold-start & operational hardening | CLI registry rebuild, no cancel, no structured logs | LOW-MED | 5 h |

**Total:** Do Now ~5 h · Do Next ~16 h · Do Later ~26 h (~47 h).

---

# Do Now — quick wins, high leverage

## M1 — Kill the config/doc drift (the drift fix)

### Problem
Tool registration moved from an `agent.tools` allowlist to a `disabledTools` denylist, but
three artifacts disagree:
- **Code (correct):** auto-discovery + `disabledTools`. [`tool_registry.py:40-91`](../../scripts/tools/tool_registry.py), config key [`bob.psd1:67`](../../config/bob.psd1).
- **Dead check:** [`bob.ps1:732`](../../scripts/bob.ps1) iterates `$bobCfg.agent.tools` — a key that does not exist → `?? @()` is empty → the loop never runs → check #12 always prints "Agent tool files present ✓" while validating nothing.
- **Stale doc:** `.claude/CLAUDE.md` "Registration rule" still says *"add to `agent.tools`… loader silently skips unregistered names."* A contributor (or Claude) following it adds a no-op list and misdiagnoses load failures.
- **AUTHORING.md is already correct** — it is the source of truth.

**Root cause:** the setup check re-implements tool discovery in PowerShell instead of asking
the Python loader. Fix = delegate to the one discovery path.

### Change
**`.claude/CLAUDE.md`** — replace the Registration rule block:
```md
**Registration rule:** No manual registration. Tools auto-discover from
`scripts/tools/*.py` (Layer 1) and `plugins/*/tool.py` (Layer 2). Creating the file is the
only step. To exclude one without deleting it, add its stem/dir name to
`agent.disabledTools` in `config/bob.psd1`. The loader prints a startup summary and tracks
load errors. See plugins/AUTHORING.md.
```

**`scripts/bob.ps1:730-739`** — replace the dead `agent.tools` loop with a call to the real
discovery, honoring `disabledTools`:
```powershell
# 12. Tools load cleanly (delegate to the Python loader — single source of discovery)
$disabled = ($bobCfg.agent.disabledTools ?? @()) -join ','
$listOut  = & $venvPy (Join-Path $repo 'scripts\tools\tool_loader.py') --list --disabled $disabled 2>&1
$loadErrs = @($listOut | Select-String -Pattern 'load error')
Show-Check 'Agent tools load without error' ($loadErrs.Count -eq 0) 'run: bob agent tools'
if ($loadErrs) { $loadErrs | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkYellow } }
```

### Effort: 1 h (incl. re-run `bob setup check` and confirm a deliberately-broken tool is now caught).

---

## M2 — Atomic `data/config.json` write

### Problem
[`_models.ps1:104-109`](../../scripts/_models.ps1) regenerates the file **all** Python depends
on, on **every** `bob` invocation, via non-atomic `Set-Content`, inside an empty `catch {}`:
- Two concurrent `bob` calls can interleave writes → Python reads truncated JSON.
- Any write failure (locked file, full disk) is invisible → Python silently reads stale config.

### Change
```powershell
# _models.ps1, replacing the current try/catch that writes config.json:
try {
  $jsonPath = Join-Path $script:ModelsRepo 'data\config.json'
  $jsonDir  = Split-Path $jsonPath
  if (-not (Test-Path $jsonDir)) { New-Item $jsonDir -ItemType Directory -Force | Out-Null }
  $tmp = "$jsonPath.$PID.tmp"
  $base | ConvertTo-Json -Depth 10 | Set-Content $tmp -Encoding UTF8
  Move-Item -LiteralPath $tmp -Destination $jsonPath -Force   # atomic on same volume
} catch {
  Write-Warning "Failed to write data/config.json: $_  (Python tools may use stale config)"
}
```
`$PID` in the temp name prevents two processes clobbering the same temp file; `Move-Item -Force`
is an atomic replace within a volume.

### Effort: 30 m.

---

## M3 — Guard the LLM response + add client-side timeouts

### Problem
- [`bob_loop.py:230`](../../scripts/bob_loop.py) — `msg = resp.choices[0].message` with no
  length check → `IndexError` raw traceback if a completion returns empty `choices` (a real
  local-server failure mode), escaping the `try` that ends at line 228.
- **No `timeout=` on any `chat.completions.create`:** agent loop ([`bob_loop.py:220`](../../scripts/bob_loop.py)),
  and all three plugins ([`draft/invoke.py:70`](../../plugins/draft/invoke.py),
  [`search/invoke.py:62`](../../plugins/search/invoke.py),
  [`summarise/invoke.py:39`](../../plugins/summarise/invoke.py)). The only backstop is the
  proxy's `request_timeout: 600` ([`gen-litellm.ps1:75-76`](../../scripts/gen-litellm.ps1)) —
  a 10-minute hang with no client escape.

### Change
Agent loop — add a config-driven timeout, one transient retry, and guard empty choices:
```python
# bob.psd1 agent block: add  requestTimeout = 600
# CRITICAL: default must be >= the proxy's request_timeout (600s) — thinking models
# (planner/R1) can run >2 min before first output (see Module L). A low value (e.g. 120)
# would kill legitimate long calls. Make it role-aware if you want tighter caps on fast roles.
_timeout = int(agent_cfg.get("requestTimeout", 600))
for attempt in range(2):                      # one retry on transient connection error
    try:
        resp = client.chat.completions.create(
            model=effective_role,
            messages=messages,
            tools=tool_schemas if tool_schemas and not hermes_mode else None,
            stream=False,
            timeout=_timeout,
        )
        break
    except Exception as e:
        if attempt == 0 and _is_transient(e):    # connection reset / 502 / 503
            print(f"[retry] transient LLM error: {e}", file=sys.stderr); continue
        print(f"LLM error at step {step + 1}: {e}", file=sys.stderr)
        return None, exit_requested
...
if not resp.choices:
    print(f"LLM returned no choices at step {step + 1}", file=sys.stderr)
    return None, exit_requested
msg = resp.choices[0].message
```
Plugins — pass `timeout=` on each `create()` (read from
`config.get("agent",{}).get("requestTimeout", 600)`; same "≥ proxy" rule). One-line change per plugin.

### Effort: 2 h (4 call sites + config key + retry helper + verify a thinking call is not cut off).

---

## M4 — Wrap `bob_memory` HTTP calls

### Problem
[`bob_memory.py:59-62`](../../scripts/bob_memory.py) (`embed()`) and
[`:180-186`](../../scripts/bob_memory.py) (`cmd_summarize_session`) call the embed/LLM servers
with a timeout but **no try/except** → `ConnectionError`/`KeyError` raw traceback when a server
is down. The agent path is shielded (dispatch catches all), but direct `bob remember` /
`bob recall` surface the traceback.

### Change
Catch at the boundary and raise/return a coherent message:
```python
def embed(text: str) -> list[float]:
    try:
        resp = requests.post(EMBED_URL, json={"model": EMBED_MODEL, "input": [text]},
                             headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except (requests.RequestException, KeyError, IndexError) as e:
        raise RuntimeError(f"Embedding server unreachable/malformed at {EMBED_URL}: {e}") from e
```
Then in `cmd_store` / `cmd_recall` / `cmd_summarize_session`, wrap the call and print a clean
error + `return` (CLI) — the agent-facing `memory_recall`/`memory_store` in `bob_core` already
funnel through the dispatch catch, so they inherit the clean message.

### Effort: 45 m.

---

## M5 — Harden the agent server

### Problem
[`bob_agent_server.py:103`](../../scripts/bob_agent_server.py) binds `0.0.0.0:8084` with **no
auth**; [`/v1/agent/completions`](../../scripts/bob_agent_server.py) runs the full tool loop.
`shell_run` is *not* an RCE vector (it requires interactive confirmation and fails closed with
no stdin — [`shell.py:22-27`](../../scripts/tools/shell.py)), but the endpoint still exposes
`file_read` (scoped to repo root, which includes `data/config.json`), `web_fetch` (SSRF),
`git`, `fabric`, `memory` to anyone on the LAN. **Info-disclosure + SSRF, not RCE** — hardened
accordingly.

### Change
- Default bind to loopback; require the litellm key as a bearer token; gate `0.0.0.0` behind
  an explicit config flag.
```python
# read once at startup from config
_bind = _config.get("agent", {}).get("serveHost", "127.0.0.1")
_key  = _config.get("litellmKey", "sk-local")

from fastapi import Header
@app.post("/v1/agent/completions", response_model=AgentResponse)
def agent_completions(req: AgentRequest, authorization: str = Header(default="")):
    if authorization != f"Bearer {_key}":
        raise HTTPException(status_code=401, detail="Unauthorized")
    ...

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=_bind, port=int(_config.get("agent", {}).get("agentPort", 8084)))
```
- Add `serveHost = '127.0.0.1'` to the `agent` block in `bob.psd1`; document that exposing to
  `0.0.0.0` also requires `web_fetch` hardening (M9).

### Effort: 1 h.

---

# Do Next — structural

## M6 — Single source of truth for ports/defaults

### Problem
The real defaults live in [`models.psd1:81-86`](../../config/models.psd1), but literal
fallbacks (`?? 8081`, `?? 8080`, …) are duplicated across ~a dozen sites:
[`bob.ps1:8-9`](../../scripts/bob.ps1), [`bob_core.py:42`](../../scripts/bob_core.py)/`:51`,
[`_models.ps1:92-97`](../../scripts/_models.ps1), [`gen-litellm.ps1:88`](../../scripts/gen-litellm.ps1),
and the setup-check block. They don't break at runtime (models.psd1 is always present) but each
is a silent-divergence trap and a source of confusion.

### Change
- Treat `Get-BobConfig`'s injected block ([`_models.ps1:92-97`](../../scripts/_models.ps1)) as
  the **only** place literals live; everywhere else read the resolved value.
- In `bob.ps1`, replace `$d.port ?? 8080` etc. with the already-resolved `(Get-BobConfig).port`.
- In Python, `bob_core` should treat missing keys as a hard error (config.json always has them
  post-M2), not silently default — or centralize the defaults in one `_DEFAULTS` dict.
- Add a `bob gen`-time assertion that every port key is present in the merged config.

### Effort: 2–3 h.

## M7 — Token-aware context management

### Problem
Two coupled scaling cliffs, both about tokens:
- [`truncate_history:119-126`](../../scripts/bob_loop.py) trims by **message count**
  (`maxHistoryMsgs=40`), ignoring token size. A few large tool outputs overflow the model
  context and the step fails opaquely.
- The Hermes system prompt injects **every** tool's JSON schema on **every** turn
  ([`bob_loop.py:82-93`](../../scripts/bob_loop.py)). Tool count therefore inflates the prompt
  linearly — at 30 plugins the fixed overhead alone can dominate a small local context window,
  compounding the truncation problem. This is why M7 is the true first cliff, not M12.

### Change
- Add a lightweight token estimate (chars/4 heuristic, or `tiktoken`/model tokenizer if
  available) and truncate to a `maxContextTokens` budget (new config key), always keeping the
  system message and dropping oldest tool/assistant pairs first.
- Truncate individual tool results to a per-result token cap before appending (the 4000-char
  cap at [`tool_registry.py:200`](../../scripts/tools/tool_registry.py) is a start but is chars,
  not tokens, and not budget-aware).
- Bound the schema overhead: emit compact schemas (drop descriptions past N tools, or select a
  relevant subset per goal) so tool-count growth doesn't silently eat the context window.

### Effort: 4–5 h.

## M8 — Shared routing helper

### Problem
Role resolution from `config.routing` (with its own default fallback) recurs in
[`draft/invoke.py:65`](../../plugins/draft/invoke.py),
[`search/invoke.py:51`](../../plugins/search/invoke.py),
[`summarise/invoke.py:36`](../../plugins/summarise/invoke.py), and three `bob.ps1` sites
(chat [`:373-378`](../../scripts/bob.ps1), vision `:831`, voice `:862`). Adding a role means
editing six places.

### Change
- Python: add `get_role(config, task, pro=False)` to `bob_core.py`; the three plugins import it.
- PowerShell: add `Get-RoleForTask -Config c -Task chat|code|think|vision|voice -Pro` to
  `_models.ps1`; `bob.ps1` calls it. One routing table, one place.

### Effort: 1.5 h.

## M9 — Loud-fail edges

### Problem (four small, related failure-hiding bugs)
1. Malformed XML tool args silently become `{}` ([`bob_loop.py:60-63`](../../scripts/bob_loop.py)) instead of self-correcting like the JSON path does.
2. Contract mismatch only warns; a `TOOL_DEFS` name with no `DISPATCH` entry loads anyway ([`tool_registry.py:136-142`](../../scripts/tools/tool_registry.py)) and fails at call time.
3. `web_fetch` accepts `file://` and `localhost` (SSRF) ([`web.py:41`](../../scripts/tools/web.py)); `git`/`fabric` accept any path/pattern.
4. Voice loop ([`bob.ps1:878-901`](../../scripts/bob.ps1)) and `describe`/vision (`:831`) have no try/catch around `Invoke-BobStream` → LLM-down crashes the session instead of printing a message and continuing.

### Change
1. Route the XML `except json.JSONDecodeError` through the same `__parse_error__` mechanism.
2. Make missing-DISPATCH a hard contract error (skip the tool + record error), matching the other contract checks.
3. `web_fetch`: allowlist `http`/`https` schemes and block RFC-1918 / loopback hosts unless a config flag opts in.
4. Wrap the stream calls; on error print a coherent line and `continue` (voice) / `return` (describe).

### Effort: 2.5 h.

## M10 — Retire `llm.ps1`, de-duplicate the CLI

### Problem
[`llm.ps1`](../../scripts/llm.ps1) duplicates `bob.ps1`'s header, config bootstrap, and
`status` command verbatim (~400 overlapping lines) — every fix must be applied twice. Within
`bob.ps1`, PID-read/`Stop-Process`/delete blocks are copy-pasted across service branches
([`:467-482`](../../scripts/bob.ps1), `:916-937`), and TCP port checks are inlined despite
`Test-PortInUse` existing in `_models.ps1`.

### Change
- Delete `llm.ps1`, or reduce it to a thin alias that dot-sources shared functions from a new
  `scripts/_bob-lib.ps1` (extract `Invoke-BobStream`, `Format-ForSpeech`, routing, port/PID helpers).
- Replace copy-pasted stop logic with a `Stop-ServiceByPid -Name -PidFile` helper; use
  `Test-PortInUse` everywhere instead of inline TCP checks.

**Non-goal:** the 60+ branch `switch` in `bob.ps1` is *not* decomposed here. It's a working
dispatch; extracting shared *functions* (above) removes the duplication that actually causes
bugs, while a full split of the switch is high-risk churn with little payoff. Revisit only if
per-command logic keeps growing.

### Effort: 2 h.

## M11 — Real startup pre-flight (`bob doctor`)

### Problem
Validation runs only when a user explicitly types `bob setup check`, and it's incomplete (no
GPU/VRAM, no endpoint reachability, no write-permission check). Post-M1 it will at least be
honest; this makes it a genuine pre-flight the agent path can call.

### Change
- Rename/alias to `bob doctor`; run the existing checks plus: endpoint `/models` reachable,
  GPU/VRAM (via `Get-GpuVramGB`), `logs/` + `data/` writable, config.json parses.
- Have the agent path (`bob agent`) run a fast subset before the loop and print one coherent
  line on failure instead of relying on downstream tracebacks.

### Effort: 2–3 h.

## M17 — Regenerate `config.json` only when stale

### Problem
[`Get-BobConfig`](../../scripts/_models.ps1) rewrites `data/config.json` on **every** `bob`
invocation — including read-only `bob status` and every recursive turn of the voice loop
(which calls `bob.ps1` per utterance, [`bob.ps1:880`](../../scripts/bob.ps1)). Each call
re-parses all `*.psd1`, re-serializes JSON, and hits disk. M2 makes the write *safe*; this
makes it *unnecessary* most of the time — a design + scalability fix.

### Change
- Regenerate only when a source is newer than the output:
```powershell
$srcMax = (Get-Item $script:ModelsFile, $script:BobFile, $userFile -EA SilentlyContinue |
           Measure-Object LastWriteTimeUtc -Maximum).Maximum
if (-not (Test-Path $jsonPath) -or (Get-Item $jsonPath).LastWriteTimeUtc -lt $srcMax) {
    # ...atomic write from M2...
}
```
- Keeps config.json correct after any edit, but turns the hot path (status, voice turns,
  scheduled agent runs) into a cheap `Test-Path` + timestamp compare.

### Effort: 1.5 h.

---

# Do Later — bets for the multi-user / MCP future

## M12 — Session + auth abstraction
No session store, per-user isolation, or auth today; the shared-registry server is single-shot
and stateless, and MCP is only a commented hint ([`bob.psd1:75-77`](../../config/bob.psd1)).
Introduce a `Session` object (id, history, budget) persisted to SQLite, and an auth layer
(reuse the litellm key or add per-client tokens) before any multi-user or MCP work. **8–12 h.**

## M13 — Test harness
Testing is manual `test()` functions run via CLI; no pytest suite. The validated-contract +
injected-registry design is unusually test-friendly — add a fixture that builds `ToolRegistry`
against a fake config and asserts every tool imports/configures, plus per-tool unit tests that
mock the LLM/HTTP. Wire into `test-dry-run.ps1`. **4–5 h.**

## M14 — Wire memory into the agent loop (or fix the claim)
The persona prompt promises *"memories provided in context"* ([`bob.psd1:11`](../../config/bob.psd1)),
but `run_agent` builds `[system, user]` only ([`bob_loop.py:207-210`](../../scripts/bob_loop.py)) —
memory is wired into the **chat REPL** ([`bob.ps1:407-420`](../../scripts/bob.ps1)) but not the
agent. Either inject `memory_recall(goal)` into the agent's system context (gated on
`memory.enabled`) or drop the claim from the agent-mode prompt. **3 h.**

**Blocker — confirmed API mismatch (found during Do-Now, 2026-07-01):**
[`bob_core.py:72`](../../scripts/bob_core.py)/`:83` call `bob_memory.store(...)` /
`bob_memory.recall(...)`, but [`bob_memory.py`](../../scripts/bob_memory.py) only defines
`cmd_store` / `cmd_recall` — so `bob_core.memory_store` / `memory_recall` raise `AttributeError`.
Dormant today (`memory.enabled=$false`), but it breaks the instant memory is turned on or M14
wires recall into the agent. **Architecturally-right fix:** give `bob_memory.py` importable
`store()`/`recall()` core functions; `cmd_store`/`cmd_recall` (CLI) *and* `bob_core` both call
them — the same "logic in one place" rule the plugins follow (`invoke.py` core + thin callers).
Do this as the first step of M14 (or fold into M16 if M14 slips). ~30 min.

## M15 — Streaming in the agent loop/server
`stream=False` is hardcoded ([`bob_loop.py:224`](../../scripts/bob_loop.py)); only the CLI chat
path streams. Add optional SSE streaming for the agent loop and an SSE variant of the server
endpoint for responsive UIs. **3 h.**

## M16 — Low-severity cleanup batch
- Document/justify the empty catches: [`bob.ps1:70`](../../scripts/bob.ps1), `:232`, `:425`, `:442`, `:479`; [`_models.ps1:98`](../../scripts/_models.ps1).
- [`play/tool.py:52-53`](../../plugins/play/tool.py) bare `except Exception: pass` → log the swallow.
- `.last-agent-result.txt` write ([`bob_loop.py:320`](../../scripts/bob_loop.py)) → temp+rename (same pattern as M2).
- `cmd_store` dedup race ([`bob_memory.py:74-94`](../../scripts/bob_memory.py)) → accept as best-effort or wrap in a transaction.
- Fold the CLI's `_iter_all_tools` ([`tool_loader.py:41-54`](../../scripts/tools/tool_loader.py)) into the registry so discovery lives in one place.
- Model-role display order ([`_models.ps1:147`](../../scripts/_models.ps1)) — cosmetic; leave or derive from profile keys. **2 h total.**

## M18 — Cold-start & operational hardening

### Problem
Three operational seams that only bite at scale (many plugins / concurrent or scheduled runs):
- **CLI registry rebuild:** the `bob agent` path builds the registry fresh every invocation
  ([`bob_loop.py:174-180`](../../scripts/bob_loop.py)) — imports + `configure()` for *all*
  tools per call. The server caches it; the CLI (and voice loop) do not. At 30 plugins this is
  real per-call latency.
- **No graceful cancellation:** Ctrl-C mid-step kills the process; an in-flight tool/LLM call
  isn't unwound and `.last-agent-result.txt`/history may be left mid-write.
- **No structured logging:** failures are scattered `stderr` prints. Debugging 3 concurrent
  users or a scheduled task means grepping interleaved output with no request/session id.

### Change
- Cache the built registry to a module-level singleton keyed by config hash (or a small on-disk
  import cache) so repeated CLI calls in one session skip re-import.
- Install a `SIGINT` handler in `run_agent` that finishes the current tool result, flushes state
  via the M16 temp+rename, and exits cleanly with a message.
- Route agent/tool events through `logging` with a per-run id to `logs/bob-agent.log` (structured
  lines), keeping the human `stderr` previews for interactive use.

### Effort: ~5 h.

---

## Traceability (finding → sub-item)

| Review finding | Sub-item |
|----------------|----------|
| `agent.tools` dead check + stale CLAUDE.md (drift) | **M1** |
| `data/config.json` non-atomic write in empty catch | **M2** |
| `resp.choices[0]` unguarded IndexError | **M3** |
| No client-side LLM timeout (loop + 3 plugins + memory) | **M3, M4** |
| `bob_memory` embed/summarize HTTP unhandled | **M4** |
| Agent server `0.0.0.0`, no auth (info-disclosure + SSRF) | **M5** |
| Port literal fallbacks duplicated ~dozen sites | **M6** |
| Token/context management (msg-count only) | **M7** |
| Model routing logic duplicated 6+ times | **M8** |
| Silent XML tool-arg drop | **M9** |
| Contract mismatch only warns | **M9** |
| Tools policy-permissive (web_fetch SSRF, git/fabric) | **M9** (+M5) |
| Voice/vision crash on LLM-down | **M9** |
| `llm.ps1` legacy twin | **M10** |
| Duplicated PID-stop / inline port checks | **M10** |
| Startup validation optional + incomplete | **M11** |
| No session/auth/async seams (MCP) | **M12** |
| No test harness | **M13** |
| Memory not injected in agent loop; persona misleads | **M14** |
| No streaming in agent loop | **M15** |
| Empty catches / play bare-except / races / cosmetics | **M16** |
| `shell_run` unusable non-interactively | *documented in M5; behavior is intentional (fail-closed)* |
| Model-role display order hardcoded | **M16** (cosmetic) |
| *Plan-review gaps (2026-07-01):* | |
| No client-side retry on transient LLM error | **M3** (added) |
| Per-turn all-tool schema bloat at N plugins | **M7** (added) |
| `config.json` regenerated on every call | **M17** (added) |
| CLI registry rebuild per invocation | **M18** (added) |
| No graceful cancellation (Ctrl-C mid-step) | **M18** (added) |
| No structured logging / observability | **M18** (added) |
| Monolithic `switch` not decomposed | **M10** non-goal (intentional) |
| Error-handling *convention* (not just point fixes) | *open — recommend a CONTRIBUTING note* |
| `bob_core.store/recall` vs `bob_memory.cmd_*` mismatch (confirmed bug) | **M14** blocker (add importable `store()`/`recall()` core) |

## Files Modified (by sub-item)

| File | Sub-items |
|------|-----------|
| `.claude/CLAUDE.md` | M1 |
| `scripts/bob.ps1` | M1, M6, M8, M9, M10, M11 |
| `scripts/_models.ps1` | M2, M6, M8, M10, M17 |
| `scripts/bob_loop.py` | M3, M7, M9, M14, M15, M16, M18 |
| `scripts/bob_core.py` | M3, M6, M8 |
| `scripts/bob_memory.py` | M4, M16 |
| `scripts/bob_agent_server.py` | M5, M12, M15 |
| `scripts/tools/tool_registry.py` | M7, M9, M16, M18 |
| `scripts/tools/tool_loader.py` | M16 |
| `scripts/tools/web.py` | M9 |
| `plugins/{draft,search,summarise}/invoke.py` | M3, M8 |
| `plugins/play/tool.py` | M16 |
| `config/bob.psd1` | M3, M5, M7 |
| `scripts/llm.ps1` (+ new `scripts/_bob-lib.ps1`) | M10 |
| `scripts/gen-webui.ps1` | M9 (exit-code check) |
| `scripts/test-dry-run.ps1` | M13 |

## Verification

```powershell
# M1 — drift: break a tool, confirm setup check now catches it
'raise SyntaxError' | Add-Content scripts/tools/web.py   # temporary
bob setup check          # "Agent tools load without error" should be RED
git checkout scripts/tools/web.py

# M2 — atomic config: hammer concurrent writes, confirm valid JSON every time
1..20 | ForEach-Object -Parallel { bob status | Out-Null }
Get-Content data/config.json | ConvertFrom-Json | Out-Null   # must not throw

# M3 — timeout + empty choices: point litellm at a dead backend, run agent
bob agent "say hi"       # coherent "LLM error"/"no choices", not a traceback

# M4 — memory: stop embed server, run recall
bob recall "anything"    # coherent "Embedding server unreachable", not a traceback

# M5 — server: confirm loopback + auth
bob agent serve &
curl http://localhost:8084/v1/agent/completions -d '{"goal":"hi"}'   # 401 without bearer

# Regression
.\scripts\test-dry-run.ps1
```
