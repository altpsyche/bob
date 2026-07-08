# Decouple the codebase + tests from development-phase markers — investigation prompt

**Paste this into a fresh Claude Code session at the repo root.** It is a cold-start handoff: verify
every claim against the repo before acting (counts and line numbers drift). The job is first to FIND
comprehensively, then to propose a remediation — do not start editing until the inventory is agreed.

## The problem

The code and tests are tattooed with **development-phase / slice markers** — short alphanumeric tags
that reference a planning ledger nobody reading the code has: `A1`, `B4`, `O3`, `O12`, `M9`, `M14`,
`N7`, `NE0`, `NB3`, `MEM-6`, `S8`, `C3`, `D8`, `ONE-B2`, `ONE-D`, `WI-7`, `ND1`, and the test files
`test_slice2_stack.py`, `test_slice_d5_build.py`, etc. They appear in comments, docstrings, test file
names, and test class/method names.

These tags couple the code to *when* a change was made in the project's history instead of describing
*what the code does and why*. A newcomer (or the user) can't decode `O13` or `MEM-10`; the information
they carry is only meaningful next to a plan document. The user wants them gone — the codebase and the
tests should read as a coherent system organized by **behavior and domain**, not by the sequence of
development phases that produced them.

Example of the smell (real, from `scripts/tools/web.py`):
```python
_MAX_REDIRECTS = 5  # NE0 — cap manual redirect following so each hop can be re-validated
```
The valuable part is "cap manual redirect following so each hop can be re-validated." The `NE0 —`
prefix is noise. And `tests/test_slice_d5_build.py` should be named for what it tests (the llama.cpp
build), not the slice number that introduced it.

## Ground truth (verify, then use as a starting map)

As of writing (`grep` over `scripts/ plugins/ tests/`, `--include='*.py'`):
- **~890 marker-token occurrences across ~79 Python files.**
- **14 phase-named test files**: `test_slice1_meta.py`, `test_slice2_stack.py`, `test_slice3_health.py`,
  `test_slice4_models.py`, `test_slice5_schedule.py`, `test_slice6_generate.py`,
  `test_slice_d1_fetch.py`, `test_slice_d2_lock.py`, `test_slice_d3_mlock_diag.py`,
  `test_slice_d4_eval.py`, `test_slice_d5_build.py`, `test_slice_d6_update.py`,
  `test_slice_d7_setup_voice.py`, `test_slice_d8_kernel.py`.
- **~13 docs** under `docs/` reference the same markers.
- No test module imports another test module by filename, so renaming test files is safe **provided**
  the `test_*.py` prefix stays (unittest/pytest discovery depends on it). Still: grep `ci.yml`,
  `scripts/check.py`, `scripts/smoke.py`, and `docs/` for any hardcoded test-file names before renaming.

Observed marker families (non-exhaustive — the investigation must derive the full set):
`ONE-A|B|C|D|E` (+ `ONE-B1..B5`), `O0..O16`, `N0..N9`, `M0..M14`, `MEM-0..10`, `NE0..NE2`,
`NB1..NB4`, `C0..C3`, `D0..D8`, `S0..S9`, `A0..A9`, `B0..B9`, `WI-*`, `ND*`, and the literal word
`slice`.

## What counts as "an issue" (cast the net wide)

1. **Tag prefixes in comments/docstrings** — `# O2 — …`, `# MEM-6/7 …`, `"""M13 — …"""`, `(N7)`,
   `# NE0/M9 …`. The tag is noise; the prose after it is usually worth keeping (rewritten).
2. **Phase-named test files** — `test_slice*.py` → renamed for the module/behavior under test.
3. **Phase-named test classes/methods** — e.g. a class or `test_…` named after a slice/phase rather
   than the behavior. (Most method names here already describe behavior — confirm, don't assume.)
4. **Phase-organized structure** — any module, section header, or test grouping arranged "by slice/
   phase" rather than by domain. Flag structural coupling, not just string tags.
5. **Docs** that are pure historical ledgers vs. docs that describe the current system. (Archival plan
   docs may legitimately keep their phase language — see below. The goal is the *code and tests*.)

## Careful cases — do NOT blindly strip or rename

- **False positives.** Short tokens like `N1`, `O2`, `S3`, `A1`, `D4` also occur as legitimate content
  (variable names, model/arch strings like `sm_120`, math, CLI flags, IDs). Every hit must be judged
  in context — is it a *phase reference* or real content? A safe signal: the tag sits at the start of a
  comment/docstring followed by `—`/`-`/`:` and an explanation, or in parentheses as `(N7)`.
- **Tags that became de-facto architecture names.** `NB1..NB4` are used in `.claude/CLAUDE.md` as names
  for real seams ("NB3 — the OS seam", "NB1 — neutral single source of truth"). The *concept* is good;
  the *label* is the noise. Rewrite `NB3 — OS-specific behavior goes through osenv` to
  `OS-specific behavior goes through the osenv seam (scripts/osenv.py)` — keep the meaning, drop the
  code. Do the same in CLAUDE.md and the Key Patterns section.
- **Preserve the WHY.** Many tagged comments encode real rationale (SSRF hardening, cache design,
  fallback ordering, decision records). Remediation is *rewrite to be timeless*, not delete. If a
  comment is ONLY a tag with no content, delete it.
- **Archival plan docs.** `docs/improvements/*` and any ROAD-TO / MODULE-* ledgers are historical
  records; they may keep phase language. Confirm with the user which docs are "living" (describe the
  system now) vs "archive" (describe the journey). Only rewrite living docs.

## Method — fan out, then converge

Do NOT try to read 79 files in one pass. Raise agents to dig in parallel, then dedupe and verify:

1. **Discovery fan-out.** Split the tree into areas (`scripts/`, `scripts/tools/`, `scripts/bob/`,
   `plugins/`, `tests/`, `docs/`, `.claude/`, config). Launch one Explore/general-purpose agent per
   area to enumerate every phase-marker occurrence with file:line, the surrounding text, and a
   judgment (real phase marker vs. false positive). Each agent returns a structured list, not file
   dumps.
2. **Build one inventory.** Merge into a single categorized list: (a) comment/docstring tags,
   (b) test-file renames, (c) test class/method renames, (d) structural/organizational coupling,
   (e) docs. Include a proposed rewrite/new-name for each and flag every judgment call (false-positive
   candidates, NB-style concept-labels, archival docs).
3. **Verification pass.** A second agent (or a skeptical re-read) checks the false-positive calls and
   the "keep the concept" calls — the cost of a wrong strip is a lost rationale or a broken build flag.
4. **Present the inventory to the user and get sign-off on scope** (especially: which docs are living,
   whether NB-seam names get renamed or just de-tagged, and the test-file naming scheme) BEFORE editing.
5. **Remediate in small, reviewable batches**, keeping the gate green after each. Suggested order:
   comment/docstring de-tagging per module → test class/method renames → test file renames (with the
   grep-for-references check first) → docs → CLAUDE.md. Prefer many small diffs over one giant sweep.

## Remediation principles

- **Describe behavior and domain, not history.** A comment says why the code is the way it is; a test
  name says what behavior it pins. Neither should reference a phase, slice, or ledger id.
- **Rename test files by the unit under test**, e.g. `test_slice_d5_build.py` → `test_build.py`,
  `test_slice2_stack.py` → `test_stack.py`, `test_slice6_generate.py` → `test_generate.py`. Resolve
  collisions with existing files by merging or by a domain-qualified name. Keep the `test_` prefix.
  Use `git mv` so history follows (do NOT commit — staging the move in the working tree is fine).
- **Strip the tag, keep the prose.** `# O15 — context editing: once the transcript passes …` becomes
  `# Context editing: once the transcript passes …`.
- **Delete content-free tags.** A bare `# S1` with nothing else goes away.
- **No information loss.** If a tag pointed at a genuine decision worth recording, the rationale stays
  (in the comment, or a short note in a living doc) — just without the ledger id.

## Constraints (hard)

- **Do NOT `git commit` or `git push`.** The user reviews and commits. Work in the tree, leave it for
  review. (This is also in `.claude/CLAUDE.md`.)
- **Keep the gate green.** `tools/venv-litellm/bin/python scripts/check.py`; it exits 1 locally only on
  the known `versions.lock STALE` false-positive, so gate on the unittest `Ran N … OK` line
  (`cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'`). After any
  test-file rename, re-run discovery to confirm the suite count is unchanged (nothing silently dropped).
- **Cross-OS + no new UI-text regressions.** Don't break Windows; keep the no-emoji / no-em-dash rule
  for user-facing strings (functional glyphs `● ○ ✓ ✗ ·` are fine).
- **Behavior must not change.** This is a naming/comment refactor only — zero runtime behavior change.
  Renames and comment edits only; if a "comment" is load-bearing (e.g. a `# noqa`, `# type:` or a
  pragma), leave the directive intact.

## Deliverables

1. A single categorized **inventory** (file:line, current text, proposed change, judgment flags).
2. After sign-off, the **working-tree changes** applied in small batches, gate green, nothing committed.
3. A short note of what was **deliberately left** (archival docs, real content that looked like a tag,
   any NB-style names the user chose to keep) so the next reader knows it was considered, not missed.

Start by reproducing the ground-truth counts, then launch the discovery fan-out.
