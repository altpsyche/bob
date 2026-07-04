# Module S — Front-Door Unification (one `bob`, one loop)

**Status:** draft — Tier-1 (text) cleared to implement; Tier-2 (voice/vision) delegated to **P3**.
**Depends on:** **O — all shipped** (the agent loop is the unification target), NE (the interactive
shell / dispatch / command registry), NE6-MEM / MEM2 (memory + sessions). **Read first:**
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) (C1 dispatch, C6 registries) and
[MODULE-NE-unified-interface.md](MODULE-NE-unified-interface.md).

**Why this module exists.** The agent loop (M13+) was built as a *new* execution path beside the
pre-existing PowerShell chat/voice REPLs; the two never merged. C1 flagged this as a deliberate *phased
migration* — [registry.py:12-14](../../scripts/bob/registry.py): "chat/voice/describe/recall are pwsh
today and **stay so until a later module ports them to Python**." That later module is S. Today Bob has
two conversational front doors with very different capability:

- **`bob agent` / `bob` (the NE2 shell)** — the full loop: tools, MEM-3/autoRecall/BOB.md + MEM-4
  consolidation, O2/O3/O5/O6/O9/…, persisted owner-scoped sessions, rich streaming, approval.
- **`bob chat` / `code` / `think` / `voice` / `describe` / …** — pwsh REPLs
  ([bob.ps1:535-650,969-1099](../../scripts/bob.ps1)) that call `/chat/completions` directly via
  `Invoke-BobStream`: **no tools, no auto-memory** (only a manual `!recall`), a *separate* streaming
  implementation, and a *divergent* memory path (`bob-memory.ps1 summarize-session` vs the loop's
  `consolidate_session`).

So the "harness" is split, with duplicated + divergent streaming and memory logic, and `bob chat` — the
obvious "talk to Bob" verb — is the *lesser* door. S closes this so there is **one `bob`, one loop**,
with **zero feature loss**.

**Scope split (by prerequisite).** The unification divides cleanly:
- **Tier 1 — text (`chat`/`code`/`think`): this module.** No prerequisite — the loop already runs a
  pure no-tools chat turn today ([bob_loop.py:1190-1194,1365-1384](../../scripts/bob_loop.py)); it just
  needs a chat-mode seam + the dispatch flip + the missing flags.
- **Tier 2 — voice/vision (`voice`/`describe`/`screenshot`/`listen`/`transcribe`/`speak`): P3.** These
  need **multimodal-in-loop** (`run_agent_events` cannot accept an image today — there is no `image_url`
  handling anywhere in `bob_loop.py`) and a `/voice` shell mode. P3 already scopes "deep multimodal in
  the loop"; S **delegates** the voice/vision doors to it rather than duplicating that work early.

**Non-goals — verbs that correctly stay pwsh.** Infra/orchestration (`up`/`build`/`fetch`/`stop`/
`models`/`doctor`/`services`…) is not conversational and must not be agentified. Memory CRUD
(`recall`/`remember`/`memory`/`budget`) stays a thin DB CLI. S touches only the conversational doors.

## Overview

| Sub | Name | What | Impact | Effort |
|-----|------|------|--------|--------|
| S1 | Chat-mode seam in the loop | `no_tools` + `max_tokens` params (default off/None → byte-identical) | HIGH | 2–3 h |
| S2 | `chat`/`code`/`think` → Python | registry flip (C1) + `cli.py` handler: one-shot via loop, REPL via shell chat-mode; `--pro/--think/--code` via `get_role`; `--max/--raw/--sys`/legacy | HIGH | 3–4 h |
| S3 | Retire the pwsh chat REPL | remove the dead `chat/code/think` switch cases; keep `Invoke-BobStream` (voice/vision still use it until P3) | MED | 1–2 h |
| S4 | (delegated) Voice/vision → **P3** | documented hand-off, not implemented here | — | — (P3) |

**Total (Tier 1):** ~6–9 h. **After S:** `bob chat/code/think`, `bob agent`, and `bob` (shell) all run
the *same* `run_agent_events`; one streaming impl; one memory path; the pwsh chat REPL is gone.

---

## S1 — Chat-mode seam in the loop

### Problem
A unified `bob chat` should be "the loop, but no tools." The loop already returns the model's answer
after one streamed turn when no tools are present ([bob_loop.py:1365-1384](../../scripts/bob_loop.py))
and only appends the Hermes tool addendum `if hermes_mode and tool_schemas`
([:1190-1194](../../scripts/bob_loop.py)) — so a **no-tools** run is a plain chat completion today. Two
small things are missing: a clean way to force no-tools, and an output-token cap (`--max`).

### Change
- Add `no_tools: bool = False` to `run_agent_events` / `run_agent`. When set, use an **empty registry
  view** (`registry.filtered(allow=[])` — the O1/NE0 `_RegistryView` seam
  [tool_registry.py](../../scripts/tools/tool_registry.py)) so `tool_schemas == []`, no addendum, no
  tool loop — without globally disabling tools. Default `False` → **byte-identical** to today.
- Add `max_tokens: int | None = None`, threaded into the completion request kwargs (today the loop
  sends no `max_tokens`, so output is uncapped). `None` → unchanged; a value caps generation (`--max`).
- Keep everything else (persona, roles, memory injection, streaming, cancel, history) as-is — a chat
  turn inherits all of it for free.

### Effort: 2–3 h.
### Acceptance
Tests (fake client): `no_tools=True` yields `tool_schemas==[]` and a single `final` with no
`tool_call`; `no_tools=False` unchanged; `max_tokens=N` appears in the request kwargs, `None` omits it;
memory/persona/history still apply in no-tools mode. `run_agent` returns the answer.

---

## S2 — `chat` / `code` / `think` → Python (dispatch flip + handler)

### Problem
These three verbs are `runtime: pwsh` ([registry.py:28-33](../../scripts/bob/registry.py)) and fall
through to the `bob.ps1` REPL. To unify, they must route to Python and drive the loop — while preserving
every user-facing flag/behavior (parity audit below).

### Change
- **Registry flip (C1):** set `chat`/`code`/`think` to `runtime: "python"` + a `handler` key; add the
  handler(s) to `cli.py` `_HANDLERS`; regenerate `config/verbs.json` via `python -m bob.registry` (the
  `--check` gate enforces sync). Both shims then route `bob chat …` → `python -m bob chat …`.
- **Handler** (`_handle_chat`, one function keyed by task=chat|code|think):
  - **Role:** resolve `--pro`/`--think`/`--code` → a concrete role via `bob_core.get_role(config, task,
    pro)` ([bob_core.py:57-72](../../scripts/bob_core.py) — already mirrors the pwsh `Get-RoleForTask`).
    *Critical:* the loop's default role is `agent`/Hermes, **not** `chat` — the handler must route or the
    model silently changes.
  - **One-shot** (`bob chat "prompt"`): `run_agent(goal, role=…, no_tools=True, max_tokens=…,
    stream=not --raw)`; `--raw` prints the bare result (pipe-safe, no previews); else stream + newline.
  - **Interactive** (`bob chat` no prompt): launch the **NE shell in chat mode** (role preset +
    `no_tools`) — inheriting persisted sessions, MEM-3/autoRecall/consolidate, rich streaming, and
    approval, which is strictly better than the pwsh REPL's manual `!recall` + `Invoke-BobStream`.
  - **Flags/legacy:** `--max N`, `--raw`, `--sys <text>` (system override), and the legacy
    `bob chat <knownRole> <prompt>` explicit-model form.

### Effort: 3–4 h.
### Parity map (pwsh feature → unified handling; zero loss)

| pwsh feature (bob.ps1) | Unified handling |
|------------------------|------------------|
| `--pro`/`--think`/`--code` routing (:537-539,571-572) | `get_role(config, task, pro)` in the handler |
| one-shot prompt (:578-584) | `run_agent(no_tools=True, …)` |
| `--raw` pipe-safe output (:581-582) | handler prints bare result, `stream=False` |
| `--max N` (:544-548) | S1 `max_tokens` param |
| `--sys <text>` + legacy `<role> <prompt>` (:551-566) | handler flags; `role=` accepts any model id |
| REPL + empty-line-exit + header (:586-598) | NE shell (better REPL: sessions/memory/render) |
| `!recall` single-slot (:601-615) | shell autoRecall (auto) + optional `/recall` slash (polish) |
| `!memory` status (:618-621) | shell `/status` (+ existing `bob memory status`) |
| autoSummarize-on-exit (:628-637) | shell MEM-4 `consolidate_session` on exit (already wired) |
| spinner / "bob serve" error (:82-116) | loop preflight error + shell renderer |

### Acceptance
Tests: `bob chat "hi"` (one-shot) routes to the `chat` role, no tools, streams an answer; `--pro`/
`--code`/`--think` resolve the right role via `get_role`; `--raw` emits bare text; `--max` caps output;
`bob chat` (no args) launches the shell in chat mode. `verbs.json` in sync (gate). Live: `bob chat`,
`bob code "…"`, `bob think --pro "…"` all run through the loop.

---

## S3 — Retire the pwsh chat REPL

### Problem
Once `chat`/`code`/`think` route to Python, the `bob.ps1` switch cases for them
([bob.ps1:535-650](../../scripts/bob.ps1)) are dead code — a second, divergent implementation of "talk
to a model" (its own streaming + its own `bob-memory.ps1` path).

### Change
- Remove the dead `chat`/`code`/`think` switch cases from `bob.ps1`. **Keep `Invoke-BobStream`** — it's
  still used by `voice`/`describe`/`speak` until P3 ports those.
- The command-registry parity test (switch ⊆ registry) still passes: the verbs remain registered (now
  `python`), only their pwsh *cases* are removed.
- One streaming implementation (`_consume_stream`) and one memory path (`consolidate_session`) remain.

### Effort: 1–2 h.
### Acceptance
Tests: the command-registry parity test passes with the cases removed; `bob chat` still works (routes to
Python). No reference to the removed cases remains. Gate green.

---

## S4 — Voice / vision → P3 (delegated, not implemented here)

`voice`/`describe`/`screenshot`/`listen`/`transcribe`/`speak` stay pwsh until **P3** because they need
work S deliberately does not do:
- **Multimodal-in-loop:** `run_agent_events` must accept `image_url` message content (none today) before
  `describe`/`screenshot` can move onto the loop — this is P3's "vision in the loop."
- **`/voice` shell mode:** listen→turn→speak wrapping the shell's `_run_turn`, preserving the P3/voice
  watch-list (spoken system prompt, `Format-ForSpeech` markdown-stripping, whisper auto-start, `--no_think`
  fast reply, the `exit_voice_tools`/exit-42 hook, Windows-only capture/playback → cross-OS).

S records this hand-off; P3's spec absorbs it. After P3, the pwsh voice/vision cases retire too and the
unification is complete.

## Files (new / touched — projected)

| File | Sub-items |
|------|-----------|
| `scripts/bob_loop.py` (`no_tools`, `max_tokens` params) | S1 |
| `scripts/bob/cli.py` (`_handle_chat` + `_HANDLERS`), `scripts/bob/registry.py` (flip 3 verbs), `config/verbs.json` (regen) | S2 |
| `scripts/bob/shell.py` (chat-mode entry: role preset + no_tools) | S2 |
| `scripts/bob.ps1` (remove dead chat/code/think cases; keep Invoke-BobStream) | S3 |
| `config/defaults.json` (any chat defaults, e.g. chat maxTokens), `tests/*` | S1–S3 |

## Verification

- Python `py_compile` + unittest; `scripts\check.ps1` gate (incl. the `verbs.json` sync check and the
  command-registry parity test); `.\scripts\test-dry-run.ps1`; on Windows **and** Linux/WSL.
- New loop params default to today's behavior (`no_tools=False`, `max_tokens=None`) — `bob agent`/shell
  unchanged.
- Live: `bob chat "…"`, `bob code`, `bob think --pro`, `bob chat` (→ shell chat mode); confirm memory +
  streaming come from the one loop.
- Cite `file:line` for every claim.

## Non-goals

Agentifying infra/orchestration or memory-CRUD verbs (they correctly stay pwsh). Voice/vision (Tier 2 →
P3). Changing the OpenAI-compatible protocol or the model routing table. Removing `Invoke-BobStream`
while voice/vision still use it.
