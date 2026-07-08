"""The interactive REPL/TUI: the no-arg `bob` front door on an interactive TTY.

Splash (header + model/role + session + tool/skill counts) + a prompt. Non-slash input is an agent
turn; slash commands drive the shell: a turn (`/agent`, `/voice`, `/skill`), inspection (`/tools`,
`/skills`, `/status`, `/help`), state (`/model`, `/agency`, `/session`, `/theme`, `/clear`, `/reset`),
and the cockpit that manages the whole stack from inside (`/up`, `/restart`, `/webui`, `/services`,
`/stop`, `/logs`). Every command is one entry in `_COMMANDS`; the completion tree, the dispatch table, and the
`/help` listing all derive from it, so a new command is a single edit. The turn drives
`run_agent_events` (bob_loop) — the SAME event stream the HTTP server consumes ([bob_agent_server.py])
— so a new event type surfaces by adding one case in `_TurnRenderer.handle`, never a shell rewrite.

Rendering aims at frontier-grade *inline* UX (like Claude Code / aider), NOT a full-screen TUI: a
scrolling transcript where assistant text streams as live Markdown (code/tables render), tool calls are
their own blocks with a spinner + ✓/✗ result, a styled prompt carries a persistent bottom toolbar, and
approvals show a risk-coloured panel. A full-screen alternate-buffer layout is deliberately avoided — it
breaks native scrollback and is fragile across terminals.

The look lives in [bob.theme](theme.py): a typed `Theme` parsed once from `config/ui.json` (the single
editing surface — colours, header font/gradient, glyphs, spacing, layout toggles). `/theme` shows it and
reloads the file. Everything degrades safely — no pyfiglet → a bold header; non-UTF-8 console → ASCII
glyphs + an ASCII font.

Coexistence: rich renders; prompt_toolkit owns input (line editing / history / slash
completion) — readline is absent from Windows CPython, so stdlib input() has no editing there. The two
are never active at once: the prompt is idle while a turn streams, and a turn is fully quiescent while an
approval prompt is up.

Approval: the loop is event-driven, not a blocking input(). The turn generator runs in a worker
thread pushing events onto a queue; the main thread renders them. When the loop needs approval it yields
`approval_required` (→ queue) then blocks its own `approve()` on an answer queue; the main thread sees
the event, prompts the user, and hands the decision back. Ctrl-C trips the shared CancelToken → the turn
returns to the prompt, never the OS.

Built behind an isatty gate: scripts/CI (no TTY) never enter the shell — `run()` refuses
and prints help instead, so a redirected/piped `bob` keeps today's behaviour.
"""
import json
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

# The runtime modules live in scripts/ and scripts/tools/ (tool_registry, bob_core, bob_loop, …). Under
# `python -m bob` only the package's parent (scripts/) is guaranteed importable; add both explicitly so
# `from tool_registry import …` works regardless of how the process was launched (mirrors bob_loop.py).
_SCRIPTS = Path(__file__).resolve().parent.parent          # scripts/bob/shell.py -> scripts
for _p in (_SCRIPTS, _SCRIPTS / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from bob import catalog
from bob import theme as theme_mod
from bob.theme import Theme

_SENTINEL = object()

# Streamed-Markdown refresh rate. Used BOTH for rich.Live's redraw cadence and to throttle how often
# the growing buffer is re-parsed: a full Markdown parse is O(buffer), so parsing on every token makes
# a long answer O(n^2). Rebuilding the renderable at most this many times/second keeps it ~linear while
# staying visually smooth; the final buffer is always rendered once the segment completes.
_STREAM_HZ = 8

# Spoken words that leave /voice mode (matched after stripping trailing punctuation). Ctrl-C while
# listening does the same.
_VOICE_EXIT_WORDS = {"exit", "quit", "stop", "goodbye", "bye"}

# The shell's slash commands, defined ONCE. The completion tree (`_SLASH`), the dispatch handler
# table (BobShell.dispatch), and the /help listing (_cmd_help) all derive from this list, so adding a
# command is a single entry here. `handler` is a BobShell method name (bound via getattr at dispatch);
# "" marks a command handled inline (only /exit — it returns False to leave the REPL). `args` is the
# help-display argument syntax; `subs` are completion-only sub-commands; `aliases` are extra names that
# complete + dispatch but aren't listed in /help (e.g. /quit for /exit). It stays the TUI's OWN
# surface — what you can DO from inside `bob` — deliberately NOT the CLI verb catalog (`bob help`).
@dataclass(frozen=True)
class _Cmd:
    name: str
    desc: str
    handler: str = ""
    args: str = ""
    subs: tuple = ()
    aliases: tuple = ()

    def names(self) -> tuple:
        return (self.name, *self.aliases)


_COMMANDS = [
    _Cmd("/agent", "run the agent loop on a one-shot goal", "_run_turn", args="<goal>"),
    _Cmd("/voice", "spoken conversation (mic → loop → speech)", "_cmd_voice"),
    _Cmd("/model", "show or switch the role (chat, coder, planner, …)", "_cmd_model", args="[role]"),
    _Cmd("/agency", "tool-approval mode: show | confirm | silent", "_cmd_agency",
         args="[level]", subs=("show", "confirm", "silent")),
    _Cmd("/session", "persisted conversation history", "_cmd_session",
         args="[new|list|resume <ref>|name <text>|delete <ref>|show]",
         subs=("new", "list", "resume", "name", "delete", "show")),
    _Cmd("/skill", "list or run a skill", "_cmd_skill", args="[name]"),
    _Cmd("/tools", "list the agent's tools", "_cmd_tools"),
    _Cmd("/skills", "list available skills", "_cmd_skills"),
    _Cmd("/status", "system dashboard — every service, up/down", "_cmd_status"),
    _Cmd("/services", "service dashboard; toggle a service in place", "_cmd_services",
         args="[start|stop [name]]", subs=("start", "stop")),
    _Cmd("/up", "start the stack in the background (endpoint + proxy + WebUI)", "_cmd_up",
         args="[--with-services]"),
    _Cmd("/restart", "restart the inference endpoint", "_cmd_restart"),
    _Cmd("/webui", "open the Open WebUI browser tab", "_cmd_webui"),
    _Cmd("/stop", "stop local inference (frees VRAM)", "_cmd_stop"),
    _Cmd("/logs", "recent inference-server log", "_cmd_logs"),
    _Cmd("/theme", "switch the colour theme or reload it", "_cmd_theme",
         args="[<preset>|reload]", subs=("reload",)),
    _Cmd("/clear", "clear the conversation context (keeps the saved session)", "_cmd_clear"),
    _Cmd("/reset", "wipe ALL local data and return to first-run", "_cmd_reset"),
    _Cmd("/help", "this reference", "_cmd_help"),
    _Cmd("/exit", "leave the shell", args="", aliases=("/quit",)),   # "" handler → inline exit
]


def _slash_tree() -> dict:
    """The NestedCompleter tree derived from `_COMMANDS`: each name (and alias) maps to a sub-map of
    its sub-commands or None."""
    tree = {}
    for c in _COMMANDS:
        node = {s: None for s in c.subs} if c.subs else None
        for nm in c.names():
            tree[nm] = node
    return tree


# Commands with no handler are handled inline by dispatch (just /exit + its /quit alias → leave).
_EXIT_CMDS = frozenset(nm for c in _COMMANDS if not c.handler for nm in c.names())
_SLASH = _slash_tree()


def _slash_completer(dynamic=None):
    """A completer that makes `/` self-documenting AND completes argument values. At the top level it
    lists every slash command with its one-line description (from `_COMMANDS`). Past the command word
    it completes live values when the context has a `dynamic` provider (e.g. `/model <role>`,
    `/skill <name>`, `/services start <svc>`, `/session resume <ref>`), otherwise it defers to the
    static `_SLASH` sub-command tree. `dynamic` maps a token tuple (the completed words, e.g.
    `("/services", "start")`) to a zero-arg callable returning candidate strings. Wrapped in
    FuzzyCompleter by the caller — which strips the word being typed, so each branch yields the FULL
    candidate set for its context and lets the fuzzy layer filter. prompt_toolkit is imported lazily so
    shell.py stays importable without it."""
    from prompt_toolkit.completion import Completer, Completion, NestedCompleter

    dynamic = dynamic or {}
    meta = {nm: c.desc for c in _COMMANDS for nm in c.names()}   # name + aliases → its description
    nested = NestedCompleter.from_nested_dict(_SLASH)

    class _SlashCompleter(Completer):
        def get_completions(self, document, complete_event):
            stripped = document.text_before_cursor.lstrip()
            if " " not in stripped:                     # completing the command word itself (+ meta)
                for name, desc in meta.items():
                    if name.startswith(stripped):
                        yield Completion(name, start_position=-len(stripped), display_meta=desc)
                return
            provider = dynamic.get(tuple(stripped.split()))    # completed words → live value provider
            if provider is not None:
                for val in provider():
                    yield Completion(val, start_position=0)
                return
            yield from nested.get_completions(document, complete_event)   # static sub-commands

    return _SlashCompleter()

# A tool result is an error/refusal (colour it ✗, not ✓) when it starts with one of these markers —
# both the dispatcher's own errors and the tools' in-band refusals (file sandbox, approval denial).
_ERR_MARKERS = (
    "tool error", "unknown tool", "bad arguments", "access denied", "not found", "not a directory",
    "was denied", "no allowedreadpaths", "is disabled", "error reading", "error writing",
    "error listing", "file_read:", "file_write:", "file_list:",
)


def is_interactive() -> bool:
    """The shell launches only when BOTH ends are a real terminal. A pipe/redirect/CI on
    either stdin or stdout means a script, which must get help, never the REPL."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def _force_utf8() -> None:
    """Best-effort: make stdout/stderr UTF-8 so the glyphs and Markdown never hit a cp1252 encode error
    on a legacy Windows console. `errors='replace'` degrades an unexpected char to '?' instead of
    crashing. No-op where already UTF-8 (POSIX, Windows Terminal)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def _is_error_result(res: str) -> bool:
    head = (res or "").lstrip().lower()[:64]
    return any(head.startswith(m) or m in head for m in _ERR_MARKERS)


def _compact_args(arguments: str) -> str:
    """A one-line, human-readable rendering of a tool call's JSON arguments (key=value, value clipped)."""
    s = (arguments or "").strip()
    try:
        data = json.loads(s)
    except Exception:
        return (s[:80] + "...") if len(s) > 80 else s
    if isinstance(data, dict):
        parts = []
        for k, v in data.items():
            vs = str(v).replace("\n", " ")
            parts.append(f"{k}={vs[:48] + '...' if len(vs) > 48 else vs}")
        return ", ".join(parts)
    return (s[:80] + "...") if len(s) > 80 else s


def _derive_session_name(text: str, limit: int = 48) -> str:
    """A short, human-readable session name from its first message: whitespace collapsed to one line,
    clipped to `limit`. Used to auto-name a session the moment it gets its first turn."""
    line = " ".join((text or "").split())
    return (line[:limit].rstrip() + "…") if len(line) > limit else line


def _looks_like_diff(res: str) -> bool:
    """Conservative unified-diff detection: a hunk header (`@@ -`) or the `--- `/`+++ ` file-header
    pair. Anchored to those markers so ordinary tool output (which may start with a stray + or -) is
    never mistaken for a diff and mis-rendered."""
    head = (res or "").lstrip()
    if not head:
        return False
    lines = head.splitlines()
    if lines[0].startswith("@@ -"):
        return True
    return len(lines) >= 2 and lines[0].startswith("--- ") and lines[1].startswith("+++ ")


def _preview(res: str, n: int = 240) -> str:
    """First non-empty line of a tool result, clipped — enough to see what happened, not a data dump."""
    text = (res or "").strip()
    if not text:
        return "(no output)"
    first = text.splitlines()[0]
    more = len(text) > len(first)
    return first[:n] + ("..." if len(first) > n or more else "")


class _TurnRenderer:
    """Renders one agent turn's event stream as a scrolling transcript. Assistant text streams as live
    Markdown (tables/code/lists render, not raw) via one rich.Live per text segment; tool calls appear
    as their own blocks with a spinner while the tool runs, and a ✓/✗ line on the result. On a
    non-terminal console (tests, pipes) it degrades to plain `console.print` — no Live, no spinner — so
    output is still captured. Only ever one rich.Live active at a time (a text segment XOR a spinner),
    which rich requires. Reads the pre-parsed `Theme`; does no config parsing per turn."""

    def __init__(self, console, agency: str, theme: Theme):
        self.console = console
        self.agency = agency
        self.term = console.is_terminal
        self.t = theme
        self.streamed = False
        self.answer = ""
        self._buf = ""
        self._live = None
        self._status = None
        self._last_update = 0.0   # monotonic time of the last live re-parse (throttle to _STREAM_HZ)
        self._spin_label = ""     # base label of the active spinner (for the live elapsed timer)
        self._spin_start = 0.0

    # -- streamed assistant text (Markdown) --------------------------------

    def begin(self) -> None:
        """Show a 'thinking' spinner immediately, before the first token/tool — no dead air while the
        model works. The first handled event stops it (every handler calls _stop_spin first)."""
        self._start_spin("thinking")

    def _renderable(self, text: str):
        if not self.t.markdown:
            from rich.text import Text
            return Text(text)
        from rich.markdown import Markdown
        md = Markdown(text)
        w = self.t.prose_width
        if w and 0 < w < (self.console.width or 0):    # cap prose width for readability on wide terms
            from rich.align import Align
            return Align.left(md, width=w)
        return md

    def _start_live(self) -> None:
        if self._live is None and self.term and self._status is None:
            from rich.live import Live
            self._live = Live(self._renderable(self._buf), console=self.console,
                              refresh_per_second=_STREAM_HZ, vertical_overflow="visible")
            self._live.start()
            self._last_update = time.monotonic()

    def _flush_text(self) -> None:
        """Commit the current streamed text segment (rendered), then reset the buffer."""
        if self._buf.strip():
            self.answer = self._buf
            if self._live is not None:
                self._live.update(self._renderable(self._buf))
                self._live.stop()
            else:
                self.console.print(self._renderable(self._buf))
        elif self._live is not None:
            self._live.stop()
        self._live = None
        self._buf = ""

    # -- spinner (tool running) --------------------------------------------

    def _start_spin(self, label: str) -> None:
        if self.term and self._status is None and self._live is None:
            self._spin_label = label
            self._spin_start = time.monotonic()
            self._status = self.console.status(f"[{self.t.muted}]{label}[/]", spinner=self.t.spinner)
            self._status.start()

    def _stop_spin(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None
        self._spin_start = 0.0

    def tick(self) -> None:
        """Refresh the active spinner's label with elapsed seconds, so a long wait shows a live timer
        ('thinking · 7s') instead of a static word. Called from the consume loop's idle poll; a no-op
        under a second, while streaming text, or on a non-terminal console."""
        if self._status is not None and self._spin_start:
            secs = int(time.monotonic() - self._spin_start)
            if secs >= 1:
                self._status.update(f"[{self.t.muted}]{self._spin_label} · {secs}s[/]")

    # -- event handlers ----------------------------------------------------

    def handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "token":
            self._stop_spin()
            self.streamed = True
            self._buf += ev.get("text", "")
            if self.term:
                self._start_live()
                # Re-parse at most _STREAM_HZ times/second (not per token) so a long answer stays
                # ~linear, not O(n^2). Live keeps redrawing the last renderable between updates, and
                # _flush_text renders the complete buffer when the segment ends, so nothing is lost.
                now = time.monotonic()
                if self._live is not None and (now - self._last_update) >= 1.0 / _STREAM_HZ:
                    self._live.update(self._renderable(self._buf))
                    self._last_update = now
        elif t == "tool_call":
            self._stop_spin()
            self._flush_text()
            if self.agency != "silent":
                name = ev.get("name", "?")
                # Tool activity is indented so it reads as subordinate to the assistant's answer (which
                # stays flush-left as the turn's primary output).
                self.console.print(f"  [{self.t.tool}]{self.t.gear}[/] [bold]{name}[/]"
                                   f"[{self.t.muted}]({_compact_args(ev.get('arguments'))})[/]")
                self._start_spin(f"running {name}...")
        elif t == "tool_result":
            self._stop_spin()
            if self.agency != "silent":
                res = ev.get("result") or ""
                if _is_error_result(res):
                    self.console.print(f"    [{self.t.error}]{self.t.bad}[/] [{self.t.error}]{_preview(res)}[/]")
                elif _looks_like_diff(res):
                    from rich.padding import Padding
                    from bob import render
                    self.console.print(f"    [{self.t.success}]{self.t.ok}[/] [{self.t.muted}]diff[/]")
                    self.console.print(Padding(render.diff_view(res, self.t), (0, 0, 0, 4)))
                else:
                    self.console.print(f"    [{self.t.success}]{self.t.ok}[/] [{self.t.muted}]{_preview(res)}[/]")
        elif t == "error":
            self._stop_spin()
            self._flush_text()
            from rich.panel import Panel
            self.console.print(Panel(ev.get("message", ""), border_style=self.t.error,
                                     title="error", title_align="left", expand=False))
        elif t == "final":
            self._stop_spin()
            self._flush_text()
            if ev.get("reason") == "max_steps" and ev.get("result") is None:
                self.console.print(f"[{self.t.warn}](stopped after max steps without a final answer)[/]")

    def quiesce(self) -> None:
        """Stop any live display so an approval prompt (prompt_toolkit) can own the terminal cleanly."""
        self._stop_spin()
        self._flush_text()

    def close(self) -> None:
        self._stop_spin()
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None


class BobShell:
    def __init__(self, config, tools, skills, console=None, sessions=None, role=None,
                 no_tools=False):
        self.config = config
        self.tools = tools
        self.skills = skills
        # `bob chat/code/think` launch the shell in chat mode: a preset role + no_tools (a plain
        # conversation). Default (bare `bob`/`bob shell`) keeps the agent role + full toolset.
        self.role = role or config.get("routing", {}).get("agentRole", "chat")
        self.no_tools = no_tools
        self.agency = config.get("agent", {}).get("agency", "show")
        # Owner-scoped persisted sessions. The row is created LAZILY on the first turn
        # (session_id stays None until then) so opening `bob` and leaving leaves no empty session.
        # `sessions` is a SessionStore (injected in build()); None in unit tests unless supplied.
        self.owner = config.get("agent", {}).get("defaultOwner", "local")
        self._max_tokens = int(config.get("agent", {}).get("maxSessionTokens", 0) or 0)
        # The project this shell was launched in (git root / cwd, or None if scopeByProject
        # off). Threaded into each turn so project-type memory is scoped to this repo.
        try:
            from bob_core import project_key
            self.scope = project_key(config=config)
        except Exception:
            self.scope = None
        self.sessions = sessions
        self.session_id = None           # persisted id once created; None = no row yet
        self.history: list = []          # [{role, content}] — the live context; mirrors the store
        self._always: set = set()        # tools the user chose "always" for this session
        # Two-stage exit: a Ctrl-C at the prompt (or one that cancels a turn) arms this; a second
        # consecutive Ctrl-C at the prompt leaves. Any dispatched line clears it.
        self._pending_exit = False
        # A session name set (via /session name) BEFORE the first turn, applied when the row is
        # created lazily. None once applied / for auto-naming from the first message.
        self._pending_name = None
        # Runtime theme-preset override from /theme <preset>; None = use config/ui.json's choice.
        self._theme_preset = None
        from rich.console import Console
        # highlight=False: only the theme's colours apply — rich's ReprHighlighter must not tint
        # identifiers/numbers (e.g. a magenta tool name) and fight the palette. no_color honours the
        # NO_COLOR convention (strips every hue; the transcript still reads via weight + glyphs).
        self.console = console or Console(highlight=False, no_color=theme_mod.no_color_active())
        self.theme = Theme.load(config, self.console)   # parsed once; renderer/splash read its fields

    # -- construction ---------------------------------------------------------

    @classmethod
    def build(cls, config=None, role=None, no_tools=False):
        """Build the shell with warm registries (one tool build, one skill build)."""
        from bob_core import load_config
        from bob_session import SessionStore
        from bob_skills import SkillRegistry
        from tool_registry import ToolRegistry

        config = config or load_config()
        agent_cfg = config.get("agent", {})
        disabled_raw = agent_cfg.get("disabledTools", [])
        disabled = ({t.strip() for t in disabled_raw.split(",") if t.strip()}
                    if isinstance(disabled_raw, str) else set(disabled_raw))
        tools = ToolRegistry.build(config, disabled, quiet=True)   # clean splash — no startup summary
        skills = SkillRegistry.build()
        # Same SessionStore the agent server uses (agent.sessionDbPath, resolved against the
        # repo root; _SCRIPTS is scripts/, its parent is the repo), so a session persists across
        # restarts and is resumable from either surface.
        session_db = _SCRIPTS.parent / agent_cfg.get("sessionDbPath", "data/sessions.db")
        sessions = SessionStore(session_db, default_owner=agent_cfg.get("defaultOwner", "local"))
        return cls(config, tools, skills, sessions=sessions, role=role, no_tools=no_tools)

    # -- splash ---------------------------------------------------------------

    def splash(self) -> str:
        """Plain-text splash (no rich) — the fallback/testable form of the header info."""
        from bob_core import check_litellm

        counts = catalog.counts(self.tools, self.skills)
        endpoint = "endpoint ready" if check_litellm(self.config) else "endpoint DOWN, run: bob up"
        return "\n".join([
            "Bob, local AI assistant",
            f"model: {self.role}   agency: {self.agency}   session: {self._sid_label()}",
            f"{counts}   {endpoint}",
            "",
            "Type a message to chat, /agent <goal> to run the agent, /help for commands, /exit to quit.",
        ])

    def _print_splash(self) -> None:
        from bob import registry
        from bob_core import check_litellm
        from rich.panel import Panel
        from rich.rule import Rule
        from rich.text import Text

        t = self.theme
        theme_mod.render_header(t, self.console)
        for _ in range(t.header_margin):
            self.console.print()

        justify = "center" if t.centered else "left"
        dim, sep = t.muted, "  ·  "

        if t.tagline:
            self.console.print(Text(t.tagline, style=f"italic {t.accent}"), justify=justify)
            self.console.print()

        reachable = check_litellm(self.config)
        # Line 1 — health pill + identity: ● ready · agent · show · session xxxx
        line1 = Text(justify=justify)
        line1.append(f"{t.dot} ", style=(t.success if reachable else t.error))
        line1.append("ready" if reachable else "offline", style=(t.success if reachable else t.error))
        line1.append(sep, style=dim)
        line1.append(self.role, style=f"bold {t.accent}")
        line1.append(sep, style=dim)
        line1.append(self.agency, style=dim)
        line1.append(sep, style=dim)
        line1.append("session ", style=dim)
        line1.append(self._sid_label(), style=dim)

        # Line 2 — capability counts with the numbers in accent, labels dim.
        counts = [
            (len(getattr(self.tools, "_loaded_names", []) or []), "tools"),
            (len(registry.commands(include_hidden=False)), "commands"),
            (len(self.skills.list()) if hasattr(self.skills, "list") else 0, "skills"),
        ]
        line2 = Text(justify=justify)
        for i, (n, label) in enumerate(counts):
            if i:
                line2.append(sep, style=dim)
            line2.append(str(n), style=f"bold {t.accent}")
            line2.append(f" {label}", style=dim)

        if t.meta_panel:
            body = Text(justify=justify)
            body.append_text(line1); body.append("\n"); body.append_text(line2)
            self.console.print(Panel(body, border_style=(t.accent if reachable else t.error),
                                     expand=False, padding=t.panel_padding))
        else:
            self.console.print(line1, justify=justify)
            self.console.print(line2, justify=justify)

        if not reachable:
            self.console.print(Text("run  bob up  to start the local endpoint", style=t.warn),
                               justify=justify)
        if t.rule:
            self.console.print(Rule(style=t.accent))

        # Tip — "type to chat" dim, the slash-verbs in accent so they read as actionable. The most
        # useful entry points for a newcomer: run a task, switch model, discover everything, leave.
        tip = Text(justify=justify)
        tip.append("type to chat", style=f"italic {dim}")
        for verb in ("/agent", "/model", "/help", "/exit"):
            tip.append(sep, style=dim)
            tip.append(verb, style=t.accent)
        self.console.print(tip, justify=justify)

    # -- REPL -----------------------------------------------------------------

    def run(self) -> int:
        """The interactive loop. Refuses (help) when not on a TTY so scripts/CI never enter it."""
        if not is_interactive():
            from bob.cli import _print_help
            _print_help()
            return 0

        from prompt_toolkit import PromptSession
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        import osenv

        self._print_splash()
        if self._first_run_pending():
            self._print_first_run()
        if self.theme.input_multiline:
            self.console.print(f"[{self.theme.muted}]multiline input on: Enter for a new line, "
                               f"Meta (or Esc) then Enter to send[/]")
        style = Style.from_dict(self.theme.prompt_style)
        message = HTML(f"<prompt>{self.theme.prompt}</prompt> <arrow>{self.theme.arrow}</arrow> ")

        def toolbar():
            ntools = len(getattr(self.tools, "_loaded_names", []) or [])
            nskills = len(self.skills.list()) if hasattr(self.skills, "list") else 0
            parts = [f"<b>{self.role}</b>", self.agency, self._sid_label(),
                     f"{ntools} tools", f"{nskills} skills"]
            ctx = self._context_label()
            if ctx:
                parts.append(ctx)
            return HTML(" " + " · ".join(parts) + "    ^C cancel · /help · /exit ")

        session = PromptSession(
            history=FileHistory(str(osenv.data_dir() / "shell-history.txt")),
            style=style,
            bottom_toolbar=toolbar,
            **self._session_kwargs(),
        )
        self._session = session   # so /theme can recolour the live prompt, not just the transcript
        while True:
            try:
                with patch_stdout():
                    line = session.prompt(message)
            except KeyboardInterrupt:      # Ctrl-C at the prompt: first press arms exit, second leaves
                if self._on_prompt_interrupt():
                    break
                continue
            except EOFError:               # Ctrl-D: leave cleanly
                break
            # A command that does blocking work (voice playback, /up, …) must never crash the shell on
            # Ctrl-C — catch it here so any interrupted command just returns to the prompt.
            try:
                if not self.dispatch(line):
                    break
            except KeyboardInterrupt:
                self.console.print(f"[{self.theme.muted}]cancelled[/]")
        self._on_exit()
        return 0

    def _session_kwargs(self) -> dict:
        """The prompt_toolkit input options this shell configures: fuzzy slash completion (the menu
        opens and live-filters on '/'), fish-style history ghost-text, colour depth aligned to Rich's,
        and optional multiline input. Split out of run() so it's inspectable without a live TTY."""
        from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
        from prompt_toolkit.completion import FuzzyCompleter
        from prompt_toolkit.output import ColorDepth
        kw = {
            "completer": FuzzyCompleter(_slash_completer(self._completion_providers())),
            # '/' opens + live-filters the menu; a normal message matches no key, so no menu appears.
            "complete_while_typing": True,
            "auto_suggest": AutoSuggestFromHistory(),
            # Match Rich's colour fidelity, or 1-bit (monochrome) when NO_COLOR is in effect.
            "color_depth": (ColorDepth.DEPTH_1_BIT if self.theme.no_color
                            else theme_mod.ptk_color_depth(self.console)),
        }
        if self.theme.input_multiline:
            # Enter inserts a newline; Meta/Esc+Enter submits — for pasting code / long prompts.
            kw["multiline"] = True
            kw["prompt_continuation"] = self._continuation
        return kw

    def _continuation(self, width, line_number, is_soft_wrap):
        """The dim gutter drawn on each continued line of a multiline prompt: an ellipsis right-aligned
        under the prompt so the input column stays visually anchored."""
        from prompt_toolkit.formatted_text import HTML
        return HTML("<continuation>%s</continuation>") % ("… ".rjust(max(width, 2)))

    def _completion_providers(self) -> dict:
        """Live argument-value completers, keyed by the completed-token tuple the user has typed:
        `/model <role>`, `/skill <name>`, `/services start|stop <svc>`, `/session resume <ref>`. Each
        value is a zero-arg callable queried at completion time so it always reflects current state."""
        return {
            ("/model",): self._known_roles,
            ("/skill",): lambda: [s["name"] for s in self.skills.list()],
            ("/services", "start"): self._service_names,
            ("/services", "stop"): self._service_names,
            ("/session", "resume"): self._session_refs,
            ("/session", "delete"): lambda: ["all"] + self._session_refs(),
            ("/theme",): lambda: theme_mod.preset_names() + ["reload"],
        }

    def _service_names(self) -> list:
        """Service names (and distinct labels) for /services completion, from the SERVICES registry."""
        import stack
        out = []
        for s in stack.SERVICES:
            out.append(s["name"])
            if s.get("label") and s["label"] != s["name"]:
                out.append(s["label"])
        return out

    def _session_refs(self) -> list:
        """Resumable references for /session completion: each owned session's name (if any) + its
        short id, newest first — so you can resume by a readable name or a short id."""
        if self.sessions is None:
            return []
        refs = []
        for sid in self.sessions.list_owned(self.owner):
            s = self.sessions.get(sid)
            if not s:
                continue
            if s.get("name"):
                refs.append(s["name"])
            refs.append(sid[:8])
        return refs

    def _on_prompt_interrupt(self) -> bool:
        """Ctrl-C at the prompt. The first press arms exit (and says so); a second consecutive press
        confirms it. Returns True when the shell should leave. Any dispatched line clears the arm, so a
        lone Ctrl-C never exits by surprise."""
        if self._pending_exit:
            return True
        self._pending_exit = True
        self.console.print(f"[{self.theme.muted}]press Ctrl-C again to exit[/]")
        return False

    def dispatch(self, line: str) -> bool:
        """Route one input line. Returns False to exit the REPL, True to keep looping. A leading '/'
        is a shell command; anything else is an agent turn."""
        self._pending_exit = False        # the user acted → disarm any pending two-stage exit
        line = (line or "").strip()
        if not line:
            return True
        if not line.startswith("/"):
            self._run_turn(line)
            return True

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in _EXIT_CMDS:
            return False
        handler = self._handlers().get(cmd)
        if handler is None:
            self.console.print(f"[yellow]Unknown command: {cmd}[/]  (try /help)")
            return True
        # A handler may return False to leave the REPL (e.g. /reset, after wiping). Everything else
        # (None, or a turn's answer string) keeps looping.
        return handler(arg) is not False

    def _handlers(self) -> dict:
        """The cmd → bound-method table, derived from `_COMMANDS` (name + aliases → its handler)."""
        table = {}
        for c in _COMMANDS:
            if not c.handler:
                continue
            fn = getattr(self, c.handler)
            for nm in c.names():
                table[nm] = fn
        return table

    # -- slash commands -------------------------------------------------------

    def _cmd_help(self, _arg: str = "") -> None:
        from rich.table import Table
        t = self.theme
        tbl = Table(show_header=False, box=None, pad_edge=False)
        tbl.add_column(style=t.accent, no_wrap=True)
        tbl.add_column(style=t.muted)
        # Lead with the primary affordance (typing a message), then every command from the one source.
        tbl.add_row("(type a message)", "chat with Bob, or describe a task to run")
        for c in _COMMANDS:
            tbl.add_row(f"{c.name} {c.args}".strip(), c.desc)
        self.console.print(tbl)
        self.console.print(
            f"\n[italic {self.theme.muted}]These [bold]/[/]commands drive the shell; type anything "
            f"else to ask Bob, which can run any task for you. Catalogs: [bold]/tools[/] · "
            f"[bold]/skills[/]. For scripting from a terminal, [bold]bob help[/] is the CLI reference.[/]"
        )

    def _cmd_tools(self, _arg: str = "") -> None:
        from bob import render
        self.console.print(render.tools_view(self.tools, self.theme))

    def _cmd_skills(self, _arg: str = "") -> None:
        from bob import render
        self.console.print(render.skills_view(self.skills, self.theme))

    def _cmd_status(self, _arg: str = "") -> None:
        from bob_core import check_litellm
        from rich.table import Table

        reachable = check_litellm(self.config)
        t = self.theme
        tbl = Table(show_header=False, box=None, pad_edge=False)
        tbl.add_column(style=t.muted, justify="right")
        tbl.add_column()
        tbl.add_row("model", f"[{t.accent}]{self.role}[/]")
        tbl.add_row("agency", self.agency)
        tbl.add_row("session", self._sid_label())
        tbl.add_row("turns", str(len(self.history)))
        tbl.add_row("catalog", catalog.counts(self.tools, self.skills))
        tbl.add_row("endpoint",
                    f"[{t.success}]ready[/]" if reachable else f"[{t.error}]DOWN[/] (run: bob up)")
        self.console.print(tbl)
        # The whole system in one glance — so services (WebUI, SearXNG, n8n, Langfuse, …) aren't a
        # separate mystery from the assistant.
        self._render_dashboard()

    def _render_dashboard(self) -> None:
        """The cockpit dashboard: every service, grouped, with state legible WITHOUT relying on colour —
        a filled ● (green) = running, a hollow ○ (red) = down. Reads the one stack.service_snapshot
        (same data as `bob status`). The last column shows what the service IS when it's up, and HOW to
        start it when it's down — so a running row identifies itself and a down row is actionable. No URL
        column (the port is right there). `/services` shows this; `/status` appends it."""
        import stack
        from rich.table import Table
        t = self.theme
        tbl = Table(show_header=False, box=None, pad_edge=False)
        tbl.add_column(no_wrap=True)                       # status glyph
        tbl.add_column(style=t.accent, no_wrap=True)       # service
        tbl.add_column(style=t.muted, no_wrap=True, justify="right")  # port
        tbl.add_column()                                   # desc (up) / start hint (down)
        seen_group = None
        for r in stack.service_snapshot(self.config):
            if r["group"] != seen_group:
                tbl.add_row("", f"[bold {t.accent}]{r['group']}[/]", "", "")
                seen_group = r["group"]
            if r["up"]:
                glyph, detail = f"[{t.success}]●[/]", f"[{t.muted}]{r['desc']}[/]"
            else:
                glyph, detail = f"[{t.error}]○[/]", f"[{t.warn}]start: {self._tui_start_hint(r)}[/]"
            tbl.add_row(glyph, f"  {r['label']}", f":{r['port']}", detail)
        self.console.print(tbl)
        self.console.print(f"[{t.muted}]● running   ○ down · toggle from here: /services start|stop <name>[/]")

    def _tui_start_hint(self, r: dict) -> str:
        """The IN-TUI command that starts a down service — the cockpit acts from inside, not via raw
        `bob` verbs. (The plain-text `bob status` keeps CLI verbs; that's the CLI context.) Only the
        agent HTTP server is genuinely external — it's a separate long-running server, not a toggle."""
        import stack
        if r["core"] or r["name"] == "open-webui":
            return "/up"                                  # inference + WebUI come up together
        if r["name"] in stack._DAEMON_CONTROL or r["docker"]:
            return f"/services start {r['name']}"         # whisper/piper + the Docker services
        if r["name"] == "agent-api":
            return "bob agent serve   (separate server)"
        return "/up"

    def _cmd_services(self, arg: str = "") -> None:
        """/services — the cockpit dashboard (every service, up/down). /services start|stop [name]
        toggles one; with no name, the Docker services (SearXNG/n8n/Langfuse) as a group. After a
        toggle the dashboard re-renders, so the changed row flips colour — the visual-feedback loop."""
        parts = arg.split()
        if not parts:
            self._render_dashboard()
            return
        action = parts[0].lower()
        if action not in ("start", "stop"):
            self.console.print("[yellow]usage: /services [start|stop [name]][/]  (no name = Docker group)")
            return
        name = parts[1].lower() if len(parts) > 1 else None
        self.console.print(self._toggle_service(action, name))
        self._render_dashboard()      # visual feedback — the toggled row flips ●

    def _toggle_service(self, action: str, name) -> str:
        """Route a /services toggle to the right lifecycle op, derived from the SERVICES registry (no
        hardcoded service sets): daemons → service_control; Docker (or no name) → the compose group;
        core inference / WebUI / agent-api aren't single-service toggles here (dedicated commands)."""
        import stack
        by = {s["name"]: s for s in stack.SERVICES}
        label_to_name = {s.get("label", s["name"]): s["name"] for s in stack.SERVICES}
        docker = {s["name"] for s in stack.SERVICES if s.get("docker")}
        daemons = set(stack._DAEMON_CONTROL)
        if name is None:
            return stack.services_control(self.config, action)    # Docker group (the default target)
        canonical = name if name in by else label_to_name.get(name)
        if canonical is None:
            return f"unknown service '{name}' (see /services)"
        if canonical in daemons:
            return stack.service_control(self.config, canonical, action)
        if canonical in docker:
            return stack.services_control(self.config, action, service=canonical)  # just this container
        return (f"'{canonical}' can't be toggled individually here. Use "
                f"/up, /restart, /stop (inference/WebUI), or `bob agent serve` (agent-api).")

    def _cmd_up(self, arg: str = "") -> None:
        """/up [--with-services] [--no-open] — bring the stack up in the background (endpoint + proxy +
        WebUI) without leaving the shell. The cockpit: manage the system from here, not raw `bob` verbs."""
        import stack
        toks = arg.split()
        with_services = "--with-services" in toks or "services" in toks
        open_browser = "--no-open" not in toks
        self.console.print(stack.stack_up(self.config, open_browser=open_browser,
                                          with_services=with_services))
        self._render_dashboard()      # visual feedback — the rows that came up flip green

    def _cmd_restart(self, _arg: str = "") -> None:
        """/restart — bounce the inference endpoint + proxy (+ WebUI) and wait for ready."""
        import stack
        self.console.print(stack.stack_restart(self.config))
        self._render_dashboard()

    def _cmd_webui(self, _arg: str = "") -> None:
        """/webui — open the Open WebUI browser tab if it's running; else point at how to start it.
        (The foreground `bob webui` blocks a terminal, so the shell never launches it inline — /up or
        /services start webui bring it up in the background.)"""
        import osenv
        import stack   # noqa: F401 — keeps scripts/tools import parity with the other cockpit cmds
        from bob_core import _port
        port = _port(self.config, "webuiPort")
        if osenv.is_port_in_use(port):
            osenv.open_url(f"http://localhost:{port}")
            self.console.print(f"[{self.theme.muted}]opening http://localhost:{port}[/]")
        else:
            self.console.print("Open WebUI isn't running. [bold]/up[/] starts it in the background "
                               "(or [bold]/services start webui[/]).")

    def _cmd_stop(self, _arg: str = "") -> None:
        """/stop — tear down local inference (frees VRAM) without leaving the shell. Auto-start brings
        it back on your next turn."""
        import stack   # scripts/tools is on sys.path (module top)
        self.console.print(stack.stack_stop(self.config))
        self._render_dashboard()      # visual feedback — everything flips to down (VRAM freed)

    def _cmd_logs(self, arg: str = "") -> None:
        """/logs [N] — a bounded tail of the inference-server log (no follow; the shell owns the TTY)."""
        import stack
        n = int(arg) if arg.strip().isdigit() else 40
        self.console.print(stack.stack_logs(self.config, n))

    def _known_roles(self) -> list:
        """The model/role names this endpoint is configured for — the VALUES in routing + vision (a
        role is sent verbatim as the model name, [bob_loop] `_single_turn`). Sorted, de-duped. Used to
        validate /model and to complete it. Empty when config carries no routing (tests) → no gating."""
        routing = self.config.get("routing", {})
        vision = self.config.get("vision", {})
        names = {v for v in list(routing.values()) + list(vision.values()) if isinstance(v, str)}
        return sorted(names)

    def _cmd_model(self, arg: str) -> None:
        if not arg:
            self.console.print(f"model/role: {self.role}")
            return
        self.role = arg.strip()
        known = self._known_roles()
        if known and self.role not in known:
            # Not a configured role — the turn would fail at the model call. Warn (don't silently
            # accept) and point at the valid set, but still switch so a custom LiteLLM model isn't blocked.
            self.console.print(f"[{self.theme.warn}]role → {self.role}[/]  "
                               f"[{self.theme.muted}](not a known role: {', '.join(known)})[/]")
        else:
            self.console.print(f"[green]role → {self.role}[/]")

    def _cmd_agency(self, arg: str) -> None:
        arg = arg.strip().lower()
        if not arg:
            self.console.print(f"agency: {self.agency}  (show|confirm|silent)")
            return
        if arg not in ("show", "confirm", "silent"):
            self.console.print("[yellow]agency must be one of: show | confirm | silent[/]")
            return
        self.agency = arg
        self.console.print(f"[green]agency → {self.agency}[/]")

    def _sid_label(self) -> str:
        """Short display form of the current session (`new` before the first turn creates a row)."""
        return self.session_id[:8] if self.session_id else "new"

    def _context_label(self) -> str:
        """A compact context-window usage label for the toolbar: the estimated token count of the live
        history, plus a percentage when a session token budget is configured — so how full the window
        is stays visible. Empty string if the estimator is unavailable."""
        try:
            from bob_loop import _estimate_tokens
            used = sum(_estimate_tokens(m.get("content", "") or "") for m in self.history)
        except Exception:
            return ""
        if self._max_tokens:
            return f"~{used}/{self._max_tokens} tok ({int(100 * used / self._max_tokens)}%)"
        return f"~{used} tok"

    def _cmd_session(self, arg: str) -> None:
        """/session new | list | resume <ref> | name <text> | show [ref] — owner-scoped persisted
        sessions. `ref` is an id, an 8-char prefix, or a session name."""
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "new":
            self._on_session_end(self.session_id)   # consolidate the one we're leaving (memory fills)
            self.session_id = None                  # lazy — the row is created on the next message
            self.history = []
            self._pending_name = None
            self.console.print("[green]new session[/] [dim](starts on your next message)[/]")
        elif sub == "list":
            self._session_list()
        elif sub == "resume":
            self._session_resume(rest)
        elif sub == "name":
            self._session_name(rest)
        elif sub == "delete":
            self._session_delete(rest)
        elif sub in ("show", ""):
            self._session_show(rest)
        else:
            self.console.print(
                f"[yellow]/session {sub}?[/]  "
                f"(new | list | resume <ref> | name <text> | delete <ref> | show [ref])")

    def _session_delete(self, ref: str) -> None:
        """/session delete <ref> | all — remove one owned session (by id/prefix/name) or, with 'all',
        every session for this owner (confirmed). Dropping the active session returns to a fresh one."""
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        ref = ref.strip()
        if not ref:
            self.console.print("[yellow]usage: /session delete <ref> | all[/]")
            return
        if ref.lower() == "all":
            ids = self.sessions.list_owned(self.owner)
            if not ids:
                self.console.print("[dim]no sessions to delete[/]")
                return
            if not self._confirm(f"Delete ALL {len(ids)} session(s)? This can't be undone."):
                self.console.print("[dim]cancelled[/]")
                return
            n = self.sessions.delete_all_owned(self.owner)
            self.session_id = None            # the active one is gone → back to a pending session
            self.history = []
            self.console.print(f"[green]deleted {n} session(s)[/]")
            return
        target = self._match_session_id(ref)
        if target is None or not self.sessions.delete_owned(target, self.owner):
            self.console.print(f"[yellow]no such session for this owner: {ref}[/]")
            return
        if target == self.session_id:
            self.session_id = None
            self.history = []
        self.console.print(f"[green]deleted[/] {target[:8]}")

    def _session_name(self, name: str) -> None:
        """/session name <text> — rename the current session. Before the first turn there is no row
        yet, so the name is queued and applied when the session is created."""
        name = name.strip()
        if not name:
            self.console.print("[yellow]usage: /session name <text>[/]")
            return
        if self.session_id is None:
            self._pending_name = name
            self.console.print(f"[green]name set[/] [dim](applies when this session starts)[/]")
            return
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        self.sessions.set_name_owned(self.session_id, self.owner, name)
        self.console.print(f"[green]renamed[/] {self._sid_label()} → {name}")

    def _match_session_id(self, ref: str):
        """Resolve a session reference within the current owner's sessions: a full id, an unambiguous
        8-char id prefix, an exact (case-insensitive) name, or a unique name substring. Returns the
        full id, or None if unknown/ambiguous."""
        if self.sessions is None or not ref:
            return None
        ids = self.sessions.list_owned(self.owner)
        if ref in ids:
            return ref
        id_pref = [i for i in ids if i.startswith(ref)]
        if len(id_pref) == 1:
            return id_pref[0]
        low = ref.lower()
        named = [(i, (self.sessions.get(i) or {}).get("name") or "") for i in ids]
        exact = [i for i, nm in named if nm.lower() == low]
        if len(exact) == 1:
            return exact[0]
        subs = [i for i, nm in named if low in nm.lower()]
        return subs[0] if len(subs) == 1 else None

    def _session_list(self) -> None:
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        ids = self.sessions.list_owned(self.owner)
        if not ids:
            self.console.print("[dim]no saved sessions yet[/]")
            return
        from rich.table import Table
        from rich.text import Text
        t = self.theme
        tbl = Table(show_header=True, header_style=t.accent, box=None, pad_edge=False)
        tbl.add_column("id", style=t.muted)
        tbl.add_column("name")
        tbl.add_column("updated", style=t.muted)
        tbl.add_column("turns", justify="right")
        tbl.add_column("first message", style=t.muted)
        for sid in ids[:20]:
            s = self.sessions.get(sid)
            if not s:
                continue
            users = [m for m in s["history"] if m.get("role") == "user"]
            first = users[0]["content"] if users else ""
            # names are user text — pass as Text so a stray '[' can't be read as rich markup.
            name = Text(s.get("name")) if s.get("name") else Text("(unnamed)", style=t.muted)
            marker = "→ " if sid == self.session_id else "  "
            tbl.add_row(marker + sid[:8], name, (s["updated_at"] or "")[:19], str(len(users)),
                        (first[:40] + "…") if len(first) > 40 else first)
        self.console.print(tbl)

    def _session_resume(self, ref: str) -> None:
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        if not ref:
            self.console.print("[yellow]usage: /session resume <id>[/]  (see /session list)")
            return
        target = self._match_session_id(ref)
        s = self.sessions.get_owned(target, self.owner) if target else None
        if s is None:
            self.console.print(f"[yellow]no such session for this owner: {ref}[/]")
            return
        self._on_session_end(self.session_id)   # consolidate the one we're leaving (memory fills)
        self.session_id = s["id"]
        self.history = s["history"]
        turns = len([m for m in s["history"] if m.get("role") == "user"])
        self.console.print(f"[green]resumed[/] {s['id'][:8]}  [dim]({turns} turns)[/]")

    def _session_show(self, ref: str) -> None:
        if not ref and self.session_id is None:
            self.console.print("[dim]no active session yet; starts on your first message[/]")
            return
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        target = self._match_session_id(ref) if ref else self.session_id
        s = self.sessions.get_owned(target, self.owner) if target else None
        if s is None:
            self.console.print(f"[yellow]no such session for this owner: {ref}[/]")
            return
        users = [m for m in s["history"] if m.get("role") == "user"]
        first = users[0]["content"] if users else "(empty)"
        from rich.text import Text
        line = Text()
        line.append(f"session {s['id'][:8]}  ·  ")
        line.append(s.get("name") or "(unnamed)", style=(None if s.get("name") else self.theme.muted))
        line.append(f"  ·  {len(users)} turns  ·  updated {(s['updated_at'] or '')[:19]}")
        self.console.print(line)
        self.console.print(f"[dim]first:[/] {first[:80]}")

    def _cmd_clear(self, _arg: str = "") -> None:
        # Clears only the live context window; the persisted session is untouched (resuming it later
        # restores the full history). Use /session new to start a fresh persisted session.
        self.history = []
        self.console.print("[green]context cleared[/] [dim](the saved session is unchanged)[/]")

    def _confirm(self, question: str, expect: str = None) -> bool:
        """A blocking confirmation prompt for a destructive action. With `expect`, require typing that
        exact word (type-to-confirm); otherwise a y/N. A cancelled/aborted prompt is a No. Split out so
        tests can stub it without a TTY."""
        try:
            from prompt_toolkit import prompt as ptk_prompt
            if expect:
                return ptk_prompt(f"{question} type '{expect}' to confirm: ").strip() == expect
            return ptk_prompt(f"{question} [y/N] ").strip().lower() in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    def _cmd_reset(self, _arg: str = ""):
        """/reset — DESTRUCTIVE. Wipe ALL local data (conversations, memory, API keys, schedules,
        history) and the onboarding markers, then leave the shell so the next `bob` starts at first-run.
        Requires typing 'reset' to confirm. Returns False to exit the REPL once done."""
        from bob import kernel
        if not self._confirm(f"[{self.theme.error}]{kernel.RESET_WARNING}[/]", expect=kernel.RESET_CONFIRM):
            self.console.print("[dim]reset cancelled[/]")
            return True
        if self.sessions is not None:
            try:
                self.sessions.close()     # release the DB lock so the file can be removed (Windows)
            except Exception:
                pass
        removed = kernel.reset_all_data()
        self.session_id = None            # nothing left to consolidate on exit
        self.history = []
        self.console.print(f"[{self.theme.success}]{kernel.reset_done_line(removed)}[/]")
        return False                      # leave the REPL

    def _cmd_skill(self, arg: str) -> None:
        parts = arg.split(maxsplit=1) if arg else []
        name = parts[0] if parts else ""
        skill_args = parts[1] if len(parts) > 1 else ""
        if not name:
            self.console.print(catalog.render_skills(self.skills))
            return
        if name not in self.skills.skills:
            self.console.print(f"[yellow]Unknown skill: {name}[/]  (try /skills)")
            return
        if self.skills.skills[name]["steps"]:            # tool-sequence — synchronous, no model
            self.console.print(self.skills.run(name, self.tools))
            return
        # Sub-agent skill: drive it as an event stream through the SAME renderer as an agent
        # turn — surface through run_agent_events, never bespoke skill-rendering in the shell.
        def factory(cancel, approve):
            return self.skills.run_events(
                name, self.tools, config=self.config, args=skill_args,
                cancel=cancel, approve=approve, owner=self.owner, scope=self.scope)

        self._consume(factory)

    def _cmd_theme(self, arg: str) -> None:
        """/theme [<preset>|reload] — no arg shows the active theme + where to edit it; a preset name
        (mauve, dark, light, daltonized, ansi) switches the palette live; `reload` re-reads
        config/ui.json (picking up file edits and dropping any live preset override)."""
        low = arg.strip().lower()
        if low == "reload":
            self._theme_preset = None
            self._apply_theme(Theme.load(self.config, self.console))
            self.console.print(f"[{self.theme.success}]theme reloaded from {self.theme.source}[/]")
            theme_mod.render_header(self.theme, self.console)
            return
        if low in theme_mod.preset_names():
            self._theme_preset = low
            self._apply_theme(Theme.load(self.config, self.console, preset=low))
            self.console.print(f"[{self.theme.success}]theme → {low}[/]")
            theme_mod.render_header(self.theme, self.console)
            return
        if low:
            self.console.print(
                f"[{self.theme.warn}]unknown theme '{arg.strip()}'[/]  "
                f"[{self.theme.muted}](presets: {', '.join(theme_mod.preset_names())} · or 'reload')[/]")
            return
        t = self.theme
        h = t.header
        active = self._theme_preset or theme_mod.load_ui(self.config).get("theme", "mauve")
        self.console.print(
            f"[bold]theme[/] [{t.accent}]{active}[/]  ·  /theme <preset> to switch "
            f"({', '.join(theme_mod.preset_names())})  ·  edit [{t.accent}]{t.source}[/] then /theme reload\n"
            f"  header : {h.text!r}  font={h.font!r}  align={h.align}  dir={h.gradient_dir}\n"
            f"           gradient={list(h.gradient)}\n"
            f"  colors : accent={t.accent} success={t.success} error={t.error} "
            f"warn={t.warn} tool={t.tool} muted={t.muted}\n"
            f"  tagline: {t.tagline!r}\n"
            f"  layout : markdown={t.markdown} meta_panel={t.meta_panel} rule={t.rule} "
            f"blank_between_turns={t.blank_between_turns} prose_width={t.prose_width}"
        )

    def _apply_theme(self, theme: Theme) -> None:
        """Swap in a freshly loaded Theme and recolour the live prompt/toolbar too (not just the
        transcript). The prompt style is captured by the PromptSession at build time, so update it in
        place when a session is active."""
        self.theme = theme
        session = getattr(self, "_session", None)
        if session is not None:
            try:
                from prompt_toolkit.styles import Style
                session.style = Style.from_dict(theme.prompt_style)
            except Exception:
                pass

    # -- agent turn -----------------------------------------------------------

    def _run_turn(self, goal: str) -> str:
        """Run one agent turn, streaming its events. Uses the real run_agent_events generator. Returns the
        final answer string (None if cancelled/errored) so callers like /voice can act on it (TTS)."""
        goal = (goal or "").strip()
        if not goal:
            return None
        # Over-budget refusal — mirror the server's _load_session_or_404 402 branch. Guarded on
        # session_id because of lazy creation (no session before the first turn); a no-op unless a
        # positive agent.maxSessionTokens is configured (over_budget is False for a 0 budget).
        if self.sessions is not None and self.session_id and self.sessions.over_budget(self.session_id):
            self.console.print(
                f"[{self.theme.warn}]session token budget exhausted. /session new to continue[/]")
            return None
        from bob_loop import run_agent_events

        def factory(cancel, approve):
            return run_agent_events(
                goal, self.config, role=self.role, agency=self.agency,
                registry=self.tools, history=self.history, stream=True,
                cancel=cancel, approve=approve, owner=self.owner, scope=self.scope,
                no_tools=self.no_tools,
            )

        result = self._consume(factory)
        # Continuity: keep the live buffer and the persisted session in lockstep. Record only a real
        # answer (a cancelled/errored turn returns None and is not persisted).
        if result is not None:
            self.history.append({"role": "user", "content": goal})
            self.history.append({"role": "assistant", "content": result})
            self._persist_turn(goal, result)
        return result

    def _cmd_voice(self, _arg: str = "") -> None:
        """/voice — a spoken conversation inside the shell. Loops mic → STT → agent turn → TTS,
        wrapping the SAME `_run_turn` as text, so voice inherits memory + write-back + one persona + retry
        + logging + tools automatically (the whole point of the one-engine unification — no separate
        streaming path, no 256-token cap, no separate persona). Each reply streams to the screen and is Ctrl-C
        cancellable; Ctrl-C while listening — or saying 'exit'/'stop'/'quit'/'goodbye' — leaves voice mode
        back to the text prompt. Uses the shell's current role: reasoning/verbosity is a `/model` choice,
        not a per-turn `/no_think` string hack (which would corrupt the persisted turn + memory)."""
        import bob_voice

        t = self.theme
        # Auto-ensure speech-to-text, consistent with chat's auto-start — voice should "just work", not
        # make you go run `bob whisper` first. TTS uses the piper binary directly (no server), and speak()
        # points at `bob setup-voice` itself if the binary/voice model are missing.
        if not bob_voice.stt_ready(self.config):
            self.console.print(f"[{t.muted}]starting whisper (speech-to-text)…[/]")
            import stack
            try:
                stack.ensure_deps(self.config, stt=True)   # the one ensure-deps seam; idempotent, waits
            except Exception as e:  # noqa: BLE001 — advisory; the readiness re-check below decides
                self.console.print(f"[{t.warn}]{e}[/]")
            if not bob_voice.stt_ready(self.config):
                self.console.print(
                    f"[{t.warn}]whisper STT unreachable on :{bob_voice.stt_port(self.config)} — "
                    f"run [bold]bob setup-voice[/] to build it, then /voice again.[/]")
                return
        self.console.print(
            f"[{t.accent}]voice mode[/] [{t.muted}]· speak after 'listening' · say \"exit\" or press Ctrl-C "
            f"to leave · headphones avoid echo[/]")
        stt_port = bob_voice.stt_port(self.config)
        try:
            while True:
                # Phase feedback so the wait never reads as a hang: record → transcribe → think → speak,
                # each announced. (Recording blocks until you pause ~1.5s; then STT + the model run.)
                self.console.print(f"[{t.accent}]listening…[/] [{t.muted}](speak, then pause. "
                                   f"Ctrl-C or say \"exit\" to leave)[/]")
                try:
                    wav = bob_voice.record(self.config)
                except KeyboardInterrupt:            # Ctrl-C while recording → leave voice mode
                    break
                except RuntimeError as e:            # audio stack vanished mid-session
                    self.console.print(f"[{t.error}]{e}[/]")
                    break
                if not wav:                          # nothing captured → nudge (mic?) and keep listening
                    self.console.print(
                        f"[{t.muted}]didn't catch anything. Speak a bit louder, check your mic "
                        f"input device/volume, or Ctrl-C to exit[/]")
                    continue
                try:
                    with self._phase("transcribing…"):
                        transcript = bob_voice.transcribe_bytes(wav, stt_port)
                except RuntimeError as e:            # whisper server vanished mid-session
                    self.console.print(f"[{t.error}]{e}[/]")
                    break
                if not transcript.strip():
                    self.console.print(f"[{t.muted}]couldn't make out any words. Try again, or Ctrl-C[/]")
                    continue
                if transcript.strip().lower().rstrip(".!?") in _VOICE_EXIT_WORDS:
                    break
                self.console.print(f"[{t.accent}]›[/] {transcript}")
                # One failed turn must not abort the whole session; _run_turn already renders its
                # own errors (incl. a 'thinking' spinner) and returns None on cancel/error, so we just
                # guard the TTS side-effect.
                result = self._run_turn(transcript)
                if result:
                    spoken = bob_voice.format_for_speech(result)
                    if spoken:
                        try:
                            with self._phase("speaking…"):
                                bob_voice.speak(spoken, self.config)
                        except KeyboardInterrupt:     # Ctrl-C during playback → stop audio, leave voice
                            break
                if getattr(self, "_exit_requested", False):   # a tool (e.g. music_play) ends voice mode
                    break
        finally:
            self.console.print(f"[{t.muted}]voice ended[/]")

    def _phase(self, label: str):
        """A short-lived status spinner for a voice phase (transcribing / speaking). Falls back to a
        plain print on a non-terminal console (tests) so nothing hangs waiting on a spinner."""
        return self.console.status(f"[{self.theme.muted}]{label}[/]", spinner=self.theme.spinner)

    def _persist_turn(self, goal: str, result: str) -> None:
        """Mirror the server's _record_turn ([bob_agent_server.py]): append the turn to the
        owner-scoped SessionStore, creating the session lazily on the first turn. Best-effort — a
        store hiccup must not break the turn's UX."""
        if self.sessions is None:
            return
        try:
            if self.session_id is None:
                self.session_id = self.sessions.create(
                    token_budget=self._max_tokens, owner_id=self.owner)["id"]
                # Name the fresh session: an explicit /session name if one was queued, else auto from
                # this first message — so /session list and resume-by-name are useful immediately.
                name = self._pending_name or _derive_session_name(goal)
                if name:
                    self.sessions.set_name_owned(self.session_id, self.owner, name)
                self._pending_name = None
            from bob_loop import _estimate_tokens
            used = _estimate_tokens(goal) + _estimate_tokens(result or "")
            self.sessions.append_turn(self.session_id, goal, result, tokens_used=used)
        except Exception as e:
            self.console.print(f"[{self.theme.warn}]session not saved: {e}[/]")

    def _consume(self, factory, on_approval=None) -> str:
        """Drive an event generator in a worker thread; render events on the main thread; bridge
        approvals; honour Ctrl-C via the shared CancelToken. `factory(cancel, approve)` returns the
        generator (the real turn, or a fake in tests). Returns the final result string (None if the
        run produced no answer / was cancelled). Mirrors the server's worker-thread + CancelToken
        consumer ([bob_agent_server.py])."""
        from bob_loop import CancelToken

        on_approval = on_approval or self._approve
        cancel = CancelToken()
        events: queue.Queue = queue.Queue()
        answers: queue.Queue = queue.Queue(maxsize=1)   # main → worker: one pending approval at a time

        def approve_bridge(action):
            # Runs in the worker thread: the approval_required event already went to `events`; block
            # here until the main thread renders the prompt and hands back a decision.
            return answers.get()

        def worker():
            try:
                for ev in factory(cancel, approve_bridge):
                    events.put(ev)
            except Exception as e:  # never let a worker crash strand the main thread
                events.put({"type": "error", "message": str(e)})
            finally:
                events.put(_SENTINEL)

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        if self.theme.blank_between_turns:
            self.console.print()
        renderer = _TurnRenderer(self.console, self.agency, self.theme)
        renderer.begin()          # 'thinking' spinner until the first event — no dead air
        result = None
        cancelled = False
        self._exit_requested = False   # set from the final event; /voice reads it to leave voice mode
        # Poll with a short timeout rather than block forever on get(): on Windows a Ctrl-C can't
        # interrupt a lock held in C, so a bare get() would swallow the signal — the timeout returns
        # control to Python bytecode ~10×/s so a pending KeyboardInterrupt is delivered promptly.
        try:
            while True:
                try:
                    try:
                        ev = events.get(timeout=0.1)
                    except queue.Empty:
                        renderer.tick()      # refresh the spinner's live elapsed timer
                        continue
                    if ev is _SENTINEL:
                        break
                    if ev.get("type") == "approval_required":
                        renderer.quiesce()   # let the approval prompt own the terminal
                        decision = False if cancelled else bool(on_approval(ev))
                        self._put(answers, decision)
                    else:
                        if ev.get("type") == "final":
                            result = ev.get("result")
                            self._exit_requested = bool(ev.get("exit_requested"))
                        renderer.handle(ev)
                except KeyboardInterrupt:  # Ctrl-C: trip the run's cancel, release any pending approval
                    cancel.cancel()
                    cancelled = True
                    self._unblock(answers)
                    # Arm the two-stage exit: an immediate second Ctrl-C at the prompt now leaves.
                    self._pending_exit = True
        finally:
            renderer.close()
        t.join(timeout=10)
        if cancelled:
            self.console.print(f"[{self.theme.warn}]cancelled[/]")
        return None if cancelled else result

    # -- approval -------------------------------------------------------------

    def _approve(self, action: dict) -> bool:
        """Prompt the user to approve one gated tool call (main thread; the turn is quiesced). Honours
        a session-scoped 'always' set so a repeated tool isn't re-asked."""
        tool = action.get("tool", "?")
        if tool in self._always:
            return True
        args = _compact_args(action.get("arguments"))
        risk = action.get("risk", "confirm")
        from rich.panel import Panel
        color = self.theme.error if risk == "high" else self.theme.warn
        self.console.print(Panel(
            f"[bold]{tool}[/]([{self.theme.muted}]{args}[/])",
            title=f"approve tool · risk={risk}", title_align="left",
            border_style=color, expand=False,
        ))
        try:
            from prompt_toolkit import prompt as ptk_prompt
            from prompt_toolkit.formatted_text import HTML
            ans = ptk_prompt(HTML("  <ansigreen>y</ansigreen>es / "
                                  "<b>N</b>o / <ansicyan>a</ansicyan>lways › ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise KeyboardInterrupt
        if ans in ("a", "always"):
            self._always.add(tool)
            return True
        return ans in ("y", "yes")

    # -- first run + lifecycle ------------------------------------------------

    def _first_run_pending(self, flag_path=None) -> bool:
        """True the first time the shell is opened (then False forever). Marked by a flag file in the
        data dir; `flag_path` is injectable for tests."""
        import osenv
        p = Path(flag_path) if flag_path else (osenv.data_dir() / ".onboarded")
        if p.exists():
            return False
        try:
            p.write_text("1", encoding="utf-8")
        except Exception:
            pass
        return True

    def _print_first_run(self) -> None:
        """A one-time welcome panel pointing at the health check + the top entry points."""
        from rich.align import Align
        from rich.panel import Panel
        from rich.text import Text
        t = self.theme
        body = Text()
        body.append("Welcome. Bob runs entirely on your machine.\n\n")
        for cmd, what in (("bob doctor", "check your setup is healthy"),
                          ("/help", "see every command"),
                          ("/agent <goal>", "let Bob use tools to do a task")):
            body.append(f"{cmd:<14}", style=f"bold {t.accent}")
            body.append(f" {what}\n", style=t.muted)
        panel = Panel(body, title="first run", title_align="left",
                      border_style=t.accent, expand=False, padding=t.panel_padding)
        # Align.center centers the panel as a block; console justify=center would also center the panel's
        # inner text and ragged the columns.
        self.console.print(Align.center(panel) if t.centered else panel)
        self.console.print()

    def _on_session_end(self, session_id) -> None:
        """Lifecycle seam — the point where a session is being left (on /exit, /session new,
        /session resume). Wires end-of-session memory consolidation in via _consolidate_session.
        Guarded: no-op without a persisted session, and never raises into the exit path."""
        if not session_id or self.sessions is None:
            return
        try:
            self._consolidate_session(session_id)
        except Exception as e:
            self.console.print(f"[{self.theme.warn}]session consolidation skipped: {e}[/]")

    def _consolidate_session(self, session_id) -> None:
        """End-of-session consolidation: extract durable facts from this session's turns and
        store them (deduped) + one episodic recap. Gated on memory.enabled && memory.autoConsolidate;
        skipped for a session with no turns. Synchronous but best-effort (the core swallows failures)."""
        mem = self.config.get("memory", {})
        if not (mem.get("enabled", False) and mem.get("autoConsolidate", True)):
            return
        if not self.history:
            return
        from bob_core import consolidate_session
        # A brief status so the exit pause (one LLM call, bounded by memory.consolidateTimeout) reads
        # as intentional, not a freeze.
        self.console.print(f"[{self.theme.muted}]saving session memory…[/]")
        result = consolidate_session(self.history, config=self.config, owner=self.owner,
                                     scope=self.scope, session_id=session_id)
        n = result.get("facts", 0)
        if n:
            self.console.print(f"[{self.theme.muted}]remembered {n} fact(s) from this session[/]")

    def _on_exit(self) -> None:
        self._on_session_end(self.session_id)
        self.console.print(f"[{self.theme.muted}]bye[/]")

    # -- small queue helpers (deny/unblock a pending approval on cancel) -------

    @staticmethod
    def _put(q: queue.Queue, value) -> None:
        try:
            q.put_nowait(value)
        except queue.Full:
            pass

    def _unblock(self, answers: queue.Queue) -> None:
        """Feed a deny into the answer queue so a worker blocked in approve() is released on cancel."""
        self._put(answers, False)


def run(config=None, role=None, no_tools=False) -> int:
    """Entry point for `python -m bob shell` (and the no-arg interactive front door). `role` +
    `no_tools` let `bob chat/code/think` launch the shell in chat mode (preset role, tools off)."""
    if not is_interactive():
        from bob.cli import _print_help
        _print_help()
        return 0
    _force_utf8()   # before build() creates the rich Console, so it inherits a UTF-8 stdout
    return BobShell.build(config, role=role, no_tools=no_tools).run()


def run_voice(config=None, role=None, no_tools=False) -> int:
    """Entry point for `bob voice`: launch the shell straight into /voice mode (mic→STT→loop→TTS)
    instead of the text REPL, then run the session-end write-back on exit — voice sessions get the same
    memory consolidation as text ones. TTY-gated like run(): a non-TTY invocation prints help."""
    if not is_interactive():
        from bob.cli import _print_help
        _print_help()
        return 0
    _force_utf8()
    shell = BobShell.build(config, role=role, no_tools=no_tools)
    try:
        shell._cmd_voice("")
    finally:
        shell._on_exit()   # consolidate + close the session, mirroring the REPL exit path
    return 0
