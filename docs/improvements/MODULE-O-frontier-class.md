# Module O — Frontier Class (closing the capability gap)

**Status:** draft / not implemented. **Depends on:** **NB + NC + ND** (and NE for the interface it
surfaces in) — not just N. Per the roadmap and [ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md),
O runs *after* the portability track, so **every OS-touching O item is now dual-platform (Windows +
Linux)**, not Windows-only — this is reflected in the O5 estimate below. O reuses N's identity,
cancellation, streaming, observability, and the CI workflow NB6 created (C5 — O10 *extends* it).
**Read first:** ARCHITECTURE-CONTRACTS.md (C3 secrets for O8, C5 CI for O10, C6 skills for O11).

**Why this module exists.** Modules M and N made the harness *trustworthy* — reliable, safe,
owner-scoped, cancellable, observable, tested. They deliberately did **not** touch the agent loop
or the tool-execution model (an explicit non-goal in both). Measured against frontier agent
harnesses (Claude Code, Codex CLI, Cursor agent, Devin, OpenHands), the remaining distance is
therefore *architecture*, not hardening:

| Frontier dimension | Bob after N | Gap this module closes |
|--------------------|-------------|------------------------|
| Agent-loop sophistication | ~5.5/10 | no sub-agents, no parallel tool calls, no planning/reflection |
| Context management | ~5/10 | truncation only — no compaction/summarization, no per-agent context isolation |
| Tool safety / sandbox | ~7/10 | tools run in-process with full user rights; no OS sandbox; no granular permission model |
| Extensibility / MCP | ~6/10 | MCP *server* seam only (N10) — cannot *consume* external MCP servers |
| Auth / multi-tenancy | ~7.5/10 | shared-secret bearer, restart-to-revoke, no scopes/RBAC/rate limits |
| Observability | ~7/10 | structured file log — no distributed tracing / OTel spans |
| Testing / CI | ~7/10 | local pre-commit gate — no hosted CI, no agent-capability eval |

**Scope note — the non-goal is lifted here.** O1/O2/O4 change `run_agent_events` itself. That is
intentional and is the point of this module. Everything else (the OpenAI-compatible protocol, the
three-layer tool model, `bob.ps1` dispatch, local-first defaults) stays. This is a large module —
land it in the dependency order below, one verifiable sub-item at a time, exactly as M and N were.

## Overview

| Sub | Name | Gap addressed | Impact | Effort |
|-----|------|---------------|--------|--------|
| O6 | Permission / approval system | tool safety (granular authz) | HIGH | 4–5 h |
| O5 | OS-level tool sandbox (**Windows + Linux backends**) | tool safety (blast radius) | HIGH | 16–24 h |
| O2 | Parallel tool execution | agent-loop throughput | HIGH | 4–5 h |
| O3 | Context compaction | context management | HIGH | 5–6 h |
| O1 | Sub-agents / delegation | agent-loop sophistication | HIGH | 10–14 h |
| O4 | Planning + reflection + self-repair | agent-loop quality | MED | 6–8 h |
| O7 | MCP client (consume external servers) | extensibility | MED | 6–8 h |
| O8 | Frontier auth (token store, RBAC, limits) | multi-tenancy | MED | 6–8 h |
| O9 | OpenTelemetry tracing | observability | MED | 4–5 h |
| O10 | Agent eval harness (**extends** the CI matrix) | testing / regression | MED | 5–6 h |
| O11 | Skill execution engine (sub-agent-backed skills) | capability (from NE4 split, C6) | MED | 5–7 h |

**Total:** ~71–96 h (O5 grew for dual-OS sandbox; O11 added from the NE4 split). **After O:** the
reliability/safety axes are already 8; O lifts the three architecture axes (loop, context, sandbox)
to frontier and pushes the rest to 9–10.

---

## O6 — Permission / approval system (do first: the safety floor for more autonomy)

### Problem
Authorization is all-or-nothing per tool: `shell_run` always prompts ([shell.py](../../scripts/tools/shell.py));
`file_read`/`file_write` gate on path allowlists (N9); everything else runs unconditionally once the
tool is enabled. There is no per-tool *mode*, no per-owner policy, and no audit trail of what the
agent actually did. Granting the agent more autonomy (O1/O2/O5) without a real permission model is
how a prompt-injected tool result turns into damage.

### Change
- A `PermissionPolicy` resolved from config: per-tool mode `allow | ask | deny`, overridable
  per-owner (ties into N1 `owner_id`) and per-agent-depth (a sub-agent may be more restricted than
  the root). Default: read tools `allow`, mutating tools (`file_write`, `shell_run`, future
  `git_commit`) `ask`, network tools `allow` under the existing SSRF guard.
- Enforce in `ToolRegistry.dispatch_call` ([tool_registry.py](../../scripts/tools/tool_registry.py))
  — one choke point every tool call already passes through. `ask` returns a structured
  `permission_required` result the client/CLI resolves (the server surfaces it as an SSE
  `permission` event; the CLI prompts). `deny` returns a clean refusal the model can read.
- An append-only audit line per tool call (tool, args digest, decision, owner, run-id) through the
  N5 `bob.agent` logger — every mutation is attributable.
- Treat all tool output as untrusted input to the model (prompt-injection posture): document it and
  keep mutating actions behind `ask`.

### Effort: 4–5 h.
### Acceptance
Tests: a `deny` tool never dispatches; an `ask` tool yields a `permission` event/prompt and only
runs on approval; per-owner override applies; the audit log records every decision. Live: an agent
run that tries `file_write` under default policy pauses for approval.

---

## O5 — OS-level tool sandbox

### Problem
Tools run **in-process** with the full privileges of the `bob` user — `shell_run` spawns `pwsh`,
`file_*` touch the real filesystem, `web_fetch` uses the host network. The N9 allowlists/denylists
are *policy*, not *isolation*: a bug or a clever injection that reaches a mutating tool has the
user's full blast radius. Frontier harnesses run tools in a sandbox (restricted token / container /
VM) with an explicit escape hatch.

### Change
- A small `sandbox.py` seam — `run_sandboxed(cmd, cwd, limits) -> result` used by `shell.py` and any
  exec surface; read-only tools stay in-process (no benefit to sandboxing a pure read). It dispatches
  to a **per-OS backend** (via NB3's `osenv`), because O now ships on Windows *and* Linux:
  - **Windows backend:** a child process under a **restricted token** + a **Job Object**
    (CPU/memory/UI limits, filesystem view scoped to allow-listed roots, deny-by-default).
  - **Linux backend:** `bubblewrap`/`nsjail` if present (mount/pid/net namespaces + seccomp), else a
    `unshare`/rlimit-constrained child; a loud fallback to unsandboxed only when `agent.sandbox='off'`.
  - macOS: `sandbox-exec` profile (best-effort; deferred with the rest of macOS).
- Network egress policy for sandboxed tools reuses the N9 SSRF guard + the C3 OS-aware secrets denylist
  (a sandboxed shell must not reach `~/.ssh`/secrets even with a filesystem view).

### Effort: 16–24 h (**two full sandbox backends** now that Linux is a supported platform, not the
original Windows-only 8–12 h — the review's M1 correction).
### Acceptance
On **both** OSes: a sandboxed `shell_run` cannot write outside allow-listed roots or read a denied
secret even if the command tries; resource limits kill a runaway; `sandbox='off'` restores today's
behavior with a warning. Runs in ND2's Linux + Windows acceptance matrix. Documented in SECURITY.md
with the escape-hatch caveat and the per-OS backend matrix.

---

## O2 — Parallel tool execution

### Problem
When the model emits several independent tool calls in one step, the loop dispatches them
**sequentially** (the `for tc in tool_calls` loops in `run_agent_events`). N independent reads take
N× as long as one. Frontier harnesses fan them out.

### Change
- Dispatch the step's tool calls **concurrently** via a bounded `ThreadPoolExecutor`
  (cap = `min(cpu-2, agent.maxParallelTools)`), preserving result order when appending to
  `messages` so the transcript stays deterministic.
- Cancellation-aware: check the N3 `CancelToken` before submitting and abandon pending futures on
  cancel (a running tool can't be preempted, but no new ones start).
- Serialize mutating tools even within a parallel batch (a `deny`/`ask` from O6, or a
  `mutating=True` tool flag, forces sequential + confirmed execution); only side-effect-free tools
  run truly in parallel.
- Emit `tool_call`/`tool_result` SSE events as each completes (interleaved), tagged with the call id.

### Effort: 4–5 h.
### Acceptance
Tests: 3 independent fake tools each sleeping 100 ms complete in ~100 ms not ~300 ms; result order
in `messages` is stable; a mutating tool in the batch runs sequentially; cancel mid-batch stops new
dispatches. Deterministic transcript verified against the sequential baseline.

---

## O3 — Context compaction

### Problem
`truncate_history` (M7, [bob_loop.py](../../scripts/bob_loop.py)) keeps the message count/token
budget by **dropping oldest turns** — information is lost silently once a run gets long. Frontier
harnesses *compact*: summarize the dropped span into a compact note and keep it.

### Change
- When history crosses the token budget, summarize the oldest droppable span (tool-heavy
  assistant/user pairs first) into a single compact "conversation so far" system note instead of
  discarding it — reuse the existing summarizer (`bob_memory.cmd_summarize_session` / the chat role)
  rather than adding a model dependency.
- Always preserve the system prompt, the original goal, and the last K turns verbatim; compact only
  the middle. Cap the summary itself to a token budget so compaction can't re-overflow.
- Per-result compaction: large tool outputs get summarized-on-append past a size threshold (today
  they're hard-truncated at `maxToolResultTokens`), keeping the salient part.
- Config: `agent.compaction = 'summarize' | 'truncate'` (default `summarize`), `compactKeepLastTurns`.

### Effort: 5–6 h.
### Acceptance
Tests: a run that would drop 10 turns instead emits 1 summary note + keeps the last K; total tokens
stay within budget; the goal and system prompt survive; `compaction='truncate'` reproduces the
M7 behavior. A long multi-tool run no longer "forgets" an early decision (eval in O10).

---

## O1 — Sub-agents / delegation

### Problem
There is exactly one agent loop with one flat context. Complex tasks that a frontier harness
decomposes (spawn a focused sub-agent per subtask, each with its own clean context, synthesize the
results) run here as one ever-growing transcript — the single biggest capability gap.

### Change
- A first-class **`spawn_agent` tool** (Layer-1) that runs a nested `run_agent_events` with:
  - an **isolated** message history (fresh system + the delegated subtask; the parent's transcript
    is *not* inherited — the isolation is the point),
  - a **restricted** tool set + permission policy (O6) and a **depth cap**
    (`agent.maxAgentDepth`, default 2) to prevent unbounded recursion,
  - its own N3 `CancelToken` chained to the parent's (parent cancel → children cancel),
  - its own N5 run-id, logged as a child span of the parent (O9),
  - a **structured summary** returned to the parent (not the raw sub-transcript) so the parent's
    context stays small.
- Parent-side: the loop can issue several `spawn_agent` calls in one step and, via O2, run them in
  **parallel** — the fan-out/fan-in pattern frontier harnesses use.
- Reuses the session store (N2) for optional sub-run persistence and the metrics line (N5) per child.

### Effort: 10–14 h (this is the module's centerpiece; it composes O2 + O3 + O6 + O9).
### Acceptance
Tests: a parent goal spawns 2 sub-agents whose contexts don't leak into each other or the parent;
depth cap refuses a 3rd level; parent cancel propagates to children within ~1s; the parent receives
summaries, not raw transcripts; two sub-agents run concurrently (wall-clock < sum). An eval task
(O10) that decomposes cleanly scores higher with sub-agents than without.

---

## O4 — Planning + reflection + self-repair

### Problem
The loop is pure ReAct: think→act→observe until a final answer or `maxSteps`. There is no explicit
plan, no verification of the result, and no recovery when a step's tool fails — it just feeds the
error back and hopes. Frontier harnesses plan, then verify, then self-repair.

### Change
- Optional **plan phase** (`agent.plan = $true`): a first turn (planner role via `get_role`) emits
  a short step list injected as context; cheap and bounded, off by default for simple goals.
- **Reflection/verify pass**: before emitting the final answer, an optional critic turn asks
  "does this actually satisfy the goal / did any tool silently fail?"; on "no", the loop continues
  instead of returning. Bounded by `maxSteps`.
- **Self-repair**: on a tool contract/validation error, retry the *call* once with the error fed
  back as a `__parse_error__`-style correction (the mechanism already exists for malformed tool
  args) before giving up on that step.
- All three are config-gated and metered (N5) so their cost is visible; default profile keeps them
  off for latency-sensitive one-shots and on for `bob agent --deep`.

### Effort: 6–8 h.
### Acceptance
Tests: plan phase injects a plan without changing tool contracts; the verify pass catches a
deliberately-wrong answer and continues; a flaky tool succeeds on self-repair retry; all gated off
by default reproduce today's behavior. Eval (O10) shows a quality lift on multi-step tasks.

---

## O7 — MCP client (consume external MCP servers)

### Problem
N10 exposes Bob's tools *over* MCP (server). Bob cannot *use* the MCP ecosystem — it can't mount an
external MCP server's tools into its own registry, which is how frontier harnesses gain hundreds of
integrations without bespoke code.

### Change
- An MCP **client** that, per `agent.mcpServers` (name → stdio/SSE launch spec), connects at startup,
  lists the remote tools, and registers each as a synthetic entry in `ToolRegistry` (namespaced
  `mcp:<server>:<tool>`), so the agent loop and permission model treat them like any Layer-1 tool.
- `dispatch_call` routes `mcp:*` names to the client (JSON-RPC round-trip) with the N3 timeout +
  the O6 permission policy (remote tools default to `ask`).
- Health/reconnect handling; a failed server logs and is skipped (the loud-fail convention), never
  crashes startup.

### Effort: 6–8 h.
### Acceptance
Tests (against a fake in-process MCP server): remote tools appear in the registry, dispatch
round-trips, a dead server is skipped with a logged error, permission policy applies. Smoke:
mount the filesystem/fetch reference servers and call one tool end-to-end.

---

## O8 — Frontier auth: token store, RBAC scopes, rate limits

### Problem
N1 gave per-token ownership but tokens live in `bob.psd1` (revoke = edit + restart), every token has
the same capabilities, and there is no per-owner rate limiting. Frontier multi-tenancy needs
issuance/revocation without downtime, scopes (which tools/models/roles an owner may use), and limits.

### Change
- A small **token store** (SQLite, alongside `sessions.db`): `token_hash, owner, scopes, rate,
  created, revoked`. `_build_token_owner` (N1) reads it in addition to config; `bob agent token
  issue|revoke|list|scope` manages it. Hot revocation (checked per request) — no restart. Token
  values resolve through the **C3 secret seam** (env/keychain/`secrets.json`), never a tracked file;
  provider keys (DeepSeek/HF/Langfuse) resolve the same way — O8 unifies *all* credentials behind C3.
- **Scopes/RBAC**: an owner's allowed tools/roles gate the O6 permission policy and `get_role`; a
  request outside scope is refused. Composes with sub-agent restriction (O1).
- **Per-owner rate limits** enforced at the server boundary (token-bucket), returning 429.
- Config tokens (N1 shape) remain valid as a static fallback; the store is additive.

### Effort: 6–8 h.
### Acceptance
Tests: issue→use→revoke works without restart; an out-of-scope tool/role is refused; the rate limit
returns 429 then recovers; config-defined tokens still work. Live: revoke a token mid-session → next
call 401.

---

## O9 — OpenTelemetry tracing

### Problem
N5 gives a greppable structured log with a per-run id — excellent for one machine, but there are no
spans, no parent/child correlation across sub-agents (O1), and no export to a tracing backend.

### Change
- Emit **OTel spans**: one per run, child spans per step, per tool call, per sub-agent (O1), each
  carrying the N5 run-id, owner, model role, token estimate, and outcome.
- Export via OTLP to the existing Langfuse (`:3001`) / any collector, gated by
  `agent.tracing = $true`; when off, the N5 file log is unchanged (zero new dependency at rest).
- The SSE `run-id` header lets a client correlate its request to the trace.

### Effort: 4–5 h.
### Acceptance
Tests: spans are emitted with correct parent/child nesting for a sub-agent run and carry the run-id;
tracing off = no OTLP calls and identical file-log output. Live: a run appears as a trace tree in
Langfuse.

---

## O10 — Agent eval harness (extends the CI matrix)

### Problem
N8 gave a local `check.ps1` gate; NB6 created the CI workflow and ND2 added the fresh-install matrix
— but nothing measures *capability*, only correctness/install. Frontier harnesses gate on an
agent-task eval so a change that regresses reasoning/tool-use is caught.

### Change (extends the existing CI — C5, does not create a new workflow)
- An **agent eval harness**: a fixed set of scored tasks (tool-use, multi-step, sub-agent
  decomposition, refusal/permission) run against a pinned model — the **NC8 CPU model** for the
  hosted per-PR tier, a real local model for the GPU tier — with a mocked/records-based scorer,
  producing a capability score per commit.
- **Adds an `eval` job to the existing `.github/workflows/ci.yml`** (NB6 created it, ND2 added the
  matrix — O10 appends the eval job on that same matrix; **no new Windows-only runner**). Non-blocking
  first, then a threshold gate once baselines stabilize.
- Track the O1/O3/O4 wins quantitatively (they cite this eval in their acceptance).

### Effort: 5–6 h.
### Acceptance
The eval job runs on the existing Windows+Linux matrix; the harness produces a stable score locally
and in CI; a deliberately-regressed loop change drops the score. Documented in CONTRIBUTING.md.

---

## O11 — Skill execution engine (from the NE4 split, C6)

### Problem
NE4 builds the skills *registry + catalog* but explicitly defers *execution* of sub-agent-backed
skills to O (the chicken-and-egg the review flagged: a rich skill spawns a sub-agent = O1). Until
this lands, such skills are listed but report "requires O."

### Change
- A **skill executor** that runs a skill manifest as a scripted tool sequence and/or an O1 sub-agent
  sub-run, under the O6 permission policy and O8 scopes, traced by O9. Reuses `spawn_agent` (O1) for
  the sub-agent-backed case; simple tool-sequence skills reuse NE's stub executor.
- `bob skill <name> …` (non-interactive) and the NE shell's skill invocation both route here.
- Ships the first real sub-agent-backed skills (e.g. `code-review`, `deep-research`) as examples,
  authored against the NE4 contract.

### Effort: 5–7 h.
### Acceptance
A sub-agent-backed skill (e.g. `deep-research`) runs end-to-end: spawns sub-agents (O1), respects
permissions (O6) + scopes (O8), appears as a trace tree (O9), and returns a synthesized result;
the NE catalog now shows it as runnable, not "requires O".

---

## Traceability (frontier dimension → sub-item)

| Dimension (Bob after N) | Sub-item(s) |
|-------------------------|-------------|
| Agent-loop sophistication (5.5) | **O2** (parallel), **O1** (sub-agents), **O4** (plan/reflect/repair) |
| Context management (5) | **O3** (compaction), **O1** (per-agent context isolation) |
| Tool safety / sandbox (7) | **O6** (permission model), **O5** (OS sandbox) |
| Extensibility / MCP (6) | **O7** (MCP client) |
| Auth / multi-tenancy (7.5) | **O8** (token store, RBAC, rate limits) |
| Observability (7) | **O9** (OTel tracing) |
| Testing / CI (7) | **O10** (agent eval on the CI matrix) |
| Skills execution (NE4 split, C6) | **O11** |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| `scripts/tools/tool_registry.py` | O6 (enforce), O2 (parallel dispatch), O7 (mcp routing) |
| `scripts/bob_loop.py` | O2, O3, O1, O4 (loop changes) |
| `scripts/tools/shell.py`, new `scripts/sandbox.py` (Windows + Linux backends via `osenv`) | O5 |
| new `scripts/tools/spawn_agent.py` | O1 |
| new `scripts/bob_mcp_client.py` | O7 |
| `scripts/bob_agent_server.py`, new `scripts/bob_authstore.py` (uses C3 secret seam) | O8, O9 |
| `scripts/bob.ps1` / `scripts/bob/` | O6 (approve), O1 (`--deep`), O8 (`token` cmds) |
| `config/bob.psd1` / neutral config | O5, O6, O3, O1, O4, O7, O8, O9 keys |
| `.github/workflows/ci.yml` (**extend** — C5), new `tests/eval/` | O10 |
| new `scripts/bob_skills.py` executor (registry is NE's) | O11 |
| `tests/*` | every sub-item |
| `docs/SECURITY.md`, `docs/AGENT-SERVER.md`, `docs/TUNING.md`, `CONTRIBUTING.md` | docs |

## Verification (per item, as M/N)

- Python `py_compile` + the `unittest` suite; PowerShell AST parse; `scripts\check.ps1` gate (N8).
- Live `bob agent serve` smoke extended per item: approval pause (O6), sandboxed write refused (O5),
  parallel wall-clock (O2), compaction keeps early facts (O3), sub-agent isolation + parallel fan-out
  (O1), plan/verify/repair (O4), external MCP tool round-trip (O7), hot revoke + scope 429 (O8),
  trace tree in Langfuse (O9), CI + eval score (O10).
- `.\scripts\test-dry-run.ps1` stays all-green throughout. Cite `file:line` for every claim.

## Non-goals

A web UI; changing the OpenAI-compatible wire protocol or model routing/profiles; requiring cloud
(everything stays local-first, cloud-optional). The dispatch front door / command registry (NB4 owns
them, C1/C6 — O adds capability that surfaces through them, it doesn't touch dispatch). Full
VM-per-task isolation (O5 targets restricted-token/Job-Object on Windows, namespaces/seccomp on Linux
— not a hypervisor). A bespoke model — the eval (O10) pins an existing local role. Durable/resumable
long-horizon tasks, deep multimodal, and computer-use — those are **Module P**.
