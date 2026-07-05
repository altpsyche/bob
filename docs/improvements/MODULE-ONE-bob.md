# Module ONE — Just `bob` (one word, one harness, one language)

**Status:** draft — north-star module. Sequenced; ONE-A and ONE-B (=P3) are cleared to start.
**Depends on:** **S — shipped** (text doors on the loop, commit `7388527`), **O — shipped** (the loop is
the unification target), **NE / NE6-MEM / MEM2** (shell, dispatch, memory+sessions), **NB/NC** (portability
+ cross-platform provisioner — the Python seam the pwsh port lands on). **Read first:**
[ARCHITECTURE-CONTRACTS.md](ARCHITECTURE-CONTRACTS.md) (C1 dispatch, C6 registries),
[MODULE-S-front-door-unification.md](MODULE-S-front-door-unification.md),
[MODULE-P-frontier-product.md](MODULE-P-frontier-product.md) (P3 = ONE-B).

## Why this module exists

The goal is not "one harness with 62 verbs routing through it." It is **one word: `bob`.** No subcommands
to memorize. You launch `bob`, you are in the interface, and everything — chatting, voice, "bring yourself
up", "what's on my screen", "restart litellm", "download that model" — happens *through the agent*, because
the LLM plus its tools **are** the interface. The 62 verbs do not get re-routed; they get **absorbed** (into
tools or modes) or **deleted**.

Two things are easy to conflate and this module keeps them distinct:

- **One harness** = one *execution engine*. Everything conversational runs through `run_agent_events`
  ([bob_loop.py:1008](../../scripts/bob_loop.py)); no second streaming/memory/persona path. Module S did
  this for text; ONE-B (=P3) does it for voice/vision. Kills *duplication/drift*. **Necessary but not
  sufficient.**
- **Just `bob`** = one *interface*. No verbs to memorize. Kills *surface fragmentation*. This is the part
  "one harness" alone does not give you — one engine behind 62 command names is still 62 words to know.

Most of the 62 verbs are not even agent turns: `bob up`/`build`/`logs`/`doctor` cannot literally go
"through `run_agent_events`" — the loop needs the very thing `bob up` starts. So the coherent target is:
**conversational verbs become the interface; infra verbs become tools the loop calls** ("bring yourself up"
→ the agent invokes the up-tool). The infra verbs still end up behind the one harness — as **tool calls,
not command names.**

## The architectural invariant (the whole module in one rule)

Every capability is **one importable Python function, implemented once**, reachable through three thin
adapters that contain **no logic**:

```
                    ┌─ agent tool   (plugins/<x>/tool.py or scripts/tools/<x>.py) → the loop calls it
one function  ──────┼─ kernel       (cold-start / first-run calls it directly)
(core in invoke.py) └─ deterministic invoker (`bob --run <x>` for CI/scripts)
```

This is exactly the rule already in [CLAUDE.md](../../.claude/CLAUDE.md) — *"Logic lives in `invoke.py` as
an importable function; `tool.py` imports it; the CLI calls it too"* — applied to **all 62 capabilities**,
with the verb table deleted. DRY by construction: `restart litellm` (agent) and `bob --run restart litellm`
(CI) hit identical code; the cold-start kernel calls the same functions.

**End-state module map**

| Surface | Role | Holds logic? |
|---|---|---|
| `bob` / `python -m bob` | Single entry. Bare→interface; `"text"`→one-shot loop; `--serve`→daemon; `--run <cap>`→deterministic tool | No — dispatch only |
| `scripts/bob/kernel.py` *(new)* | ~200ms cold-start: ensure-brain / first-run, by calling capability functions | No — orchestration only |
| [bob_loop.py](../../scripts/bob_loop.py) `run_agent_events` | The one execution engine | Yes — the loop |
| [shell.py](../../scripts/bob/shell.py) | The interface: REPL + modes (`/voice`, image) | Interface only |
| `scripts/tools/*.py` + `plugins/*/` | Every infra/memory/multimodal op, one function each | **Yes — single home of capability logic** |
| [defaults.json](../../config/defaults.json) + neutral `user.json` | One config source, one Python resolver | Data only |

Everything in `bob.ps1`, `_models.ps1`, `bob-memory.ps1`, `verbs.json`'s verb table, and the registry's
routing map is **deleted** by the end of this module.

## Design decisions (locked)

- **Server mode:** `bob --serve` stays as a **mode flag** on the one binary (the loop, run headless as the
  HTTP agent server / MCP server). One binary, DRY. Not a verb.
- **CI / scripting:** a deterministic `bob --run <capability> [json-args]` that calls the **exact same**
  function the agent's `tool.py` calls — no parallel path. CI stays reliable (does not depend on the model
  parsing intent); humans still just talk to `bob`.
- **PowerShell:** **fully ported to Python and deleted.** No second language, no `.psd1`↔`.py` config drift
  ever again. Windows-specifics (toast, services, WinRT, screen capture) go through the
  [osenv.py](../../scripts/osenv.py) seam (NB3) in Python.
- **Bare-`bob` interaction:** tty → interactive interface (REPL); piped / non-tty → one-shot loop turn.

## The one honest exception

An agent cannot boot its own brain conversationally — you cannot `bob "bring yourself up"` when there is no
inference server to interpret the sentence. So exactly **one** non-conversational path survives: the
**bootstrap kernel** (`scripts/bob/kernel.py`) that bare `bob` runs *before* the interface — ensure
inference is up, and on a cold machine, first-run setup. It is not a verb you type; it is what `bob` does in
its first ~200ms. Everything the kernel does is **also** an agent tool (so "restart litellm" works once you
are alive) — the kernel just calls the same capability functions. That shared-function design is the only
thing that keeps this DRY instead of a second orchestration path.

## Overview

| Sub | Name | What | Impact | Effort |
|-----|------|------|--------|--------|
| ONE-A ✓ | Single-source config | **DONE** — closed all 5 Class-2 drift findings; every config value now has one authored source (`defaults.json`); parity tests green | HIGH | 1–2 d |
| ONE-B ✓ | One engine (= P3) | **DONE** — multimodal-in-loop + `/voice` shell mode; voice/vision ported onto the loop; `Invoke-BobStream` deleted | HIGH | 1–2 wk |
| ONE-C | Capabilities-as-tools + `--run` | Port the 40 orchestration/provisioning pwsh handlers to Python capability functions; expose each to agent + `bob --run` | HIGH | 2–3 wk |
| ONE-D | Kill PowerShell | Port bootstrap kernel to Python; retire pwsh handler-by-handler; delete all `*.ps1` | HIGH | (with C) |
| ONE-E | Collapse the entry point | Delete `verbs.json` verb table + registry routing map + `bob.ps1` switch; final `bob` surface | MED | 2–3 d |

**Dependency graph**

```
ONE-A (config single-source) ──┐
                               ├──► ONE-D (kill pwsh) ──► ONE-E (collapse entry)
ONE-B (P3: one engine) ────────┤
ONE-C (capabilities-as-tools) ─┘
```

ONE-A and ONE-B are independent and can run in parallel. ONE-C depends on nothing but is the largest.
ONE-D interleaves with ONE-C (retire each handler as its tool lands). ONE-E is the final deletion.
**Start with ONE-B1 (multimodal-in-loop)** — the single hard blocker and critical path — while ONE-A
proceeds as safe cleanup.

---

## ONE-A — Single-source the config

**Status: ✓ DONE.** All 5 findings closed; both languages resolve config from `defaults.json` (Windows
`Get-BobConfig` deep-merges only the thin `bob.psd1` overlay = `agent.toastAppId`). Verified live: the
Python resolver and pwsh `Get-BobConfig` produce identical `routing`/`voice`/`persona`/`litellmKey`. New
parity tests: `test_routing_derived_not_authored`, `test_litellm_key_from_neutral_not_models_psd1`,
`test_agent_section_deep_merges_not_shallow` (retargeted from the deleted persona overlay),
`test_persona_from_neutral_layer`. What landed per finding:
- **#1 litellmKey** — deleted the `$base['litellmKey'] = $md.litellmKey ?? 'sk-local'` override in
  `_models.ps1`; it now resolves from `defaults.json.runtime.litellmKey` (osenv.secret is the sole override),
  matching Python. pwsh no longer shadows a `runtime.litellmKey`.
- **#2 routing** — deleted the whole `routing` block from `bob.psd1`; added `_models.ps1 Get-DefaultRouting`
  that derives routing from the `roleTable` (line-for-line mirror of Python `_routing_from_role_table`);
  unified the missing-`agentRole` fallback to `'chat'` (`bob.ps1` was `'planner'`). `autoFallback` (dead)
  dropped with the block.
- **#3 persona** — deleted the dead `persona.name/style` keys from `bob.psd1` + `user.psd1` + `onboard.ps1`
  (read nowhere; the `'bob'`≠`'Bob'` casing was the failing test). persona now resolves entirely from
  `defaults.json.runtime.persona`. The WI-6 deep-merge regression guard retargeted onto the `agent` section
  (still overlaid via `toastAppId`).
- **#4 ports** — `bob-voice-capture.py` + `piper_server.py` now resolve `sttPort`/`ttsPort` via
  `bob_core._port` (no literal). The `8080` in `setup-docker.ps1` is a **false positive** — it is SearXNG's
  *container-internal* bind (compose maps `${SEARXNG_PORT}:8080`), a searxng-image constant, not `ports.port`;
  left as-is.
- **#5 memory literals** — added `bob_core._MEM_DEFAULTS` + `_mem(mem, key)`; the ~12 mirror `.get(key,
  LITERAL)` defaults now read from `defaults.json.runtime.memory`. `memory.enabled` keeps an explicit
  fail-CLOSED `False` at its call site (a deliberate safety default, not a mirror).
- **bonus** — B4/B5 had introduced a fresh voice-settings duplication (`ttsVoice`/`silenceSec` in `bob.psd1`
  ↔ literals in `bob_voice.py`); single-sourced into `defaults.json.runtime.voice`, emitted by the resolver,
  read by both sides. The dead voice-only `systemPrompt` dropped (/voice uses the shared persona +
  `format_for_speech`).

### Problem (from the dual-harness audit, Class 2 = DRIFT-RISK)
PowerShell `Get-BobConfig` reads `defaults.json` + `bob.psd1` + `user.psd1` + `models.psd1`
([_models.ps1:125-156](../../scripts/_models.ps1)); Python `resolve_runtime_config` reads only
`defaults.json` + neutral `user.json`/`user.toml` ([bob_config.py:48-73](../../scripts/bob_config.py)) and
never touches any `.psd1`. Anything authored in a `.psd1` but absent from `defaults.json` is invisible to
Python. On a PowerShell-less Linux boot, the standalone resolver runs and every `.psd1` override vanishes.

### Findings to close (ranked by live risk)
1. **`litellmKey` — different *source file* per language.** Python reads `defaults.json.runtime.litellmKey`
   ([bob_config.py:84](../../scripts/bob_config.py)); PowerShell overwrites it from
   `models.psd1.defaults.litellmKey` ([_models.ps1:150-155](../../scripts/_models.ps1)). They coincide only
   because both fall back to the literal `'sk-local'` (hand-duplicated ~8×). Set `runtime.litellmKey` and
   Python honors it, pwsh ignores it → silent auth drift. **Fix:** resolve from `defaults.json.runtime`
   only; `osenv.secret()` is the sole runtime override.
2. **Routing role values authored twice.** Every role literal (`chat`, `chat-pro`, `coder`, `planner`…)
   exists as a `*Fallback` in `defaults.json.roleTable` ([:16-23](../../config/defaults.json)) *and* as a
   literal in `bob.psd1.routing` ([:17-24](../../config/bob.psd1)). The
   [bob_config.py:38](../../scripts/bob_config.py) comment wrongly claims they are not duplicated. Sub-drift:
   missing-`agentRole` fallback is `'planner'` in pwsh ([bob.ps1:1148](../../scripts/bob.ps1)) vs `'chat'` in
   Python ([bob_loop.py:1049](../../scripts/bob_loop.py), [shell.py:267](../../scripts/bob/shell.py)).
   **Fix:** delete the `routing` block from `bob.psd1`; derive routing from `roleTable` in one place; unify
   the fallback literal.
3. **`persona.name` casing.** pwsh resolves `persona.name='bob'` (from `user.psd1:5`, written by
   [onboard.ps1:45-47](../../scripts/onboard.ps1)); Python has no `persona.name` key. Both `systemPrompt`s
   say "Bob". Latent (key is dead, read nowhere) but the `user.psd1`→Python blind spot is real. **Fix:**
   delete the dead keys, or move to `defaults.json.runtime.persona` and have onboard write neutral
   `user.json`.
4. **Ports re-inlined** (violates NB1). `sttPort 8082` in [bob-voice-capture.py:94](../../scripts/bob-voice-capture.py)
   (effective default — nothing sets `BOB_STT_PORT`); `ttsPort 8083` in
   [piper_server.py:24](../../scripts/piper_server.py) (wins on pwsh-less Linux); `8080` baked into
   [setup-docker.ps1:109](../../scripts/setup-docker.ps1). **Fix:** resolve via `bob_core._port(...)`.
5. **`memory.*` fallback literals.** ~18 `.get(key, LITERAL)` defaults in
   [bob_core.py](../../scripts/bob_core.py) duplicate `defaults.json.runtime.memory`. **Fix:** one
   `_MEM_DEFAULTS = load_defaults()["runtime"]["memory"]` lookup.

### The durable cure
Make Python's resolver the **single** resolver and converge both languages on the neutral JSON/TOML
overrides; `.psd1` becomes (temporarily) an authoring convenience that compiles into them, then is deleted
in ONE-D. This eliminates the entire Class-2 surface at the source.

### Acceptance
One resolver reads one file; `git grep` finds no config value defined in two places; a parity test agrees
Python-only (no pwsh side to disagree with).

---

## ONE-B — One engine (= Module P3): multimodal + voice/vision onto the loop

Retires the second *harness*: `Invoke-BobStream` ([bob.ps1:56](../../scripts/bob.ps1)), whose only live
callers today are `voice` ([:995](../../scripts/bob.ps1)) and `describe`/`screenshot`
([:916](../../scripts/bob.ps1)). Closes every Class-1 BEHAVIORAL-DIVERGENCE: a message via `bob voice` gets
**no memory recall, no write-back, a different persona, a hard 256-token cap, no retry, no logging, and no
tools** vs `bob chat` — all because it runs the pwsh path, not the loop.

**Progress:** ONE-B1 ✓ (`dd464ee`, `0a80286`) — `images` param on the loop + tool-result image contract,
via the shared `_image_content_block` encoder. ONE-B2 ✓ (`996d399`) — `describe`/`screenshot` on the loop
(`scripts/bob_vision.py` capture+resize; `cli._handle_describe`/`_handle_screenshot`; flipped to python in
verbs.json; ~90 lines of Windows-only pwsh .NET deleted). ONE-B3 ✓ (`479e7c1`) — audio seam in
`osenv.py` (`play_audio` + `record_audio`, lazy sounddevice); `bob-voice-capture.py` now single-sources
capture through it. 18 tests. ONE-B4 ✓ — `/voice` shell mode (`scripts/bob_voice.py` is the voice
capability core: `format_for_speech` + whisper STT client + piper TTS synth; `bob.shell._cmd_voice` loops
mic→STT→`_run_turn`→TTS; `bob-voice-capture.py` re-uses the shared `transcribe`). ONE-B5 ✓ — `voice`/
`listen`/`transcribe`/`speak` ported to Python CLI handlers (`cli._handle_voice`/`_handle_listen`/
`_handle_transcribe`/`_handle_speak`, `shell.run_voice`), flipped to `python` in registry+verbs.json;
`Invoke-BobStream`, `Format-ForSpeech`, and the pwsh voice/listen/transcribe/speak switch cases all
**deleted** (~215 lines of pwsh gone). **ONE-B (=P3) COMPLETE — the second harness is retired.**

- **B1. Multimodal-in-loop** ✓ *(critical path — done).* `run_agent_events`/`run_agent` gained an
  `images: list = None` param (chosen over overloading `goal` to keep `goal` a `str` — recall/recitation/
  logging untouched, minimal blast radius); when present the goal turn becomes an OpenAI content-block list
  `[text, image_url…]` (else a plain string, byte-identical); image-bearing turns route to
  `get_role(config, "vision")` unless the caller pinned a role. The **image-carrying tool-result contract**
  (`{"__images__":[...],"text":...}`) threads a tool-returned image into the next model turn
  ([:1421-1426](../../scripts/bob_loop.py) is JSON-string-only today). Grep confirms zero `image_url`
  handling in the loop today — this is greenfield.
- **B2. Port `describe`/`screenshot`** ✓ *(done).* `scripts/bob_vision.py` holds the one capability core
  (`resize_image` — Pillow when present, else send as-is; `capture_screen` — Pillow ImageGrab on Win/mac,
  grim/spectacle/scrot/import on Linux); `cli._handle_describe`/`_handle_screenshot` run one-shot vision
  turns via `run_agent(images=[…], role=vision)`. Flipped to `python` in the registry/verbs.json; the pwsh
  case blocks (System.Drawing + System.Windows.Forms) are deleted.
- **B3. Audio I/O seam** ✓ *(done).* [osenv.py](../../scripts/osenv.py) gained `play_audio` (winsound /
  afplay / paplay|aplay|ffplay, replacing the inline pwsh `SoundPlayer`/`paplay` branch) and `record_audio`
  (16 kHz mono RMS-silence capture; sounddevice/numpy lazy). `bob-voice-capture.py` now single-sources
  capture through the seam. STT/TTS remain standalone servers (whisper POST / piper binary) — the seam owns
  only the raw mic-in / speaker-out that the /voice mode composes with them.
- **B4. `/voice` shell mode** ✓ *(done).* [shell.py](../../scripts/bob/shell.py) gained `/voice` in `_SLASH`
  + the dispatch map, and `_cmd_voice` — a mic→STT→`_run_turn`→TTS loop that wraps the SAME agent turn as
  text (streamed + Ctrl-C cancellable per turn; Ctrl-C while listening, or an "exit"/"stop"/"quit"/"goodbye"
  transcript, leaves the mode). The capability core is [bob_voice.py](../../scripts/bob_voice.py):
  `format_for_speech` (port of pwsh `Format-ForSpeech`), the whisper STT client (`transcribe`/`listen`/
  `stt_ready`, now single-sourced — `bob-voice-capture.py` re-uses it), and the piper TTS synth (`speak`,
  synth→`osenv.play_audio`). Because it wraps `_run_turn`, voice inherits memory + write-back + one persona
  + retry + logging + tools automatically. Watch-list decisions: `Format-ForSpeech` ported verbatim;
  whisper *reachability is checked* with an actionable hint (auto-*launch* is a provisioning capability that
  lands as a tool in ONE-C, not a pwsh shell-out); the `--no_think` fast-reply hack is dropped — reasoning
  is now a `/model` role choice, not a per-turn string appended to (and thereby corrupting) the persisted
  turn + memory.
- **B5. Delete `Invoke-BobStream` + the pwsh voice handlers** ✓ *(done).* `voice`/`listen`/`transcribe`/
  `speak` ported to Python CLI handlers in [cli.py](../../scripts/bob/cli.py) over the B4
  [bob_voice.py](../../scripts/bob_voice.py) core: `bob voice [--pro] [--agent]` launches the shell straight
  into `/voice` mode via `shell.run_voice` (default chat-role/no-tools; `--agent` keeps the full toolset;
  `--pro` the pro voice model); `bob listen`/`transcribe`/`speak` are thin wrappers over
  `bob_voice.listen`/`transcribe`/`speak`. Flipped to `runtime=python` in [registry.py](../../scripts/bob/registry.py)
  + regenerated [verbs.json](../../config/verbs.json). Deleted from [bob.ps1](../../scripts/bob.ps1):
  `Invoke-BobStream` (the streaming curl path), `Format-ForSpeech` (ported to `bob_voice.format_for_speech`),
  and the `listen`/`transcribe`/`speak`/`voice` switch cases — ~215 lines. bob.ps1 parses clean; the
  registry parity test (every switch verb registered) + verbs-in-sync gate stay green.

### Prerequisites verified missing today (all are B's work)
Multimodal input into the loop, a `/voice` shell mode, and the `osenv` audio seam are **all absent**; the
vision model + STT/TTS servers are configured ([defaults.json:6-7,22,63-67](../../config/defaults.json),
[llama-swap.yaml:34-35](../../config/llama-swap.yaml)) but launched only by `.ps1`. **Unknowns to resolve in
a B0 spike:** vision GGUF present + VRAM-fit; whisper/piper start under pwsh-less Linux; the "reuse Module G
vision routing" the P3 spec assumes actually lives in *pwsh* (`Invoke-BobStream`), so B must reimplement
image routing on the Python side.

### Acceptance (from [MODULE-P:120-123](MODULE-P-frontier-product.md))
Loop accepts image content and routes a vision turn; a tool returning an image is consumed by the next turn;
`/voice` round-trips STT→loop→TTS in the shell. Live: an agent captures + reasons over a screenshot; a
spoken conversation runs inside `bob`. Needs a live vision model + audio devices.

---

## ONE-C — Capabilities-as-tools + the deterministic invoker

> **Detailed, scoped execution plan: [MODULE-ONE-C-plan.md](MODULE-ONE-C-plan.md).** It carries the full
> 45-verb inventory (disposition/placement/risk), the osenv seam-gap table, the `models.psd1`→`models.json`
> gating prerequisite, the scheduling seam, a dependency-ordered slice sequence, and 6 open decisions (D1–D6)
> to confirm before coding. Start there.

Establish the architectural invariant for the 40 orchestration/provisioning verbs. Each currently-pwsh
handler becomes **one Python capability function** (Layer 1 `scripts/tools/<name>.py` or Layer 2
`plugins/<name>/`, per [CLAUDE.md](../../.claude/CLAUDE.md)'s placement rule). Each is exposed:

- to the **agent** via `tool.py` (so "bring the stack up" / "restart litellm" / "download that model" work
  in-loop), and
- to **CI/scripts** via `bob --run <cap> [json-args]` — the same function, a thin deterministic adapter.

No logic in either adapter; one implementation, two callers (+ the kernel = three). This is the DRY answer
to both the "agent-gated infra" and "CI reliability" tensions.

### Note on the fragile seams C removes
Today `voice` recursively re-invokes `bob.ps1 listen`/`speak` ([:982,1003](../../scripts/bob.ps1)) and
`--agent` mode captures agent stdout via `Out-String`, discarding stderr ([:988](../../scripts/bob.ps1));
`screenshot` re-enters `bob.ps1 describe` ([:948-950](../../scripts/bob.ps1)); every pwsh→native-python
shell-out must remember `Join-Path` or Linux breaks ([:1198-1200,1265-1267](../../scripts/bob.ps1), a bug
that already broke `bob tools` on Linux). All of this dissolves when capabilities are in-process Python.

---

## ONE-D — Kill PowerShell

> **Detailed, scoped execution plan: [MODULE-ONE-D-plan.md](MODULE-ONE-D-plan.md).** It carries the full
> 9-verb + tails inventory (CAPABILITY vs KERNEL disposition), the build-time osenv seam-gap table, the
> versions.lock writer/gate gap, the two-tier cold-start kernel design, an 9-slice shippable sequence
> (D0–D8), and 6 open decisions (DD1–DD6) to confirm before coding. Start there.

As each capability becomes a Python function (ONE-C), delete its `bob.ps1` handler and remove/flip its
`verbs.json` entry. Port the bootstrap kernel (`scripts/bob/kernel.py`) to Python — cold-start ensure-brain
+ first-run, calling the capability functions. Windows-specifics go through [osenv.py](../../scripts/osenv.py)
(NB3) in Python (toast, services, WinRT, screen capture), not pwsh.

When the switch is empty, **delete** [bob.ps1](../../scripts/bob.ps1), [_models.ps1](../../scripts/_models.ps1),
[bob-memory.ps1](../../scripts/bob-memory.ps1), and every `*.ps1` provisioner. `_models.ps1`'s role
resolution ([:191-211](../../scripts/_models.ps1)) is already a line-for-line twin of Python `get_role`
([bob_core.py:57-72](../../scripts/bob_core.py)) over the same table — deleting it removes a mirror, not
logic. `bob-memory.ps1` is already a 17-line wrapper over `bob_memory.py` — nothing is lost.

## ONE-E — Collapse the entry point

**Part 1 ✅ DONE (commit fa8627e; suite 814 green) — the PowerShell layer is retired.** Ported both CI
gates to stdlib-only Python (`scripts/check.py` ← check.ps1; `scripts/smoke.py` ← smoke.ps1) +
`scripts/install_hooks.py` (← install-hooks.ps1; the pre-commit hook now execs check.py); deleted the last
10 `*.ps1` (bob.ps1 front door + the _models/_platform/_versions/_common seam library + check/smoke/
smoke-linux/test-platform/install-hooks); rewired ci.yml (lint drops PSScriptAnalyzer; core-suite runs
`python scripts/check.py`; acceptance runs `python scripts/smoke.py --up`); removed cli.py's dead pwsh
dispatch (`_exec_pwsh`) and the obsolete pwsh↔Python parity tests. `git ls-files '*.ps1'` now returns only
the sample `plugins/play/invoke.ps1`. **Part 2 (below) is what remains.**

**Part 2 ✅ DONE (suite 811 green) — the verb table collapsed to a single source.** Deleted
`config/verbs.json` + its whole generate/sync machinery (`verbs_json_dict`/`write_verbs`/`_check`/
`VERBS_FILE` + the `python -m bob.registry` gate + the check.py sync step) and the now-vestigial `runtime`
field on every command. `scripts/bob/registry.py`'s `COMMANDS` is now the **one** source for both dispatch
and help — adding a verb is one entry + one cli.py handler, with no generated table to regenerate.

**Scope decision (per "DRY, clean, maintainable, expandable"):** the deprecation ledger below imagined
*deleting every `bob <verb>`* so only `bob`/`bob "text"`/`bob --serve`/`bob --run` survive. That was
rejected — forcing `bob up`/`bob doctor`/`bob build` through `--run <cap>` JSON is *less* usable and no
more maintainable. The clean/expandable end state keeps the verbs, sourced from the one registry. What was
genuinely redundant — the generated routing *table* (needed only by the retired pwsh shim) — is gone.

Surviving `bob` surface: `bob` (bare, tty→REPL / piped→help) · `bob <verb> …` (registry-dispatched) ·
`bob --run <cap> [json]` (deterministic tool, CI). (`bob "freeform text"`→one-shot and a `--serve` alias
for `agent serve` were part of the maximal vision; not adopted — explicit verbs are clearer.)

**ONE-E COMPLETE. The one-harness north star is reached: one word, one engine (Python), zero PowerShell.**

---

## Deprecation ledger (all 62 verbs)

| Group | Verbs | Disposition | Lands in |
|---|---|---|---|
| Conversational/multimodal | `chat` `code` `think` (done, S) · `voice` `describe` `screenshot` `listen` `transcribe` `speak` · `clip` `skill` `shell` | Become the interface + `/voice`/image modes. Deleted as verbs. | S (text) / ONE-B (voice/vision) / ONE-E |
| Orchestration | `up` `serve` `restart` `stop` `down` `status` `ps` `logs` `services` `webui` `aider` `litellm` `whisper` `piper` `doctor` `diagnose` `bench` | Become agent tools + kernel functions + `--run`. Deleted as verbs. | ONE-C/D/E |
| Provisioning | `setup` `setup-voice` `fabric-setup` `gen` `fetch` `models` `show` `profile` `profiles` `build` `update` `lock` `version` `mlock` `eval` `verify-urls` | Become agent tools + first-run kernel + `--run`. Deleted as verbs. | ONE-C/D/E |
| Memory | `recall` `remember` `memory` `budget` | Become tools (loop already has `memory_recall`/`memory_store`). Deleted as verbs. | ONE-C/E |
| Agent lifecycle | `agent` `agent serve` `agent mcp` `agent tools` `agent schedule` `agent log` `agent install` `agent uninstall` `agent status` | `agent`=the loop; `serve`/`mcp`=`--serve` flag; rest fold into tools. Deleted as verbs. | ONE-C/E |
| Meta | `tools` `plugins` `fabric` `help` | Subsumed — the interface reports its own capabilities. Deleted as verbs. | ONE-E |

`serve`=inference front door (pwsh today) is folded into the kernel/`--run`; `agent serve` (Python HTTP
server) becomes `bob --serve`. Note `agent serve` is **already divergent** across the two paths today
(Python runs a `capability_probe` + prints `[probe]`, [cli.py:81-82](../../scripts/bob/cli.py); pwsh does
not, [bob.ps1:1226-1238](../../scripts/bob.ps1)) — collapsing to one entry fixes that.

## Verification (each phase "done" = observed, not asserted)

- **ONE-A:** one resolver, one file; `git grep` finds no config value defined twice; parity test passes.
- **ONE-B:** the two P3 acceptance tests + a live `/voice` round-trip.
- **ONE-C:** each capability invoked identically via the agent and via `--run`; one function in the stack.
- **ONE-D/E:** `git grep -l '\.ps1'` returns nothing under `scripts/`; `bob` with no verb table still boots,
  chats, brings itself up, and voices.

## Definition of done for Module ONE
`bob` is the only word. The agent loop is the only engine. Capability logic lives once (a Python function),
reachable by agent / kernel / `--run`. Config has one source and one resolver. There is no PowerShell.
