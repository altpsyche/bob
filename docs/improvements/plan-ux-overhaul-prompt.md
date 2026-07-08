# Plan the Bob shell UI/UX overhaul — cold-start planning handoff

**Paste this into a fresh Claude Code session at the repo root (`/home/siva/dev/bob`).** Your job is to
produce an **implementation plan** for the interactive `bob` shell UI/UX overhaul — not to write feature
code yet. Verify every claim below against the repo before planning (line numbers and counts drift).

## What you're planning

The interactive `bob` shell (the no-arg TTY front door) is built on `rich` (rendering) + `prompt_toolkit`
(input). The overhaul backlog already exists and was just refreshed:

- **Read first:** `docs/improvements/MODULE-UX-overhaul.md` — the research-backed plan (UX-1 input UX,
  UX-2 slash discovery, UX-3 adaptive/accessible theming, UX-4 transcript polish, UX-5 optional async
  loop, UX-6 optional alt-screen). Its headline decision stands: **keep `rich` + `prompt_toolkit`, do
  NOT adopt Textual.** All UX-1..UX-6 are currently unimplemented.
- **Code:** `scripts/bob/shell.py` (input + turn loop + cockpit), `scripts/bob/theme.py` (theme tokens,
  `unicode_ok`, `ptk_color`), `scripts/bob/render.py` (rich views).
- **Tests:** `tests/test_bob_shell.py`, `tests/test_theme.py`, `tests/test_render.py`.

## Current state to respect (recent work — do not regress it)

This repo just went through a large cleanup; the plan and code you'll touch already reflect it:

- **One Python engine, zero PowerShell.** Everything is `python -m bob`; the only tracked `.ps1` is a
  sample plugin. Windows still uses `pwsh` as its OS *tool* shell via `osenv` — keep that; don't add any
  PowerShell orchestration.
- **The codebase was fully decoupled from development-phase markers.** There are no `O#/N#/M#/NE#/NB#/
  MEM-#/ONE-*/slice` tags left in code, tests, comments, or docs. Test files are named by domain
  (`test_bob_shell.py`, not `test_sliceN_*.py`).
- **The shell already has a cockpit layer** (POST-ONE-2): slash commands `/up /restart /webui /services
  /stop /logs` with `_render_dashboard` + in-place re-render feedback (`shell.py` ~579-699). Fold this
  into your plan (UX-2 now has ~20 slash commands to document; UX-4 can build on the dashboard
  re-render pattern).
- **Baselines the overhaul changes:** the completer is a bare `NestedCompleter.from_nested_dict(_SLASH)`
  with `complete_while_typing=False` (shell.py ~443-444); slash-command help lives in the hand-maintained
  `_SLASH_HELP` list (shell.py ~512-533), NOT the `bob.registry` CLI-verb catalog (that's a separate
  surface rendered by `render.commands_view`). Theme is a single mauve palette in `theme.py`; there is no
  `NO_COLOR`/adaptive-theme handling yet.

## Hard conventions (these are the point — enforce them in the plan)

1. **No development-phase / slice markers anywhere.** Do not introduce `UX-1`, `UX-2`, `O#`, `N#`, or any
   ledger tag into code, comments, docstrings, test names, or test file names. Those labels may appear in
   the PLAN document as a backlog index, but they must never leak into the codebase. Comments explain
   **what the code does and why**, never when it was built or which plan item it came from.
2. **No phase/slice-based tests.** Add tests to the existing behavior-named files, or create new
   behavior-named files (e.g. `tests/test_completion.py`, `tests/test_shell_theme.py`) named for the unit
   under test. Test classes and methods are named by **behavior** (`TestFuzzyCompletion`,
   `test_menu_opens_on_slash`), never by a phase/slice. Keep the `test_*.py` prefix (unittest discovery).
3. **Clean, DRY, solid.** One source of truth per concern; no duplicated logic. Reuse the existing seams
   (theme tokens in `theme.py`, the `_SLASH`/`_SLASH_HELP` pair, `render.py` views, the single
   `PromptSession`). If a slice needs a new seam, add ONE and route all callers through it. Follow the
   repo's placement rules in `.claude/CLAUDE.md` (three-layer capability model; shared constants only in
   `config/defaults.json`; OS-specific behavior only through `scripts/osenv.py`). No new UI-text
   regressions: no emoji, no em-dash in user-facing strings (functional glyphs `● ○ ✓ ✗ ·` are fine).
   Cross-OS: don't break Windows.
4. **`_SLASH` and `_SLASH_HELP` must stay a single source.** If discovery (UX-2) renders per-command
   descriptions, they come from `_SLASH_HELP` — do not create a second parallel list. Ideally unify the
   tree and the help into one structure so a new slash command is one edit, not three.

## What to produce (the deliverable)

A concrete, reviewable implementation plan, structured as **small, independently landable increments**,
each with:

- **Scope** — one behavior, shippable alone, gate green after it.
- **Exact files/functions touched** (verified against the current tree) and the **DRY seam** it reuses or
  introduces.
- **Behavior-named tests to add** (which file, what they pin) — hermetic, no network, no live model.
- **Acceptance** — the observable behavior that proves it works.
- **Risk / cross-OS notes.**

Order the increments high-daily-value-first and low-risk-first. A sensible spine (confirm against the
plan doc): input UX (fuzzy completion + `AutoSuggestFromHistory` + menu-on-`/`) → slash discovery from a
unified `_SLASH_HELP` → adaptive/accessible theming (`NO_COLOR`, light/dark, color-depth) → transcript
polish (diff rendering, multi-stage Ctrl-C, adaptive refresh) → the optional async-loop / alt-screen tail.
Note where UX-4's diff rendering should be designed so a future coding-agent `file_edit` diff can plug
into the same renderer (no duplicate diff code later).

Also do an **up-front reality audit** (a short section): read the current `shell.py`/`theme.py`/
`render.py`, list what each UX item's baseline actually is today, and flag anything in the plan doc that
no longer matches the code so the plan starts from truth.

## Constraints

- **Plan first, don't build.** Produce the plan and get sign-off on scope/sequence before writing feature
  code. (If asked to proceed, implement one increment at a time, gate green after each.)
- **Do NOT `git commit` or `git push`** (also in `.claude/CLAUDE.md`). Work in the tree; leave it for
  review.
- **Keep the gate green.** Run `tools/venv-litellm/bin/python scripts/check.py`; it exits 1 locally only
  on the known `versions.lock STALE` false-positive, so gate on the unittest `Ran N ... OK` line:
  `cd tests && ../tools/venv-litellm/bin/python -m unittest discover -s . -p 'test_*.py'` (~903 tests
  today). After any test-file change, re-run discovery to confirm the count didn't silently drop.
- The shell is behind an isatty gate (scripts/CI never enter it); keep any new interactive behavior
  testable headlessly (inject fakes / drive the seams, don't require a real TTY in tests).

Start by reading `docs/improvements/MODULE-UX-overhaul.md` and the three shell files, reconcile the plan
against the current code, then propose the increment sequence for sign-off.
