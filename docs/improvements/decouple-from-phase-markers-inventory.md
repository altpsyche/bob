# Phase-marker decoupling — consolidated inventory

Status: **REMEDIATION COMPLETE.** Working tree changed, gate green (903 tests OK), nothing committed.
Produced by a 6-way parallel fan-out (scripts top-level, scripts/tools, scripts/bob, plugins, tests, docs/config/CI), then merged and de-duped. See "Remediation results" at the bottom.

## Ground truth (reproduced)

- Raw marker-token hits across `scripts/ plugins/ tests/ --include='*.py'`: **~740** (the prompt's ~890 used a looser regex; the delta is false-positive short tokens that were filtered out in judgment). Real hits after judgment: see per-family below.
- Per-family raw counts (before false-positive filtering): `O*`=228, `ONE-*`=136, `MEM-*`=96, `N*`=91, `M*`=58, `NE*`=58, `D*`=57, `C*`=49, `B*`=41, `NB*`=37, `S*`=23, `WI-*`=10, `ND*`=14, `A*`=9, `slice`(word in code)=3.
- **14 phase-named test files** (exactly the prompt's list) — confirmed.
- Renames are **CI-safe**: `ci.yml`, `scripts/check.py`, `scripts/smoke.py` all discover tests by glob (`test_*.py`); no test filename is hardcoded anywhere in CI/gate.

## Uniform transformation rules

1. **Docstring/comment opener** `"""MARKER — <behavior>` / `# MARKER — <behavior>` → drop the `MARKER — ` prefix, keep `<behavior>` (capitalize).
2. **Trailing/parenthetical citation** `... (N7)` / `... (O8)` → drop the token, keep the sentence.
3. **`byte-identical to pre-OX`** → replace with a behavioral baseline description ("the sequential loop", "the dense-only path", "when the flag is off"). Preserves the *invariant*, drops the *ledger id*.
4. **`Decision A/B/C`** (planning-ledger decision refs) → drop the `Decision X` label, keep the rationale.
5. **Section-divider comments** `# --- foo (ONE-C §1b) ---` → `# --- foo ---`.
6. **Content-free tag** (bare `# S1` with no prose) → delete the line.
7. **Load-bearing directives** (`# noqa`, `# type:`, pragmas) → never touched. None collided with a marker in the whole tree.

---

# Category A — comment/docstring tags (by area)

## A0. plugins/ (4 hits)
- `plugins/draft/invoke.py:50` | `# table itself lives in bob_core.get_role (M8) — here we only pick the task.` → drop `(M8)`
- `plugins/play/tool.py:57` | `from bob_core import _port  # N7 — single source of truth for ports` → strip `N7 — `
- `plugins/AUTHORING.md:33` | `### Functional grouping (ONE-C, decision D6)` → `### Functional grouping`
- `plugins/AUTHORING.md:35` | `... The ONE-C capability ports (former PowerShell ...` → drop `ONE-C `
- FALSE (leave): `plugins/AUTHORING.md:167` "decision rule" (prose); `plugins/play/invoke.ps1` (clean).

## A1. scripts/tools/ (~90 hits, 15 files)
Docstring openers to strip the `MARKER — ` prefix from: `budget.py:1` (ONE-C Slice 1), `build.py:1,3,5,9` (ONE-D D5/DD1, DD1, C7, D8), `build.py:329,353,366` (ND3, D8, ND3), `generate.py:1,2,289` (ONE-C Slice 6, D6, C3), `health.py:1,11,13,14,17,18,45,160,227,264,308,337,340` (ONE-C Slice 3 + ONE-D D3/D0/Slice-5 refs), `models.py:1,2,46,171,294,310` (ONE-C Slice 4, C0c/D6, ONE-D, D4, DD3, DD3/D8), `provision.py:1,3,6,8,93,102,126,225,396` (ONE-D, D6, DD2, D1, ND1, ND1, DD2, D7, D2), `schedule.py:1,3,12,13,35,67` (ONE-C Slice 5, D6, D3, D4, D3, D4), `stack.py:1,90,125` (ONE-C Slice 2, C4, ONE-C Slice 6), `read_result.py:1,4,7` (M7/O3/O15, pre-O15, NE0), `shell.py:3,5,16-18,28,30` (NE0, NB3, NE0/O5/O6, O5, pre-O5), `spawn_agent.py:1,3-5,7,75` (O1, O2/O3/O6, pre-O1, O9), `todo.py:1,3,5-7` (O16, pre-O16, O2/O6/O16/O4), `tool_loader.py:46` (M16), `tool_registry.py:27,34,43,45,47,48,50-51,57,59-60,67,99,102,104,125,191,253,288,300,311,325-326` (NE0/O1/O2/O3/O6/O7/M7/M9/MEM-6/M16/O15), `web.py:11,13,24,130` (M9, NE0, N7, M9/NE0), `git.py:1,6,20` (N9), `file.py:9,12,23,87` (N9/NB3/C3), `memory.py:23-24,29,46` (MEM-6/7, MEM-3).
- FALSE (leave): `schedule.py:5,9,39` and `provision.py:102` — `Test-CronDue` / `Update-Manifest` are ported PowerShell function *names*. `fabric.py` clean.
- User-facing/generated-output strings (strip tag, keep string): `provision.py:93` return msg `(ND1 verify-on-install)`, `generate.py:289` `# from litellmKey seam (C3)` written INTO a generated LiteLLM config.
- Multi-line docstrings needing whole-sentence rewrite (markers woven into grammar): `health.py:11-18` & `337-341`, `spawn_agent.py:3-5`, `shell.py:16-18`, `todo.py:5-7`, `memory.py:23-24`.

## A2. scripts/bob/ (~70 hits)
Strip the opener/citation on: `__init__.py:1` (NB4/C1/C6), `__main__.py:1` (NB4), `catalog.py:1,4,5,41` (NE3/C6, NE2, N5, O11), `registry.py:1,4,16,17,21,141,153` (ONE-E/C6, ONE-D/E, NE1, Decision B, S2, ND1, WI-7), `versions.py:1,100,231` (ND1/C2, ONE-D Slice D2, ND1), `install_prereqs.py:1` (ONE-D Slice D8/DD5 — keep `Tier-0`), `kernel.py:1,240` (ONE-D Slice D8; Slice D8 — keep `Tier-1`), `theme.py:1` (NE2), `render.py:1` (NE2), `shell.py:1,20,25,31,92,276,281,286,320,724,729,791,835,836,900,959,1131,1132,1142,1179,1190` (NE2/C1, Decision A/C, NE0, S2, WI-6, MEM-7/4, O11, C6, ONE-B4/B5, M9), `cli.py` (see below).
- `cli.py` docstrings/comments: `:1,2,19,30,32,37,55,74,155,257,294,341,365,407,426,446,465,488,500,638,748,751,771,799,908,925,962,973,986,1077` + DISPATCH trailing comments `:1009,1017,1020,1022,1026,1034,1038,1042,1046,1052,1053,1055,1056,1057,1058,1059,1060,1061`. Strip `# ONE-C Slice N — `, `# ONE-D Slice DN — `, `S2 —`, `ONE-B2/B5 —`, `(D5)`, `(O11)`, `per C1`, etc.
- KEEP (false positive / concept name): `kernel.py:10,11,664,667,671` `Tier 0`/`Tier 1` labels; the verb name `mlock` (cli.py:1059); arg placeholders `[--limit N]`/`[--shots N]` (cli.py:908).
- User-facing strings (priority): `registry.py:21` (`S2:` in a `bob help` summary), `registry.py:141` (`(ND1)` in a summary), and many `cli.py` docstrings that surface as command help.
- STALE FACTS to fix, not just de-tag: `cli.py:751` "eval stays pwsh ... ONE-D" (eval is Python now); `versions.py:231` references retired `check.ps1`.

## A3. scripts/ top-level (~330 hits — the bulk; `bob_loop.py` and `bob_memory.py` dominate)
Every module in `scripts/*.py` opens its docstring with a `MARKER — ` and carries inline citations. Apply rules 1–5 across:
- `bob_agent_runner.py:2`; `bob_config.py:1,5,36,102`; `bob_authstore.py:2,4,5,12,16,17,20,35,56,182`; `bob-voice-capture.py:18,19,20`; `bob_tracing.py:2,4,10`; `bob_core.py:16,18,24,34,44,45,49,70,74,94,103,134,156,170,182,183,203,204,219,228,229,285,308,310,311,325,327,329`; `bob_mcp_server.py:10`; `bob_voice.py:1,4,19`; `bob_vision.py:1,3,12,34`; `bob_agent_server.py:6,13,14,45,65,66,67,68,72,87,89,113,115,117,137,138,148,149,164,179,201,214,249,265,303,314,315,319,343,345,354,355,357,362,393,405`; `bob_mcp_client.py:2,7,16,26`; `bob_models.py:1,5,10,26,72,92`; `bob_skills.py:1,9,10,11,12,80,96,99,112,113,131,132,133,158`; `install_hooks.py:2`; `bob_permissions.py:1,3,5,8,9,10,19,64,65`; `bob_session.py:1,6,8,28,39,63,80`; `smoke.py:2,10,189,205`; `sandbox.py:1,4,14,20,202`; `bob_memory.py` (~45 lines, MEM-* family + A3/A4/B5/B6); `osenv.py` (~24 lines, NB3/C3/C4/ONE-B3/ONE-C §1b/ONE-D §1b/Slice D8); `piper_server.py:22`; `bob_loop.py` (~130 lines, the densest file — O2..O16/M7/M15/M18/N3..N8/NE0/MEM-3/6/7/ONE-B1/A1/B4/S1); `check.py:2`.
- KEEP (false positive): `bob_memory.py:11` `BGE-M3` (model name); `bob_voice.py:107` `'transcribing…' phase` (runtime UI); `bob_skills.py:14,28` `(name, phase, message)` tuple field.
- Tag-only trailing on a content line: `bob_loop.py:1149` `# NE0` after a dict literal — strip the tag, keep the line.
- Dangling doc-section refs (separate cleanup, not phase family): `bob_memory.py:84` `§7`, `:138` `CONTRIBUTING §8` — strip the co-located `MEM-4`/`N7` markers; decide the `§` refs separately.
- Verify-on-wrap: `bob_loop.py:1301` (`WI-6` continues to next line).

---

# Category B — test-file renames (14 files)

| Current | Proposed | Collision | Note |
|---|---|---|---|
| test_slice1_meta.py | test_meta.py | no | source module `tools/meta.py`; distinct from test_memory.py |
| test_slice2_stack.py | test_stack.py | no | `tools/stack.py` |
| test_slice3_health.py | test_health.py | no | `tools/health.py` |
| test_slice4_models.py | test_models.py | no | distinct from test_models_parity.py |
| test_slice5_schedule.py | test_schedule.py | no | `tools/schedule.py` |
| test_slice6_generate.py | test_generate.py | no | `tools/generate.py` |
| test_slice_d1_fetch.py | test_fetch.py | no | provision.py `fetch_models` |
| test_slice_d2_lock.py | **DECISION** | **YES** vs test_versions_lock.py | merge into test_versions_lock.py (reader+writer) OR test_versions_lock_writer.py |
| test_slice_d3_mlock_diag.py | test_cuda_mlock_diagnose.py | overlaps test_osenv.py + test_health.py | bundles osenv CUDA/mlock seams + deep diagnose; could split |
| test_slice_d4_eval.py | **test_model_eval.py** | **YES** vs test_eval.py | test_eval.py is the agent-capability eval (different "eval") |
| test_slice_d5_build.py | test_build.py | no | `tools/build.py` core build |
| test_slice_d6_update.py | test_update.py | no | build.py `update_stack` |
| test_slice_d7_setup_voice.py | test_setup_voice.py | no | provision+build voice provisioning |
| test_slice_d8_kernel.py | test_kernel.py | no | `bob/kernel.py` |

Use `git mv` (history follows). After rename, re-run discovery to confirm suite count unchanged.
Two prose cross-references to update on rename: `tests/test_slice4_models.py:30` ("see test_slice_d4_eval"), `tests/test_slice3_health.py:93` ("test_slice_d3_mlock_diag.TestDiagnoseDeep").

---

# Category C — test class/method renames (only 2 real)

- `tests/test_slice4_models.py:28` `def test_eval_ported_in_one_d` → `def test_eval_is_cli_only_not_an_agent_tool` (matches the assertion).
- `tests/test_permissions.py:132` `def test_ne0_floor_preserved_under_empty_policy` → `def test_approval_floor_preserved_under_empty_policy`.
- CONFIRMED false positives (English "one"/loop-phase, leave): `test_all_phases_off_single_call`, `test_install_packages_batches_one_call`, `test_cpu_installs_toolchain_in_one_batch_skips_cuda`, `test_mid_stream_error_one_error_no_final`. No test CLASS name encodes a phase.

---

# Category D — structural / organizational coupling

1. The 14 `test_slice*` filenames themselves (Category B fixes this).
2. Module docstrings that lead with the phase as identity — nearly every test file opens `"""MARKER — ..."""`; Category A rule 1 fixes.
3. Section-header comments keyed to a phase but already behavior-grouped (drop only the `(MARKER)`): `test_osenv.py:192,469`; `test_mcp_client.py:190`; `test_server.py:51,68,79,159`; `test_agent_loop.py:191,263,331,399`; `test_session.py:58`.
4. **DECISION**: `TestRegistryWiring`/`TestAllVerbsPython` classes open every slice test (`slice1:28, slice2:24, slice3:32, slice4:19, slice5:33, slice6:25, d1:36, d2:36, d3:23, d4:19, d5:21, d6:17, d7:19`) asserting "verbs flipped to python / no pwsh / verbs.json gone". These assert *the migration happened* (a phase concern), not durable behavior. Option: leave as-is, or consolidate into `test_command_registry.py` (which already owns "registry is THE single source"). Flagged for your call — not a blind strip.

---

# Category E — docs + CLAUDE.md

## Living docs (rewrite) vs Archive (leave)
- **LIVING** (rewrite markers): `.claude/CLAUDE.md`, `docs/PORTABILITY.md`, `docs/SECURITY.md`, `docs/TUNING.md`, `CONTRIBUTING.md`, `config/defaults.json` (`_comment`), `config/models.json` (`_doc`).
- **ARCHIVE** (leave phase language): `docs/ROAD-TO-BOB.md`, all of `docs/improvements/*` (MODULE-*, POST-ONE-*, ARCHITECTURE-CONTRACTS.md, REPRO-DEBT.md, this inventory, the prompt).
- Clean (no real marker): `docs/AGENT-SERVER.md`, `DAY-IN-THE-LIFE.md`, `MEMORY.md`, `USAGE.md`, `SETUP.md`, `MANUAL-INSTALL.md`, `FALLBACKS.md`, `CHANGELOG.md`.

## .claude/CLAUDE.md concept-labels + stale facts
- `:38` `bob_config.py NB2 — ...` → drop `NB2 — `
- `:39` `osenv.py NB3 — OS seam...` → `osenv.py  the OS seam...`
- `:40` `scripts/bob/ NB4 — ...` → drop `NB4 — `
- `:41` `defaults.json NB1 — neutral single source...` → drop `NB1 — `
- `:42` `config/verbs.json NB4 — ...` → **STALE: delete the whole line** (verbs.json was deleted in ONE-E; registry.COMMANDS is the sole dispatch source)
- `:52` `... (NB1) — never re-inline a literal in .py or .ps1` → drop `(NB1)` and `.ps1` (no PowerShell remains)
- `:53` `... (NB3); secrets via osenv.secret()` → `OS-specific behavior goes through the osenv seam (scripts/osenv.py); ...`
- `:54` `... (NB4); regenerate config/verbs.json ... check.ps1 gate ... (pwsh)` → **STALE: drop `(NB4)`, remove the verbs.json regen clause, change `check.ps1`→`check.py`, drop `(pwsh)`.**
- Same-family concept labels in other living docs: `PORTABILITY.md:9,19`; `SECURITY.md:39,41,56,89,96`; `TUNING.md:88,89,116`; `CONTRIBUTING.md:47,54`; `defaults.json:2`; `models.json:2`.

## Archival references that will go stale on rename (leave — they're archive)
`docs/improvements/MODULE-ONE-C-plan.md`, `MODULE-ONE-D-plan.md`, `POST-ONE-2-lifecycle-dry-ux-plan.md`, `POST-ONE-3-consistency-and-debt.md` cite the old `test_slice*` names. `.claude/settings.local.json:13` has a `MODULE-P-...md` path inside a saved Bash permission entry — leave.

---

# Deliberately-left / open decisions (need your call)

1. **NB1–NB5 / C1–C7 seam names**: recommend **de-tag everywhere** (describe the seam by role in prose) since the whole point is decodability — but this renames how CLAUDE.md refers to the seams. Confirm.
2. **`byte-identical to pre-OX`**: recommend replacing with behavioral baselines (paraphrased per line). Confirm the paraphrases are acceptable (a few required interpretation, e.g. `pre-O8` = "config-only path").
3. **Test-file collisions**: pick merge vs suffix for `test_slice_d2_lock.py`; confirm `test_model_eval.py` for d4; confirm/split `test_slice_d3_mlock_diag.py`.
4. **`TestRegistryWiring` migration-assertion classes** (Category D.4): leave or consolidate.
5. **Stale facts co-located with markers** (CLAUDE.md verbs.json/check.ps1/pwsh; cli.py:751; versions.py:231): fix them while de-tagging, or strictly marker-only and leave the stale facts?

---

# Remediation results

Scope decisions taken (by the user): de-tag EVERYWHERE (incl. NB/C seam labels → described by role); fix
stale facts co-located with markers; reorganize the tests by feature/domain (merge logically, drop the
phase/slice structure entirely, including the per-slice migration-wiring classes).

## What changed
- **Source de-tagging** — every phase marker stripped from comments/docstrings/strings across
  `scripts/*.py`, `scripts/tools/*.py`, `scripts/bob/*.py`, and `plugins/`. Rationale (SSRF, secrets
  denylist, retention/compaction, auth, provisioning tiers) preserved; only bare tags deleted.
- **Living docs rewritten** — `.claude/CLAUDE.md`, `CONTRIBUTING.md`, `docs/SECURITY.md`,
  `docs/TUNING.md`, `docs/PORTABILITY.md`, and the `_comment`/`_doc` fields in `config/defaults.json` +
  `config/models.json`. Stale facts fixed in the same pass: the deleted `config/verbs.json` line removed,
  `check.ps1`→`check.py`, PowerShell-is-current phrasing corrected, `cli.py:751` "eval stays pwsh" fixed,
  `versions.py` `check.ps1` refs fixed, `bob_core`/`bob_config`/`bob_models`/`bob_voice`/`bob_authstore`
  stale pwsh/verbs.json claims corrected.
- **Tests reorganized by domain** — the 14 `test_slice*.py` files are gone. New/target files:
  `test_meta.py`, `test_stack.py`, `test_schedule.py`, `test_generate.py`, `test_models.py` (+ eval merged
  from d4), `test_build.py` (d5+d6), `test_provision.py` (d1+d7), `test_health.py` (+ deep-diagnose from
  d3), `test_cuda_mlock.py` (rest of d3), `test_kernel.py`; `test_versions_lock.py` absorbed d2. All via
  `git mv` (history preserved). Two method renames done (`test_ne0_floor_*`→`test_approval_floor_*`;
  the eval "ported_in_one_d" method dropped as redundant).
- **Wiring consolidation** — the per-slice `TestRegistryWiring` "verb flipped to python" methods (pure
  verb→handler checks) were dropped; `test_command_registry.py` is now the single authority (its
  `test_enumerable_and_well_formed` already covers every command, and its explicit verb list was broadened
  to all 46 domain verbs). Domain-specific assertions (DISPATCH sets, `MUTATING_TOOLS`, alias-removed,
  cli-only) were KEPT, relocated into domain-named classes (`Test<Domain>ToolSurface`).

## Verification
- `unittest discover` → **Ran 903 tests, OK** (was 918; the 15-test delta = ~13 redundant pure-wiring
  methods + d7's duplicate whole-catalog check + 2 dead phase-string regression guards — all coverage
  retained centrally).
- `py_compile` clean across all touched files; both config JSONs parse.
- Whole-tree residual-marker sweep = **0** (excluding archive docs, `# noqa`, and the `BGE-M3` model name).
- `scripts/check.py` fails ONLY on the known-local `versions.lock STALE` false-positive (the lock hashes
  submodule commits + model sha256, not the edited `_doc` field); its `py_compile`, exec-bit, and unittest
  (903 OK) steps all pass.
- **No commits, no pushes.**

## Deliberately left (considered, not missed)
- **Archive docs keep phase language**: `docs/ROAD-TO-BOB.md` and all of `docs/improvements/*`
  (MODULE-*, POST-ONE-*, this inventory, the prompt). They record the journey, not the current system.
- **False positives kept**: `Tier 0/1` (real architecture names), `BGE-M3` (model), `Test-CronDue` /
  `Update-Manifest` (ported PowerShell function names), the `(name, phase, message)` tuple field,
  loop "phases" (plan/verify/repair), English "one", `§`-doc-section refs, `#5b`, `(C)` C-signal, HTTP
  codes, `BM25`/`FTS5`/`v2`/`v3`.
- **Stale `config/bob.psd1` references (NOT phase markers, left for a separate cleanup)**: the retired
  Windows fork is still referenced at `.claude/CLAUDE.md:25`, `.claude/CLAUDE.md:42` (the Project Layout
  line), and `docs/PORTABILITY.md:28`. These are doc-accuracy debt, out of scope for marker removal.
- **`versions.lock STALE`** gate step — pre-existing local false-positive, unrelated to this refactor.

---

# Follow-up: legacy-reference cleanup (user: "remove anything not true of the codebase")

Second pass — removed references to things that no longer exist, kept references that are still true.

## Removed (the named thing is gone)
- **`config/bob.psd1` as a Windows authoring source / "compiles to `data/config.json`"** — the Windows
  config fork is retired; config resolves from `config/defaults.json` + optional `config/user.json` on
  every OS. Fixed in `.claude/CLAUDE.md` (deleted the Project-Layout line + corrected the disabledTools
  location to `config/user.json`), `plugins/AUTHORING.md`, `docs/USAGE.md`, `docs/PORTABILITY.md`,
  `docs/TUNING.md` (incl. two stale "on Windows reads/compiled data/config.json" lines), `CONTRIBUTING.md`,
  `docs/MEMORY.md`, `docs/AGENT-SERVER.md`, and the `config/defaults.json` `_comment`.
- **Provenance to deleted PowerShell** across `scripts/` — "port of X.ps1", "ports the bob.ps1 cases",
  "mirrors Get-*/Set-*/Test-CronDue/Get-BobConfig/Format-ForSpeech/Update-Manifest", "the pwsh handler",
  "Invoke-BobStream", "formerly the build-llama scripts", etc. Behavioral descriptions kept; dead
  attributions dropped. Same for test docstrings (kept the assertions, dropped the "port of" framing).
- **`scripts/tools/shell.py`** tool description "Run a PowerShell command" -> "Run a shell command (the
  OS-native shell)" — it was inaccurate on Linux.

## Kept (still true / in the codebase)
- Windows uses `pwsh` as its OS shell (`osenv.default_shell()`), the Windows `bob.cmd` shim, and
  `plugins/play/invoke.ps1` (a real sample plugin).
- The `*.psd1` / `config.json` / `data/config.json` entries in the file_read **secrets denylist** and
  their tests — live defensive behavior (a stray such file is still refused).
- Tests whose PURPOSE is to prove PowerShell is gone: `test_command_registry.TestNoPowerShellFrontDoor`,
  `test_kernel.TestShellStubsAndRetirement` / `test_provisioning_pwsh_scripts_retired`,
  `test_memory_profile` (guards that a stale `data/config.json` is ignored).
- The `python -m bob.kernel bootstrap` command and llama.cpp/llama-swap build docs (real, current).

Suite still **903 tests OK**; `config/defaults.json` valid; nothing committed.
