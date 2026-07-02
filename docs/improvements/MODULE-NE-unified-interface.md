# Module NE — Unified `bob` Interface (one front door)

**Status:** 🚧 in progress (2026-07-02). **NE0** (control-plane hardening — bidirectional approval
protocol, RunContext, linked cancel, registry filtered-view, + SSRF/schema/result fixes) ✅ done.
**NE1** (command registry reconciled to the true grouped catalog + parity gate) ✅ done. **NE3**
(catalog renderer, `scripts/bob/catalog.py`) ✅ done. **NE4** (skills registry `scripts/bob_skills.py`
+ seed skills + `skill` command) ✅ done. Remaining: **NE2** shell, **NE5** sessions, **NE6** help. **Depends on:** NB
(dispatch front door + command registry, C1/C6), NC (cross-platform orchestration), ND (reliable,
released base). **Precedes:** O → P — a coherent, extensible interface is the surface O's capabilities
land in. **Read first:**
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) — NE does **not** define the front door or the
command registry (NB4 does, C1/C6); NE *extends* them with grouping, the interactive shell, and the
catalog. Skills: NE builds the *registry + catalog rendering*; **O** builds skill *execution* (C6).

**Why this module exists.** Bob has grown ~30 user-facing verbs bolted onto a PowerShell `switch`
in `bob.ps1` — `chat`, `voice`, `describe`, `agent` (+ `serve`/`mcp`/`schedule`/`log`/`tools`/…),
`summarise`, `draft`, `search`, `play`, `clip`, `recall`, `up`, `status`, `services`, `gen`,
`doctor`, … There is no single coherent front door: no live catalog of what Bob can do, no
interactive session, no consistent help. Frontier tools (Claude Code, Hermes-Agent) present **one
command** that opens an interface showing the model, the session, and the full tool/skill catalog,
with an interactive prompt. NE gives Bob that: **one `bob` that has everything in one place.**

**What makes this cheap: Bob already auto-discovers.** The `ToolRegistry` builds the tool list at
startup — so the "Available Tools" catalog renders for free. NE is mostly *presentation +
interaction* over machinery that already exists, plus a small command registry so the verb list is
data, not a hardcoded menu.

**Design constraint — build it extensible, not hardcoded.** This is the one real risk of shipping a
polished UI before O: O will add sub-agents, permission prompts (O6), and parallel-tool progress
that the interface must show. So NE renders from **registries** (tools from `ToolRegistry`, commands
from a command registry, skills from a skills registry) — new capability appears in the UI by
registering, never by editing the shell. O then plugs in without a rewrite.

**Division of labor (per C1/C6).** NB4 owns the front door: the `bob` shim, `config/verbs.json`
routing, the `scripts/bob/` package, and the **command registry**. NE does **not** re-define any of
that. NE adds the *experience* on top: grouping the registry for humans, the interactive shell
(the no-arg entry NB4 reserves for it), and the catalog. On both OSes the user types `bob`; the shim
routes (orchestration → pwsh, runtime/interactive → `python -m bob`) — that contract lives in C1, not
here.

**Scope note.** NE unifies the *interface*. It does not add agent capability (that's O), change the
runtime, or own dispatch. The "skills" concept splits per C6: **NE = skills registry + catalog
rendering** (cheap, feeds the splash); **O = skill execution** (a skill may spawn a sub-agent, an O1
consumer). NE never executes a sub-agent-backed skill — it lists it and hands execution to the runtime.

## Overview

| Sub | Name | Turns scattered verbs into… | Impact | Effort |
|-----|------|-----------------------------|--------|--------|
| NE1 | Command **grouping + help** over NB4's registry | one coherent, grouped, self-describing CLI | MED | 3–4 h |
| NE2 | Interactive REPL / TUI shell | `bob` (no args) → splash + live prompt | HIGH | 8–12 h |
| NE3 | Auto-rendered catalog (tools/commands/skills) | the Hermes-style "here's everything" screen | MED | 3–4 h |
| NE4 | Skills **registry + catalog** (execution → O) | named workflows *listed* in the catalog | MED | 2–3 h |
| NE5 | In-shell sessions + memory continuity | resume, owner-scoped, remembers context | MED | 4–6 h |
| NE6 | Help / onboarding / first-run | `/help`, discoverability, guided start | LOW | 2–3 h |

**Total:** ~22–32 h. (NE1 shrank — the registry is NB4's; NE4 shrank — execution moved to O.)

---

## NE1 — Command grouping + help over NB4's registry

### Problem
NB4 (C6) builds the command registry and the `bob`/`verbs.json` dispatch — but it's flat data.
Humans need it *grouped* and *self-describing*; there's no mental-model organization or generated help.

### Change
- **Add `group` metadata + rendering** on top of NB4's command registry (do **not** rebuild the
  registry — C6 says it lives in NB4). Groups mirror the mental model: *Talk* (chat/voice/describe),
  *Act* (agent/serve/mcp), *Make* (draft/summarise/search/play/clip), *Know* (recall/memory),
  *Run* (up/status/services/doctor), *Config* (gen/models/profile).
- Feed NE3's catalog and NE6's help from this grouped view — one source of truth ("what can Bob do")
  that is generated, never hand-maintained.
- Dispatch itself (shim + `verbs.json` + `python -m bob` package) is NB4's; NE1 only adds the
  human-facing organization.

### Effort: 3–4 h (shrank: the registry + dispatch are NB4's; this is grouping + rendering).
### Acceptance
`bob help` shows every verb grouped, generated from the registry (a test asserts help == registry
contents); no verb is missing vs the NB4 registry; no behavior regressions.

---

## NE2 — Interactive REPL / TUI shell

### Problem
There is no interactive Bob. Every action is a fresh process invocation; no persistent session, no
live surface, no "just start talking."

### Change
- `bob` with **no args** launches an interactive shell: a splash (model/role, session id, tool +
  skill counts, upstream/version — the screenshot's header), then a prompt. In-shell you can: chat,
  run an agent goal (`/agent …` or natural language), invoke a tool/skill, switch model role, check
  `/status`, and see streamed output (reusing N3/N6 streaming). Slash-commands for control
  (`/help`, `/agent`, `/model`, `/session`, `/tools`, `/exit`).
- Rendered with a lightweight cross-platform TUI lib (e.g. `rich`/`prompt_toolkit` — pure Python,
  works under Windows Terminal and Linux). No PowerShell in the interactive path.
- Designed extensibly: the render loop consumes the same `run_agent_events` event stream, so O's
  future events (sub-agent spawned, permission required, parallel tool progress) surface by handling
  new event types — no shell rewrite.

### Effort: 8–12 h.
### Acceptance
`bob` opens the splash + prompt on both OSes; a chat turn streams; an agent goal runs with live
tool/step output; slash-commands work; Ctrl-C cancels the in-flight turn (N3) and returns to the
prompt, not the OS. Non-interactive `bob <cmd>` still works unchanged (scripts/CI).

---

## NE3 — Auto-rendered catalog (tools / commands / skills)

### Problem
There's no way to see, at a glance, everything Bob offers — the exact gap the screenshot fills.

### Change
- Render the catalog from the registries: **Tools** from `ToolRegistry` (already auto-discovered),
  **Commands** from NE1's registry, **Skills** from NE4's registry (when present). Grouped, counted
  ("31 tools · 79 skills"), shown on the splash and via `bob tools` / `bob skills` / `/help`.
- Honest about load state: tools that failed to load (registry `errors`) are shown as such, not
  hidden — extends the N5 observability posture into the UI.

### Effort: 3–4 h.
### Acceptance
The splash and `/help` list every discovered tool/command (and skill, if NE4) with counts; a
deliberately broken tool appears as failed, not missing; counts match the registries.

---

## NE4 — Skills registry + catalog (execution → O)

Per **C6**: NE builds the skills *registry + catalog rendering*; **O** builds skill *execution*. This
split removes the chicken-and-egg (a rich skill spawns a sub-agent, which is O1) the review flagged.

### Problem
Bob has *tools* (atomic actions) but no *skills* — named, composable, higher-level workflows (the
screenshot's "code-review", "deep-research", "architecture-diagram"). The catalog screen wants to
*show* them. But a skill's *execution* model can't be finalized before O1 (sub-agents) exists, so
building execution now would lock a shape O has to break.

### Change (NE = registry + rendering only)
- A **skills registry** parallel to `ToolRegistry`: auto-discovery + contract validation of skill
  *manifests* (`{name, group, summary, params}`), so skills render in the catalog (NE3) and `bob
  skills` / `/help` for free — the "79 skills" line in the screenshot.
- **Execution is deferred to O** (a new O sub-item): a skill runs as a scripted tool sequence and/or
  a sub-agent sub-run (O1). NE ships the registry + a stub executor that runs *simple* tool-sequence
  skills only, and hands sub-agent-backed skills to O's executor when it lands.
- Document the skill authoring contract alongside the tool one (AUTHORING.md).

### Effort: 2–3 h (shrank: execution moved to O).
### Acceptance
A skill *manifest* registers and appears in the catalog + `bob skills`; a simple tool-sequence skill
runs; a sub-agent-backed skill is *listed* and cleanly reports "requires O (sub-agents)" until O's
executor exists; a broken manifest is a hard contract error (tool convention).

---

## NE5 — In-shell sessions + memory continuity

### Problem
The interactive shell needs a conversation that persists and resumes — otherwise it's just a fancy
one-shot. Bob has the pieces (N sessions, owner-scoped; the memory store) but they aren't wired into
an interactive front end.

### Change
- The shell runs against an N session (owner = the local user via the N1 identity): shows the
  session id, supports `/session new|list|resume <id>`, and persists turns (owner-scoped, N1/N2).
- Memory continuity: on start, optionally inject relevant memories (the M14 path) and offer
  `/remember`; on exit, the existing session-summarize flow runs (if `memory.autoSummarize`).

### Effort: 4–6 h.
### Acceptance
A shell conversation persists across `/exit` and resumes with `/session resume`; turns are
owner-scoped; memory recall/summarize hooks fire when enabled; budgets (N) are respected.

---

## NE6 — Help / onboarding / first-run

### Problem
Discoverability is poor and there's no guided first-run.

### Change
- `/help` (in-shell) and `bob help` (CLI) render grouped commands + the catalog from the registries
  (NE1/NE3) — always accurate because it's generated. A short first-run panel points at `bob doctor`
  (NC7) and the top commands.

### Effort: 2–3 h.
### Acceptance
`help` output is generated from the registries (no stale hand-written list); first run shows the
guided panel once.

---

## Traceability (goal → sub-item)

| Goal | Sub-item(s) |
|------|-------------|
| One coherent, grouped, cross-OS command surface | **NE1** |
| One interactive front door (`bob` → splash + prompt) | **NE2** |
| See everything Bob can do, at a glance | **NE3**, **NE6** |
| First-class composable workflows (like the screenshot's skills) | **NE4** (phased, with O) |
| Persistent, resumable, owner-scoped conversations | **NE5** |
| Extensible so O's features plug in without a rewrite | **NE2** (event-driven render) + the registries (**NE1/NE3/NE4**) |

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| `scripts/bob/` command registry (grouping metadata — registry itself is NB4's) | NE1 |
| new `scripts/bob/shell.py` (REPL/TUI, in the NB4 package) | NE2, NE5, NE6 |
| `scripts/tools/tool_registry.py` (catalog introspection); new `scripts/bob_skills.py` (registry + rendering; execution is O's) | NE3, NE4 |
| `plugins/AUTHORING.md`, `docs/USAGE.md`, `docs/DAY-IN-THE-LIFE.md` | NE4, docs |
| `tests/*` (grouping/help == registry, shell event handling, catalog) | every sub-item |

## Verification

- Non-interactive parity: every legacy verb works through the unified entry on Windows + Linux
  (enumerated test); scripts/CI unaffected.
- Interactive smoke: `bob` → splash → chat streams → agent goal runs → Ctrl-C cancels to prompt →
  `/session resume` works. Runs under Windows Terminal and a Linux terminal.
- Catalog matches the registries; broken tool/skill shown as failed.
- `check.ps1` (N8) + the NB6 core suite green on both OSes. Cite `file:line` for every claim.

## Non-goals

Agent capability (Module O — NE surfaces it, doesn't build it). Skill *execution* (O, per C6 — NE
lists skills, O runs the sub-agent-backed ones). The dispatch front door / command registry (NB4
owns them, C1/C6 — NE only groups + renders). A GUI/web app (this is a terminal interface).
Replacing the OpenAI-compatible API. Rewriting `bob.ps1`'s orchestration *logic* (it stays; NB4
thinned the dispatch, NE gives it a coherent human face).

---

## Where it sits in the circle

```
N ✓ → NB → NC → ND → NE → O → P
                     │     └ capability (surfaces IN the NE interface) → frontier product
                     └ one `bob`: splash + live catalog + interactive REPL, cross-OS
```

NE is the last foundation piece: after ND you have a *reliable, cross-platform* Bob; NE makes it
*coherent to use*. Build the shell **registry-driven** (tools/commands/skills as data) so Module O's
sub-agents, permission prompts, and parallel tools appear by registering — never by rewriting the UI.
