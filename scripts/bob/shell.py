"""NE2 (contract C1) — the interactive REPL/TUI: the no-arg `bob` front door on an interactive TTY.

Splash (header + model/role + session + tool/skill counts) + a prompt. Non-slash input is an agent
turn; slash commands drive the shell (`/agent`, `/tools`, `/skills`, `/model`, `/status`, `/session`,
`/agency`, `/theme`, `/clear`, `/help`, `/exit`). The turn drives `run_agent_events` (bob_loop) — the
SAME event stream the HTTP server consumes ([bob_agent_server.py]) — so a new O event type surfaces by
adding one case in `_TurnRenderer.handle`, never a shell rewrite.

Rendering aims at frontier-grade *inline* UX (like Claude Code / aider), NOT a full-screen TUI: a
scrolling transcript where assistant text streams as live Markdown (code/tables render), tool calls are
their own blocks with a spinner + ✓/✗ result, a styled prompt carries a persistent bottom toolbar, and
approvals show a risk-coloured panel. A full-screen alternate-buffer layout is deliberately avoided — it
breaks native scrollback and is fragile across terminals.

The look lives in [bob.theme](theme.py): a typed `Theme` parsed once from `config/ui.json` (the single
editing surface — colours, header font/gradient, glyphs, spacing, layout toggles). `/theme` shows it and
reloads the file. Everything degrades safely — no pyfiglet → a bold header; non-UTF-8 console → ASCII
glyphs + an ASCII font.

Coexistence (Decision A): rich renders; prompt_toolkit owns input (line editing / history / slash
completion) — readline is absent from Windows CPython, so stdlib input() has no editing there. The two
are never active at once: the prompt is idle while a turn streams, and a turn is fully quiescent while an
approval prompt is up.

Approval (NE0): the loop is event-driven, not a blocking input(). The turn generator runs in a worker
thread pushing events onto a queue; the main thread renders them. When the loop needs approval it yields
`approval_required` (→ queue) then blocks its own `approve()` on an answer queue; the main thread sees
the event, prompts the user, and hands the decision back. Ctrl-C trips the shared CancelToken → the turn
returns to the prompt, never the OS.

Built behind an isatty gate (Decision C): scripts/CI (no TTY) never enter the shell — `run()` refuses
and prints help instead, so a redirected/piped `bob` keeps today's behaviour.
"""
import json
import queue
import sys
import threading
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

# Spoken words that leave /voice mode (matched after stripping trailing punctuation). Ctrl-C while
# listening does the same; the pwsh voice loop had no verbal exit, so this is a small UX add.
_VOICE_EXIT_WORDS = {"exit", "quit", "stop", "goodbye", "bye"}

# Slash-command completion tree (NestedCompleter): each key may map to a sub-map or None.
_SLASH = {
    "/help": None,
    "/tools": None,
    "/skills": None,
    "/status": None,
    "/model": None,
    "/agency": {"show": None, "confirm": None, "silent": None},
    "/session": {"new": None, "list": None, "resume": None, "show": None},
    "/theme": {"reload": None},
    "/agent": None,
    "/voice": None,
    "/skill": None,
    "/services": {"start": None, "stop": None},
    "/up": None,
    "/restart": None,
    "/webui": None,
    "/stop": None,
    "/logs": None,
    "/clear": None,
    "/exit": None,
    "/quit": None,
}

# A tool result is an error/refusal (colour it ✗, not ✓) when it starts with one of these markers —
# both the dispatcher's own errors and the tools' in-band refusals (file sandbox, approval denial).
_ERR_MARKERS = (
    "tool error", "unknown tool", "bad arguments", "access denied", "not found", "not a directory",
    "was denied", "no allowedreadpaths", "is disabled", "error reading", "error writing",
    "error listing", "file_read:", "file_write:", "file_list:",
)


def is_interactive() -> bool:
    """Decision C — the shell launches only when BOTH ends are a real terminal. A pipe/redirect/CI on
    either stdin or stdout means a script, which must get help, never the REPL."""
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


def _force_utf8() -> None:
    """Best-effort: make stdout/stderr UTF-8 so the glyphs and Markdown never hit a cp1252 encode error
    on a legacy Windows console. `errors='replace'` degrades an unexpected char to '?' instead of
    crashing. No-op where already UTF-8 (POSIX, Windows Terminal launched via bob.ps1)."""
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
                              refresh_per_second=8, vertical_overflow="visible")
            self._live.start()

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
            self._status = self.console.status(f"[{self.t.muted}]{label}[/]", spinner=self.t.spinner)
            self._status.start()

    def _stop_spin(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    # -- event handlers ----------------------------------------------------

    def handle(self, ev: dict) -> None:
        t = ev.get("type")
        if t == "token":
            self._stop_spin()
            self.streamed = True
            self._buf += ev.get("text", "")
            if self.term:
                self._start_live()
                if self._live is not None:
                    self._live.update(self._renderable(self._buf))
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
        # S2 — `bob chat/code/think` launch the shell in chat mode: a preset role + no_tools (a plain
        # conversation). Default (bare `bob`/`bob shell`) keeps the agent role + full toolset.
        self.role = role or config.get("routing", {}).get("agentRole", "chat")
        self.no_tools = no_tools
        self.agency = config.get("agent", {}).get("agency", "show")
        # WI-6 — owner-scoped persisted sessions. The row is created LAZILY on the first turn
        # (session_id stays None until then) so opening `bob` and leaving leaves no empty session.
        # `sessions` is a SessionStore (injected in build()); None in unit tests unless supplied.
        self.owner = config.get("agent", {}).get("defaultOwner", "local")
        self._max_tokens = int(config.get("agent", {}).get("maxSessionTokens", 0) or 0)
        # MEM-7 — the project this shell was launched in (git root / cwd, or None if scopeByProject
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
        from rich.console import Console
        # highlight=False: only the theme's colours apply — rich's ReprHighlighter must not tint
        # identifiers/numbers (e.g. a magenta tool name) and fight the palette.
        self.console = console or Console(highlight=False)
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
        # WI-6 — same SessionStore the agent server uses (agent.sessionDbPath, resolved against the
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
        endpoint = "endpoint ready" if check_litellm(self.config) else "endpoint DOWN — run: bob up"
        return "\n".join([
            "Bob — local AI assistant",
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
        from prompt_toolkit.completion import NestedCompleter
        from prompt_toolkit.formatted_text import HTML
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.patch_stdout import patch_stdout
        from prompt_toolkit.styles import Style
        import osenv

        self._print_splash()
        if self._first_run_pending():
            self._print_first_run()
        style = Style.from_dict(self.theme.prompt_style)
        message = HTML(f"<prompt>{self.theme.prompt}</prompt> <arrow>{self.theme.arrow}</arrow> ")

        def toolbar():
            ntools = len(getattr(self.tools, "_loaded_names", []) or [])
            nskills = len(self.skills.list()) if hasattr(self.skills, "list") else 0
            return HTML(f" <b>{self.role}</b> · {self.agency} · {self._sid_label()} · "
                        f"{ntools} tools · {nskills} skills    ^C cancel · /help · /exit ")

        session = PromptSession(
            history=FileHistory(str(osenv.data_dir() / "shell-history.txt")),
            completer=NestedCompleter.from_nested_dict(_SLASH),
            complete_while_typing=False,
            style=style,
            bottom_toolbar=toolbar,
        )
        while True:
            try:
                with patch_stdout():
                    line = session.prompt(message)
            except KeyboardInterrupt:      # Ctrl-C at an empty prompt — clear the line, stay in the shell
                continue
            except EOFError:               # Ctrl-D — leave cleanly
                break
            if not self.dispatch(line):
                break
        self._on_exit()
        return 0

    def dispatch(self, line: str) -> bool:
        """Route one input line. Returns False to exit the REPL, True to keep looping. A leading '/'
        is a shell command; anything else is an agent turn."""
        line = (line or "").strip()
        if not line:
            return True
        if not line.startswith("/"):
            self._run_turn(line)
            return True

        parts = line.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/exit", "/quit"):
            return False
        handler = {
            "/help": self._cmd_help,
            "/tools": self._cmd_tools,
            "/skills": self._cmd_skills,
            "/status": self._cmd_status,
            "/model": self._cmd_model,
            "/agency": self._cmd_agency,
            "/session": self._cmd_session,
            "/theme": self._cmd_theme,
            "/clear": self._cmd_clear,
            "/agent": self._run_turn,
            "/voice": self._cmd_voice,
            "/skill": self._cmd_skill,
            "/services": self._cmd_services,
            "/up": self._cmd_up,
            "/restart": self._cmd_restart,
            "/webui": self._cmd_webui,
            "/stop": self._cmd_stop,
            "/logs": self._cmd_logs,
        }.get(cmd)
        if handler is None:
            self.console.print(f"[yellow]Unknown command: {cmd}[/]  (try /help)")
            return True
        handler(arg)
        return True

    # -- slash commands -------------------------------------------------------

    # Slash-command reference for the shell. This is the TUI's OWN surface — what you can DO from inside
    # `bob`. It is deliberately NOT the CLI verb catalog (`bob help`): the shell is the home base, and the
    # CLI verbs are the scripting / outside-terminal surface. The footer points at each other.
    _SLASH_HELP = [
        ("(type a message)", "chat with Bob, or describe a task to run"),
        ("/agent <goal>", "run the agent loop on a one-shot goal"),
        ("/voice", "spoken conversation (mic → loop → speech)"),
        ("/model [role]", "show or switch the role (chat, coder, planner, …)"),
        ("/agency [level]", "tool-approval mode: show | confirm | silent"),
        ("/session [new|list|resume <id>|show]", "persisted conversation history"),
        ("/skill [name]", "list or run a skill"),
        ("/tools", "list the agent's tools"),
        ("/skills", "list available skills"),
        ("/status", "system dashboard — every service, up/down"),
        ("/services [start|stop [name]]", "service dashboard; toggle a service in place"),
        ("/up [--with-services]", "start the stack in the background (endpoint + proxy + WebUI)"),
        ("/restart", "restart the inference endpoint"),
        ("/webui", "open the Open WebUI browser tab"),
        ("/stop", "stop local inference (frees VRAM)"),
        ("/logs", "recent inference-server log"),
        ("/theme [reload]", "reload the colour theme"),
        ("/clear", "clear the screen"),
        ("/help", "this reference"),
        ("/exit", "leave the shell"),
    ]

    def _cmd_help(self, _arg: str = "") -> None:
        from rich.table import Table
        t = self.theme
        tbl = Table(show_header=False, box=None, pad_edge=False)
        tbl.add_column(style=t.accent, no_wrap=True)
        tbl.add_column(style=t.muted)
        for cmd, desc in self._SLASH_HELP:
            tbl.add_row(cmd, desc)
        self.console.print(tbl)
        self.console.print(
            f"\n[italic {self.theme.muted}]This is the shell. For scripting / outside the terminal "
            f"(one-shots, Open WebUI, editors, API), run [bold]bob help[/] · [bold]/tools[/] · "
            f"[bold]/skills[/] for full lists[/]"
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
                    f"[{t.success}]ready[/]" if reachable else f"[{t.error}]DOWN[/] — run: bob up")
        self.console.print(tbl)
        # The whole system in one glance — so services (WebUI, SearXNG, n8n, Langfuse, …) aren't a
        # separate mystery from the assistant.
        self._render_dashboard()

    def _render_dashboard(self) -> None:
        """The cockpit dashboard: every service, grouped, coloured ● UP/down, with its URL (running) or
        start hint (down). Reads the one stack.service_snapshot — same data as `bob status`, rendered
        richly. `/services` shows this; `/status` appends it under the session table."""
        import stack
        from rich.table import Table
        t = self.theme
        tbl = Table(show_header=True, header_style=t.muted, box=None, pad_edge=False)
        tbl.add_column("")                       # status dot
        tbl.add_column("service", style=t.accent, no_wrap=True)
        tbl.add_column("port", style=t.muted, no_wrap=True)
        tbl.add_column("where / how")
        tbl.add_column("", style=t.muted)        # description
        seen_group = None
        for r in stack.service_snapshot(self.config):
            if r["group"] != seen_group:
                tbl.add_row("", f"[bold {t.accent}]{r['group']}[/]", "", "", "")
                seen_group = r["group"]
            dot = f"[{t.success}]{t.dot}[/]" if r["up"] else f"[{t.error}]{t.dot}[/]"
            where = (f"[{t.muted}]{r['url']}[/]" if r["up"]
                     else f"[{t.warn}]start: {r['hint']}[/]")
            tbl.add_row(dot, f"  {r['label']}", f":{r['port']}", where, r["desc"])
        self.console.print(tbl)

    def _cmd_services(self, arg: str = "") -> None:
        """/services — the cockpit dashboard (every service, up/down). /services start|stop [name]
        toggles one; with no name, the Docker services (SearXNG/n8n/Langfuse) as a group."""
        self._render_dashboard()

    def _cmd_up(self, arg: str = "") -> None:
        """/up [--with-services] [--no-open] — bring the stack up in the background (endpoint + proxy +
        WebUI) without leaving the shell. The cockpit: manage the system from here, not raw `bob` verbs."""
        import stack
        toks = arg.split()
        with_services = "--with-services" in toks or "services" in toks
        open_browser = "--no-open" not in toks
        self.console.print(stack.stack_up(self.config, open_browser=open_browser,
                                          with_services=with_services))

    def _cmd_restart(self, _arg: str = "") -> None:
        """/restart — bounce the inference endpoint + proxy (+ WebUI) and wait for ready."""
        import stack
        self.console.print(stack.stack_restart(self.config))

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
            self.console.print("Open WebUI isn't running — [bold]/up[/] starts it in the background "
                               "(or [bold]/services start webui[/]).")

    def _cmd_stop(self, _arg: str = "") -> None:
        """/stop — tear down local inference (frees VRAM) without leaving the shell. Auto-start brings
        it back on your next turn."""
        import stack   # scripts/tools is on sys.path (module top)
        self.console.print(stack.stack_stop(self.config))

    def _cmd_logs(self, arg: str = "") -> None:
        """/logs [N] — a bounded tail of the inference-server log (no follow; the shell owns the TTY)."""
        import stack
        n = int(arg) if arg.strip().isdigit() else 40
        self.console.print(stack.stack_logs(self.config, n))

    def _cmd_model(self, arg: str) -> None:
        if not arg:
            self.console.print(f"model/role: {self.role}")
            return
        self.role = arg.strip()
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

    def _cmd_session(self, arg: str) -> None:
        """/session new | list | resume <id> | show [id] — owner-scoped persisted sessions (WI-6)."""
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else ""
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub == "new":
            self._on_session_end(self.session_id)   # consolidate the one we're leaving (MEM-4 fills)
            self.session_id = None                  # lazy — the row is created on the next message
            self.history = []
            self.console.print("[green]new session[/] [dim](starts on your next message)[/]")
        elif sub == "list":
            self._session_list()
        elif sub == "resume":
            self._session_resume(rest)
        elif sub in ("show", ""):
            self._session_show(rest)
        else:
            self.console.print(f"[yellow]/session {sub}?[/]  (new | list | resume <id> | show [id])")

    def _match_session_id(self, ref: str):
        """Resolve a full id or an unambiguous 8-char prefix (as shown by /session list) within the
        current owner's sessions. Returns the full id, or None if unknown/ambiguous."""
        if self.sessions is None or not ref:
            return None
        ids = self.sessions.list_owned(self.owner)
        if ref in ids:
            return ref
        matches = [i for i in ids if i.startswith(ref)]
        return matches[0] if len(matches) == 1 else None

    def _session_list(self) -> None:
        if self.sessions is None:
            self.console.print("[dim]sessions unavailable[/]")
            return
        ids = self.sessions.list_owned(self.owner)
        if not ids:
            self.console.print("[dim]no saved sessions yet[/]")
            return
        from rich.table import Table
        t = self.theme
        tbl = Table(show_header=True, header_style=t.accent, box=None, pad_edge=False)
        tbl.add_column("id", style=t.muted)
        tbl.add_column("updated", style=t.muted)
        tbl.add_column("turns", justify="right")
        tbl.add_column("first message")
        for sid in ids[:20]:
            s = self.sessions.get(sid)
            if not s:
                continue
            users = [m for m in s["history"] if m.get("role") == "user"]
            first = users[0]["content"] if users else ""
            marker = "→ " if sid == self.session_id else "  "
            tbl.add_row(marker + sid[:8], (s["updated_at"] or "")[:19], str(len(users)),
                        (first[:50] + "…") if len(first) > 50 else first)
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
        self._on_session_end(self.session_id)   # consolidate the one we're leaving (MEM-4 fills)
        self.session_id = s["id"]
        self.history = s["history"]
        turns = len([m for m in s["history"] if m.get("role") == "user"])
        self.console.print(f"[green]resumed[/] {s['id'][:8]}  [dim]({turns} turns)[/]")

    def _session_show(self, ref: str) -> None:
        if not ref and self.session_id is None:
            self.console.print("[dim]no active session yet — starts on your first message[/]")
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
        self.console.print(
            f"session {s['id'][:8]}  ·  {len(users)} turns  ·  updated {(s['updated_at'] or '')[:19]}\n"
            f"[dim]first:[/] {first[:80]}"
        )

    def _cmd_clear(self, _arg: str = "") -> None:
        # Clears only the live context window; the persisted session is untouched (resuming it later
        # restores the full history). Use /session new to start a fresh persisted session.
        self.history = []
        self.console.print("[green]context cleared[/] [dim](the saved session is unchanged)[/]")

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
        # Sub-agent skill (O11): drive it as an event stream through the SAME renderer as an agent
        # turn (C6 — surface through run_agent_events, never bespoke skill-rendering in the shell).
        def factory(cancel, approve):
            return self.skills.run_events(
                name, self.tools, config=self.config, args=skill_args,
                cancel=cancel, approve=approve, owner=self.owner, scope=self.scope)

        self._consume(factory)

    def _cmd_theme(self, arg: str) -> None:
        """Show the active theme and where to edit it. `/theme reload` re-reads config/ui.json after you
        edit it. The look is configured in that file — there are no in-shell style tweaks."""
        t = self.theme
        if arg.strip().lower() == "reload":
            self.theme = Theme.load(self.config, self.console)
            self.console.print(f"[{self.theme.success}]theme reloaded from {self.theme.source}[/]")
            theme_mod.render_header(self.theme, self.console)
            return
        h = t.header
        self.console.print(
            f"[bold]theme[/]  edit [{t.accent}]{t.source}[/] then [bold]/theme reload[/]\n"
            f"  header : {h.text!r}  font={h.font!r}  align={h.align}  dir={h.gradient_dir}\n"
            f"           gradient={list(h.gradient)}\n"
            f"  colors : accent={t.accent} success={t.success} error={t.error} "
            f"warn={t.warn} tool={t.tool} muted={t.muted}\n"
            f"  tagline: {t.tagline!r}\n"
            f"  layout : markdown={t.markdown} meta_panel={t.meta_panel} rule={t.rule} "
            f"blank_between_turns={t.blank_between_turns} prose_width={t.prose_width}"
        )

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
                f"[{self.theme.warn}]session token budget exhausted — /session new to continue[/]")
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
        """/voice — a spoken conversation inside the shell (ONE-B4). Loops mic → STT → agent turn → TTS,
        wrapping the SAME `_run_turn` as text, so voice inherits memory + write-back + one persona + retry
        + logging + tools automatically (the whole point of the one-engine unification — no Invoke-BobStream
        path, no 256-token cap, no separate persona). Each reply streams to the screen and is Ctrl-C
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
                stack.service_control(self.config, "whisper", action="start")  # idempotent; waits for :8082
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
        try:
            while True:
                self.console.print(f"[{t.muted}]listening…[/]")
                try:
                    transcript = bob_voice.listen(self.config)
                except KeyboardInterrupt:            # Ctrl-C while recording → leave voice mode
                    break
                except RuntimeError as e:            # audio stack / server vanished mid-session
                    self.console.print(f"[{t.error}]{e}[/]")
                    break
                if not transcript.strip():           # nothing captured → nudge (mic?) and keep listening
                    self.console.print(
                        f"[{t.muted}]…didn't catch anything — speak a bit louder, check your mic "
                        f"input device/volume, or Ctrl-C to exit[/]")
                    continue
                if transcript.strip().lower().rstrip(".!?") in _VOICE_EXIT_WORDS:
                    break
                self.console.print(f"[{t.accent}]›[/] {transcript}")
                # M9 — one failed turn must not abort the whole session; _run_turn already renders its
                # own errors and returns None on cancel/error, so we just guard the TTS side-effect.
                result = self._run_turn(transcript)
                if result:
                    spoken = bob_voice.format_for_speech(result)
                    if spoken:
                        bob_voice.speak(spoken, self.config)
        finally:
            self.console.print(f"[{t.muted}]— voice ended[/]")

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
        # Poll with a short timeout rather than block forever on get(): on Windows a Ctrl-C can't
        # interrupt a lock held in C, so a bare get() would swallow the signal — the timeout returns
        # control to Python bytecode ~10×/s so a pending KeyboardInterrupt is delivered promptly.
        try:
            while True:
                try:
                    try:
                        ev = events.get(timeout=0.1)
                    except queue.Empty:
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
                        renderer.handle(ev)
                except KeyboardInterrupt:  # Ctrl-C: trip the run's cancel, release any pending approval
                    cancel.cancel()
                    cancelled = True
                    self._unblock(answers)
        finally:
            renderer.close()
        t.join(timeout=10)
        if cancelled:
            self.console.print(f"[{self.theme.warn}]— cancelled[/]")
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
        body.append("Welcome — Bob runs entirely on your machine.\n\n")
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
        """WI-6c lifecycle seam — the point where a session is being left (on /exit, /session new,
        /session resume). MEM-4 wires end-of-session memory consolidation in via _consolidate_session.
        Guarded: no-op without a persisted session, and never raises into the exit path."""
        if not session_id or self.sessions is None:
            return
        try:
            self._consolidate_session(session_id)
        except Exception as e:
            self.console.print(f"[{self.theme.warn}]session consolidation skipped: {e}[/]")

    def _consolidate_session(self, session_id) -> None:
        """MEM-4 — end-of-session consolidation: extract durable facts from this session's turns and
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
    """Entry point for `python -m bob shell` (and the no-arg interactive front door). S2: `role` +
    `no_tools` let `bob chat/code/think` launch the shell in chat mode (preset role, tools off)."""
    if not is_interactive():
        from bob.cli import _print_help
        _print_help()
        return 0
    _force_utf8()   # before build() creates the rich Console, so it inherits a UTF-8 stdout
    return BobShell.build(config, role=role, no_tools=no_tools).run()


def run_voice(config=None, role=None, no_tools=False) -> int:
    """Entry point for `bob voice` (ONE-B5): launch the shell straight into /voice mode (mic→STT→loop→TTS)
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
