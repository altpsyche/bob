# Bob Shell — UI/UX Overhaul (research-backed plan)

Status: draft (2026-07-03). Scope: the interactive `bob` shell ([scripts/bob/shell.py](../../scripts/bob/shell.py), [theme.py](../../scripts/bob/theme.py), [render.py](../../scripts/bob/render.py)). Built on `rich` (render) + `prompt_toolkit` (input). Informed by a deep-research pass (27 sources, 25 claims adversarially verified 24-confirmed / 1-refuted); primary sources cited inline.

---

## 0. Headline decision — keep the stack, don't adopt Textual

**Verified (3-0): stay on `rich` + `prompt_toolkit`; do NOT rewrite in Textual.**
- Textual's default `App.run()` enters full-screen *application mode* on the alternate screen buffer (like vim/htop) — the exact behavior our inline transcript avoids. ([textual.textualize.io/guide/app](https://textual.textualize.io/guide/app/))
- Textual's **inline mode** (the only feature that would preserve scrollback) is **explicitly unsupported on Windows** — our target platform (issue #4409 → PR #4501 only *documented* the limitation; CHANGELOG through 8.2.8 / 2026-06-30 adds nothing). The claim that Textual inline mode solves our scrollback goal was **refuted 0-3**.
- If a full-screen mode is ever wanted, **Rich already provides it** natively via `Console.screen()` / `set_alt_screen` — no framework change. Frontier tools treat inline vs. alt-screen as a *runtime-selectable* mode, not a fixed choice: Claude Code ships `/tui default|fullscreen` (default "keeps the conversation in your terminal's native scrollback"; fullscreen is an opt-in "flicker-free alt-screen renderer"). ([code.claude.com/docs/…/fullscreen](https://code.claude.com/docs/en/fullscreen), [rich console docs](https://rich.readthedocs.io/en/latest/console.html))

So the inline-transcript architecture is validated. This overhaul is *refinement within the stack*, not a rewrite.

---

## 1. Current state (audit) + gap analysis

What Bob already does well (keep):
- **Splash** — pyfiglet wordmark + per-char gradient, tagline, health pill, accent counts, rule, tip ([theme.py](../../scripts/bob/theme.py) `render_header`, [shell.py](../../scripts/bob/shell.py) `_print_splash`).
- **Streaming** — `rich.Live(Markdown)` per text segment at `refresh_per_second=8`, `transient` left default so each message stays in scrollback ([shell.py](../../scripts/bob/shell.py) `_TurnRenderer`). This matches the verified inline-transcript pattern.
- **Coexistence done right** — `patch_stdout()` wraps `session.prompt()`; `Live` runs only during a turn (prompt idle). This already honors the research caveat that Rich `Live` redirect and prompt_toolkit `patch_stdout` must be **mutually exclusive per phase**.
- **Fallbacks** — `_force_utf8()` ([shell.py](../../scripts/bob/shell.py)), `unicode_ok()` glyph/ASCII fallback ([theme.py](../../scripts/bob/theme.py) `unicode_ok`), ASCII figlet font on non-UTF-8.
- **Approval** — risk-colored panel + `y/N/a`, session-scoped "always" set ([shell.py](../../scripts/bob/shell.py) `_approve`).
- **Slash tree** — `NestedCompleter` over `_SLASH`.
- **Cockpit (added by POST-ONE-2, landed 2026-07-07; not in the original audit)** — the shell now manages the whole stack from inside: `/up`, `/restart`, `/webui`, `/services [start|stop [name]]`, `/stop`, `/logs`, driven by a `_render_dashboard` + in-place re-render feedback loop (each toggle re-renders the dashboard so the changed row flips ●/○) ([shell.py](../../scripts/bob/shell.py) `_render_dashboard` at :579-699, re-render calls at :633/:665/:671/:693). This is now the primary "home base" surface, so the slash set has grown to ~20 commands (see UX-2) and the dashboard re-render pattern is a foundation UX-4's transcript polish can build on.

Gaps the research flags (build these):

| Area | Gap today | Verified best practice |
|---|---|---|
| Input | No history autosuggest | Fish-style `AutoSuggestFromHistory` ghost text (right/Ctrl-E accept, Alt-F first word) |
| Input | `NestedCompleter` only; `complete_while_typing=False` | Wrap in `FuzzyCompleter`; auto-show menu on `/` (live filter is the *primary* discovery affordance) |
| Input | Single-line only | `multiline=True` w/ Meta+Enter submit + continuation gutter (for pasting code / long prompts) |
| Color | `color_system` left auto; no NO_COLOR; prompt_toolkit `color_depth` unset | Author truecolor + verify 16-color downgrade; honor `NO_COLOR`; align prompt_toolkit `color_depth` to Rich |
| Theme | One mauve theme | auto (terminal light/dark) + light/dark + daltonized (colorblind) + ANSI (terminal-palette, doubles as fallback) + user themes |
| Transcript | No diff rendering | Syntax-highlighted diffs with +/- gutters, collapse large diffs |
| Cancel | Ctrl-C at prompt clears line; in-turn cancels | Two-stage: 1st Ctrl-C cancels the turn, 2nd exits |
| Streaming | `refresh=8` unbenchmarked; full-markdown reparse each frame is O(content) | Benchmark growing-transcript reparse; cap/þrottle refresh or drive manually |

---

## 2. Research summary (verified, cited)

**Interaction patterns**
- Slash discovery = **live incremental "/" filter** ("Type / to see every command, or / then letters to filter") — primary affordance, `/help` is the fallback. ([code.claude.com/docs/…/commands](https://code.claude.com/docs/en/commands))
- Theming = terminal-adaptive: `auto` (matches terminal light/dark bg), explicit light/dark, **daltonized** (deuteranopia-oriented) colorblind variants, **ANSI** themes that defer to the terminal's own 16 colors (legacy/no-truecolor), + user themes dir. ([code.claude.com/docs/…/terminal-config](https://code.claude.com/docs/en/terminal-config))

**Rendering / streaming (rich)**
- `Live.refresh_per_second` (default 4) is the re-render cost lever; raise for smoother, lower for infrequent — re-parsing a growing Markdown block is O(content)/frame. ([rich live docs](https://rich.readthedocs.io/en/latest/live.html))
- `Live` keeps the last frame in scrollback unless `transient=True`; `live.console.print(...)` lands **above** the live region; `redirect_stdout/stderr` on by default. ([rich live docs](https://rich.readthedocs.io/en/latest/live.html))
- Color ladder: `color_system` ∈ auto/standard(16)/256/truecolor/windows; `auto` downgrades to nearest — author truecolor, but **verify 16-color** explicitly (detection via COLORTERM/TERM is imperfect, rich #1640). ([rich console docs](https://rich.readthedocs.io/en/stable/console.html))

**Input (prompt_toolkit)**
- `prompt_async()` inside `patch_stdout()` is the sanctioned asyncio pattern to keep an input line live while coroutines stream. Edge bugs to guard: prints near exit can drop (#1079); heavy output can slow ~5× (#682). ([asking_for_input](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html))
- `AutoSuggestFromHistory` = fish-style ghost text (opt-in; pair with `FileHistory`). `NestedCompleter.from_nested_dict` + `FuzzyCompleter(...)` = hierarchical + typo-tolerant slash completion by composition. ([asking_for_input](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html))
- `multiline=True` rebinds Enter→newline; submit = Meta+Enter / Esc then Enter; continuation via `prompt_continuation`. Document the submit gesture (non-obvious). ([asking_for_input](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html))
- Ctrl-C = **bind `c-c`**, not `signal.signal(SIGINT)` (raw mode delivers 0x03 as a keypress); default `<sigint>` binding raises KeyboardInterrupt. ([key_bindings](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/key_bindings.html))
- `color_depth` ∈ 1/4/8/24-bit — set consistently with Rich's detected system. ([ptpython config](https://github.com/prompt-toolkit/ptpython/blob/main/examples/ptpython_config/config.py))

**Accessibility**
- `NO_COLOR`: when present **and non-empty**, suppress all added color (empty value does NOT disable). ([no-color.org](https://no-color.org/))

---

## 3. Phased plan (independently landable, each with tests)

### UX-1 — Input UX (highest daily value, low risk)
- Add `auto_suggest=AutoSuggestFromHistory()` to the `PromptSession` ([shell.py](../../scripts/bob/shell.py) `run`).
- Wrap the completer: `FuzzyCompleter(NestedCompleter.from_nested_dict(_SLASH))`; set `complete_while_typing=True` so `/` shows the menu immediately and filters live (the verified primary discovery affordance).
- Enable `multiline=True` with a `prompt_continuation` gutter (dim accent `…`) and a one-line hint that submit is Meta/Esc+Enter. Gate behind a theme/config flag (`input.multiline`) since it changes the Enter gesture.
- Set `color_depth` on the session to match Rich's detected system.
- *Tests:* completer wraps fuzzy + nested; autosuggest object attached; multiline flag threads from config; `_SLASH` unchanged. (prompt_toolkit interaction itself stays manual-acceptance — no TTY in CI.)

### UX-2 — Slash discovery + command meta
- Replace the plain `NestedCompleter` values with a custom completer (or `NestedCompleter` + a `meta_dict`) that shows a one-line **description per command** in the completion menu, so `/` is self-documenting. There are now ~20 slash commands to describe (the cockpit set — `/up`, `/restart`, `/webui`, `/services`, `/stop`, `/logs` — plus `/session`, `/skill`, etc.), not the ~10 of the original audit. Source the descriptions from the hand-maintained `_SLASH_HELP` list ([shell.py](../../scripts/bob/shell.py) `_SLASH_HELP` at :512-533), which `/help` already renders — this is the shell's OWN surface. Do NOT pull from the `bob.registry` command catalog: that is the separate outside-terminal CLI-verb surface (rendered by `render.commands_view`), deliberately distinct from the in-shell slash set. Keep `/help` as the full catalog.
- *Tests:* every `_SLASH_HELP` entry resolves to a description; the completer meta matches `_SLASH_HELP`; unknown `/x` still routes to the "unknown command" path.

### UX-3 — Theming: adaptive + accessible (biggest visual-identity win)
- Extend the theme to **named presets**: `mauve` (current), `light`, `dark`, `daltonized` (colorblind-safe), `ansi` (uses the terminal's 16 colors — doubles as the truecolor fallback). Selected via `ui.theme` (name) with `config/ui.json` still overriding individual keys. Optional `auto` that picks light/dark from the terminal background.
- **NO_COLOR** honored in [theme.py](../../scripts/bob/theme.py): when set non-empty, force a monochrome theme (and set Rich `no_color`). Verify prompt_toolkit respects it too.
- Verify the **16-color downgrade** renders acceptably (author truecolor, test with `color_system="standard"`); pin/align prompt_toolkit `color_depth`.
- *Tests:* each preset loads to a full `Theme`; `NO_COLOR` non-empty → monochrome, empty → color kept (the load-bearing semantics); ANSI preset uses only the 16 names; presets deep-merge under `config/ui.json`.

### UX-4 — Transcript polish
- **Diff rendering**: when a tool result is a diff / file change (e.g. `file_write`, future edits), render it with `rich.Syntax` + `+`/`-` gutters in success/error colors; collapse diffs over N lines with a "… (N more)" line. New render helper in [render.py](../../scripts/bob/render.py).
- **Ctrl-C two-stage**: 1st Ctrl-C cancels the in-flight turn (current behavior), 2nd within a short window exits — matching frontier convention. Keep the cancel path in `_consume`; add the "press again to exit" state.
- **Benchmark** the streaming markdown reparse cost as the block grows; if hot, throttle `refresh_per_second` adaptively or drive manual refresh on token batches (guard prompt_toolkit #682 heavy-output slowdown).
- *Tests:* diff renderer produces gutters + collapses; large-diff truncation; render loop unaffected (fake event stream).

### UX-5 — (Optional) async loop
- Migrate the worker-thread + `queue` consumer in `_consume` to the sanctioned `prompt_async()` + `patch_stdout()` asyncio pattern. Cleaner and matches the reference, but a real refactor — only if UX-1..4 expose friction. Guard #1079 (dropped prints near exit).
- *Tests:* event ordering + cancellation parity with the current threaded consumer.

### UX-6 — (Optional, future) alt-screen mode
- A `/tui fullscreen` toggle using Rich `Console.screen()` for a flicker-free full-screen mode, inline staying default (mirrors Claude Code's two-mode design). Low priority; only if requested.

**Suggested order:** UX-1 → UX-3 → UX-2 → UX-4, then optional UX-5/UX-6. UX-1 and UX-3 deliver the most felt improvement.

---

## 4. Risks & caveats (from research)
- Most library findings establish **what's possible**, not benchmarked UX — the markdown reparse cost (UX-4) must be measured, not assumed.
- Rich `Live` redirect and prompt_toolkit `patch_stdout` both want `sys.stdout`; **keep them phase-exclusive** (input phase = patch_stdout; streaming phase = Live) — Bob already does; preserve this when touching the loop.
- prompt_toolkit edge bugs: dropped prints near exit (#1079), ~5× slowdown under heavy output (#682).
- Claude Code's cited features (`/tui`, daltonized/ANSI themes, live `/` filter) are recent (v2.1.x, 2026) and evolving; treat as patterns to emulate, not APIs to depend on.
- 16-color downgrade can look poor; verify explicitly.

---

## 5. Open questions (research gaps — need a dedicated pass or first-principles design)
No primary-sourced claims survived verification on these (they were asked but under-covered), though relevant docs exist as starting points:
- **Tool-approval / trust UX** beyond the current session `y/N/a`: persistent per-command allowlists, `ask/allow/deny` tiers, argument-pattern matching. Sources to mine: [Claude Code permissions](https://code.claude.com/docs/en/permissions), [Codex agent approvals](https://developers.openai.com/codex/agent-approvals-security). (Overlaps Module O6.)
- **Session & memory affordances in the UI** — *partially answered by shipped work.* `/session new|list|resume|show` shipped ([shell.py](../../scripts/bob/shell.py) `_cmd_session` at :723-814), and end-of-session memory consolidation now surfaces indicators in the transcript ("saving session memory…", "remembered N fact(s) from this session"; [shell.py](../../scripts/bob/shell.py) `_consolidate_session` at :1141-1158). Remaining gap: in-turn **context / compaction indicators** (how full the window is, when a compaction happened). Source: [Claude Code memory](https://code.claude.com/docs/en/memory). (Overlaps WI-6/NE5 + [MODULE-MEM](MODULE-MEM-memory-redesign.md).)
- **Splash/onboarding & wordmark** — *partially answered by shipped work.* A one-time first-run welcome panel shipped ([shell.py](../../scripts/bob/shell.py) `_print_first_run` at :1110-1128), plus profile-seeding onboarding. Remaining gap: **prose width / diff presentation** specifics (no verified guidance) — design from the frontier-observed patterns above.

---

## 6. Non-goals
- Rewriting in **Textual** (refuted for Windows/inline) or any full-screen-by-default TUI.
- A GUI/web UI.
- The permission *policy* engine (Module O6) — UX-4's approval visuals only.
- Changing the agent loop's event protocol (NE0) — the transcript renders whatever events it emits.

---

### Sources (primary unless noted)
Claude Code: [commands](https://code.claude.com/docs/en/commands), [fullscreen](https://code.claude.com/docs/en/fullscreen), [terminal-config](https://code.claude.com/docs/en/terminal-config), [permissions](https://code.claude.com/docs/en/permissions), [memory](https://code.claude.com/docs/en/memory) ·
Rich: [live](https://rich.readthedocs.io/en/latest/live.html), [console](https://rich.readthedocs.io/en/stable/console.html) ·
prompt_toolkit: [asking_for_input](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html), [key_bindings](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/key_bindings.html), [nested.py](https://github.com/prompt-toolkit/python-prompt-toolkit/blob/main/src/prompt_toolkit/completion/nested.py) ·
Textual: [app guide](https://textual.textualize.io/guide/app/) (+ issue #4409 / PR #4501) ·
[no-color.org](https://no-color.org/) · Codex: [agent-approvals](https://developers.openai.com/codex/agent-approvals-security)
