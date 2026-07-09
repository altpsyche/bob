"""The interactive shell (scripts/bob/shell.py), exercised headlessly: splash string,
slash-command parsing, the event render loop against a FAKE run_agent_events (tokens/tool_call/
tool_result/final/error), the approval handshake (worker thread ↔ main thread), and the isatty
gate. A prompt_toolkit REPL can't be validated without a real TTY, so live keystroke behaviour (Ctrl-C
cancel, the real approval prompt) is the separate manual acceptance — here we prove everything under
it."""
import io
import time
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
from _common import FakeRegistry, fake_config

try:  # rich is the shell UI dep; skip (not error) where it isn't installed (e.g. the minimal CI python)
    import rich  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"rich not installed: {_e}")

from bob import shell as shellmod
from bob.shell import BobShell


class _FakeSkillReg:
    def __init__(self, skills=None):
        self.skills = skills or {}
        self.errors = []

    def list(self):
        return [dict(s) for s in self.skills.values()]

    def run(self, name, tools, context=None):
        return f"[ran skill {name}]"


def _make_shell(**cfg_over):
    from rich.console import Console

    console = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
    sh = BobShell(fake_config(**cfg_over), FakeRegistry(), _FakeSkillReg(), console=console)
    return sh, console


class TestSplash(unittest.TestCase):
    def test_splash_has_identity_role_session_counts(self):
        sh, _ = _make_shell()
        s = sh.splash()
        self.assertIn("Bob", s)
        self.assertIn(sh.role, s)              # agentRole from config == "agent"
        self.assertIsNone(sh.session_id)       # no persisted row until the first turn
        self.assertIn("session: new", s)       # shown as "new" until then
        self.assertIn("commands", s)           # counts line from the catalog


class TestDispatch(unittest.TestCase):
    def test_exit_returns_false(self):
        sh, _ = _make_shell()
        self.assertFalse(sh.dispatch("/exit"))
        self.assertFalse(sh.dispatch("/quit"))

    def test_blank_and_unknown_keep_looping(self):
        sh, out = _make_shell()
        self.assertTrue(sh.dispatch(""))
        self.assertTrue(sh.dispatch("   "))
        self.assertTrue(sh.dispatch("/nope"))
        self.assertIn("Unknown command", out.file.getvalue())

    def test_plain_text_routes_to_a_turn(self):
        sh, _ = _make_shell()
        seen = []
        sh._run_turn = lambda g: seen.append(g)  # don't hit the network
        self.assertTrue(sh.dispatch("what is 2+2"))
        self.assertEqual(seen, ["what is 2+2"])

    def test_agent_slash_routes_to_a_turn(self):
        sh, _ = _make_shell()
        seen = []
        sh._run_turn = lambda g: seen.append(g)
        sh.dispatch("/agent fix the bug")
        self.assertEqual(seen, ["fix the bug"])

    def test_model_and_agency_mutate_state(self):
        sh, _ = _make_shell()
        sh.dispatch("/model coder")
        self.assertEqual(sh.role, "coder")
        sh.dispatch("/agency confirm")
        self.assertEqual(sh.agency, "confirm")
        sh.dispatch("/agency bogus")            # rejected — unchanged
        self.assertEqual(sh.agency, "confirm")

    def test_model_unknown_role_warns_but_switches(self):
        sh, out = _make_shell()
        sh.dispatch("/model definitely-not-a-role")
        self.assertEqual(sh.role, "definitely-not-a-role")        # not blocked (custom models allowed)
        self.assertIn("not a known role", out.file.getvalue())    # but no longer silent

    def test_model_known_role_switches_cleanly(self):
        sh, out = _make_shell()
        sh.dispatch("/model coder")                               # a configured routing value
        self.assertEqual(sh.role, "coder")
        self.assertNotIn("not a known role", out.file.getvalue())

    def test_clear_help_describes_context_not_screen(self):
        sh, out = _make_shell()
        sh.dispatch("/help")
        self.assertIn("conversation context", out.file.getvalue())

    def test_session_new_resets_to_pending(self):
        sh, _ = _make_shell()
        sh.session_id = "deadbeefcafe"          # pretend a row already exists
        sh.history = [{"role": "user", "content": "x"}]
        sh.dispatch("/session new")
        self.assertIsNone(sh.session_id)        # pending — the row is created on the next message
        self.assertEqual(sh.history, [])

    def test_clear_empties_context(self):
        sh, _ = _make_shell()
        sh.history = [{"role": "user", "content": "y"}]
        sh.dispatch("/clear")
        self.assertEqual(sh.history, [])

    def test_tools_and_skills_render(self):
        sh, out = _make_shell()
        sh.dispatch("/tools")
        sh.dispatch("/skills")
        text = out.file.getvalue()
        self.assertIn("Tools", text)
        self.assertIn("Skills", text)


class TestSlashSource(unittest.TestCase):
    """Slash commands are defined once in _COMMANDS; the completion tree, the dispatch handler table,
    and /help all derive from it, so a new command is a single entry."""

    def test_every_handler_resolves_to_a_method(self):
        sh, _ = _make_shell()
        table = sh._handlers()
        for c in shellmod._COMMANDS:
            if not c.handler:                       # only /exit has no handler (inline exit)
                self.assertIn(c.name, shellmod._EXIT_CMDS)
                continue
            self.assertTrue(callable(getattr(sh, c.handler)))
            self.assertIn(c.name, table)            # dispatchable by name

    def test_completion_tree_covers_names_and_aliases(self):
        names = set()
        for c in shellmod._COMMANDS:
            names.update(c.names())
        self.assertEqual(set(shellmod._SLASH), names)

    def test_subcommands_nested_under_parent(self):
        self.assertEqual(set(shellmod._SLASH["/agency"]), {"show", "confirm", "silent"})
        self.assertEqual(set(shellmod._SLASH["/session"]),
                         {"new", "list", "resume", "name", "delete", "show"})
        self.assertEqual(set(shellmod._SLASH["/services"]), {"start", "stop"})
        self.assertEqual(set(shellmod._SLASH["/theme"]), {"reload"})
        self.assertIsNone(shellmod._SLASH["/tools"])     # no subs → a leaf

    def test_every_command_has_help_text(self):
        for c in shellmod._COMMANDS:
            self.assertTrue(c.desc.strip())

    def test_alias_dispatches_and_completes_like_primary(self):
        sh, _ = _make_shell()
        self.assertFalse(sh.dispatch("/quit"))           # alias of /exit → leaves the REPL
        self.assertIn("/quit", shellmod._SLASH)          # and is completable

    def test_help_lists_every_command(self):
        sh, out = _make_shell()
        sh.dispatch("/help")
        text = out.file.getvalue()
        for c in shellmod._COMMANDS:
            self.assertIn(c.name, text)


class TestRewind(unittest.TestCase):
    """/rewind restores the last turn's checkpointed edits; it is inert (a clear message) when
    checkpointing is off or nothing was snapshotted."""

    def test_off_reports_disabled(self):
        sh, out = _make_shell()
        sh.dispatch("/rewind")
        self.assertIn("checkpointing is off", out.file.getvalue())

    def test_restores_last_turn(self):
        import shutil
        import tempfile
        from pathlib import Path

        import bob_checkpoint
        d = Path(tempfile.mkdtemp(prefix="bob-shell-rw-"))
        try:
            target = d / "f.py"
            target.write_text("good\n", encoding="utf-8")
            sh, out = _make_shell(agent={"checkpointEdits": True, "checkpointDbPath": str(d / "cp.db"),
                                         "defaultOwner": "local"})
            store = bob_checkpoint.CheckpointStore(db_path=d / "cp.db", default_owner="local")
            store.snapshot("turn1", 0, "local", [target], prefer_git=False)
            target.write_text("BROKEN\n", encoding="utf-8")
            sh._last_run_id = "turn1"
            sh.dispatch("/rewind")
            self.assertEqual(target.read_text(), "good\n")
            self.assertIn("rewound", out.file.getvalue())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestVoiceMode(unittest.TestCase):
    """/voice loop glue: mic→STT→_run_turn→TTS, faked end to end. The turn path itself is the
    same _run_turn the text tests cover; here we prove the round-trip wiring, exit conditions, and edges."""

    def _voice_shell(self, transcripts):
        """A shell whose bob_voice is stubbed on the record→transcribe seam: record() yields a WAV for
        each item and transcribe_bytes() returns the scripted transcript (a KeyboardInterrupt/RuntimeError
        value is raised instead), _run_turn echoes, speak records what it spoke."""
        import bob_voice
        sh, out = _make_shell()
        spoken, turns = [], []
        seq = list(transcripts)

        def fake_record(config, silence_sec=None):
            if not seq:
                raise KeyboardInterrupt      # nothing left → simulate Ctrl-C to leave
            if isinstance(seq[0], BaseException):
                raise seq.pop(0)
            return b"WAV"                     # non-empty → proceeds to transcribe

        def fake_transcribe(wav, port):
            return seq.pop(0)                 # the scripted transcript for this capture

        sh._run_turn = lambda g: (turns.append(g), f"reply to {g}")[1]
        self._patches = [
            _patch(bob_voice, "stt_ready", lambda cfg: True),
            _patch(bob_voice, "stt_port", lambda cfg: 8082),
            _patch(bob_voice, "record", fake_record),
            _patch(bob_voice, "transcribe_bytes", fake_transcribe),
            _patch(bob_voice, "format_for_speech", lambda s: s),
            _patch(bob_voice, "speak", lambda s, cfg: (spoken.append(s), True)[1]),
        ]
        for p in self._patches:
            p.start()
        return sh, out, spoken, turns

    def tearDown(self):
        for p in getattr(self, "_patches", []):
            p.stop()

    def test_round_trips_then_exits_on_ctrl_c(self):
        sh, _out, spoken, turns = self._voice_shell(["hello", "how are you"])
        sh.dispatch("/voice")                # loop drains the two transcripts, then KeyboardInterrupt
        self.assertEqual(turns, ["hello", "how are you"])
        self.assertEqual(spoken, ["reply to hello", "reply to how are you"])

    def test_exit_word_leaves_the_loop(self):
        sh, _out, spoken, turns = self._voice_shell(["do a thing", "goodbye.", "never reached"])
        sh.dispatch("/voice")
        self.assertEqual(turns, ["do a thing"])   # stopped at the exit word; third item untouched
        self.assertEqual(spoken, ["reply to do a thing"])

    def test_exit_requested_tool_leaves_the_loop(self):
        # A tool with EXIT_VOICE (e.g. music_play) sets exit_requested on the final event; the loop
        # speaks the confirmation, then LEAVES voice mode instead of listening again.
        sh, _out, spoken, turns = self._voice_shell(["play a song", "never reached"])

        def run_turn(goal):
            turns.append(goal)
            sh._exit_requested = True        # mimic _consume seeing exit_requested from the tool
            return f"reply to {goal}"
        sh._run_turn = run_turn
        sh.dispatch("/voice")
        self.assertEqual(turns, ["play a song"])              # stopped; second item never captured
        self.assertEqual(spoken, ["reply to play a song"])    # confirmation still spoken before leaving

    def test_blank_transcript_keeps_listening(self):
        sh, _out, spoken, turns = self._voice_shell(["", "  ", "real"])
        sh.dispatch("/voice")
        self.assertEqual(turns, ["real"])         # empties skipped, no turn/speak for them

    def test_stt_down_autostarts_whisper(self):
        # New behavior: /voice auto-starts whisper (like chat's auto-start) instead of telling the user
        # to run `bob whisper`. Here the start "succeeds" (stt_ready flips True), so the loop proceeds.
        import bob_voice
        import osenv
        import stack
        sh, out = _make_shell()
        ready = iter([False, True])          # down at preflight, up after the ensure_deps start
        started = []
        with _patch(bob_voice, "stt_ready", lambda cfg: next(ready)), \
             _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "service_control", lambda cfg, name, action="start": started.append(action) or "ok"), \
             _patch(bob_voice, "record", lambda cfg, silence_sec=None: (_ for _ in ()).throw(KeyboardInterrupt())):
            sh.dispatch("/voice")
        self.assertEqual(started, ["start"])           # ensure_deps(stt=True) brought STT up
        self.assertNotIn("bob whisper", out.file.getvalue())

    def test_stt_still_down_after_autostart_points_to_setup(self):
        import bob_voice
        import osenv
        import stack
        sh, out = _make_shell()
        with _patch(bob_voice, "stt_ready", lambda cfg: False), \
             _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "service_control", lambda cfg, name, action="start": "ok"):
            sh.dispatch("/voice")
        self.assertIn("bob setup-voice", out.file.getvalue())   # honest next step when it can't start

    def test_ctrl_c_during_speak_leaves_without_crashing(self):
        # Regression: Ctrl-C while TTS audio plays raised KeyboardInterrupt out of subprocess.run and
        # crashed the whole shell. It must be caught: stop audio, leave voice mode, no traceback.
        import bob_voice
        sh, out = _make_shell()
        turns = []
        sh._run_turn = lambda g: (turns.append(g), "a reply")[1]
        seq = ["play something"]

        def fake_record(config, silence_sec=None):
            if not seq:
                raise KeyboardInterrupt
            return b"WAV"

        def boom_speak(s, cfg):
            raise KeyboardInterrupt      # simulate Ctrl-C during playback

        with _patch(bob_voice, "stt_ready", lambda c: True), \
             _patch(bob_voice, "stt_port", lambda c: 8082), \
             _patch(bob_voice, "record", fake_record), \
             _patch(bob_voice, "transcribe_bytes", lambda wav, port: seq.pop(0)), \
             _patch(bob_voice, "format_for_speech", lambda s: s), \
             _patch(bob_voice, "speak", boom_speak):
            sh.dispatch("/voice")        # must NOT raise
        self.assertEqual(turns, ["play something"])
        self.assertIn("voice ended", out.file.getvalue())

    def test_cancelled_turn_speaks_nothing(self):
        # _run_turn returns None on a cancelled/errored turn → nothing is synthesized for it.
        sh, _out, spoken, _turns = self._voice_shell(["question"])
        sh._run_turn = lambda g: None
        sh.dispatch("/voice")
        self.assertEqual(spoken, [])


def _patch(target, name, value):
    from unittest import mock
    return mock.patch.object(target, name, value)


class TestCockpit(unittest.TestCase):
    """Lifecycle controls from inside the shell (the cockpit): /up, /restart, /webui, all routed
    to the one stack.* core so you never drop to raw `bob` verbs to manage the system."""

    def test_up_routes_to_stack_up(self):
        import osenv
        import stack
        sh, out = _make_shell()
        calls = {}
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "stack_up", lambda cfg, open_browser=True, with_services=False:
                    calls.update(ob=open_browser, ws=with_services) or "brought up"):
            sh.dispatch("/up")
        self.assertEqual((calls["ob"], calls["ws"]), (True, False))
        self.assertIn("brought up", out.file.getvalue())

    def test_up_flags_parsed(self):
        import osenv
        import stack
        sh, _ = _make_shell()
        calls = {}
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "stack_up", lambda cfg, open_browser=True, with_services=False:
                    calls.update(ob=open_browser, ws=with_services) or "x"):
            sh.dispatch("/up --with-services --no-open")
        self.assertEqual((calls["ob"], calls["ws"]), (False, True))

    def test_restart_routes_to_stack_restart(self):
        import osenv
        import stack
        sh, out = _make_shell()
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "stack_restart", lambda cfg: "restarted"):
            sh.dispatch("/restart")
        self.assertIn("restarted", out.file.getvalue())

    def test_services_start_daemon_routes_to_service_control(self):
        import osenv
        import stack
        sh, _ = _make_shell()
        calls = []
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "service_control",
                    lambda cfg, name, action="start": calls.append((name, action)) or "ok"):
            sh.dispatch("/services start whisper")
        self.assertEqual(calls, [("whisper", "start")])

    def test_services_start_by_label_resolves_to_daemon(self):
        # the dashboard shows litellm as "api" — a toggle by the visible label must resolve.
        import osenv
        import stack
        sh, _ = _make_shell()
        calls = []
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "service_control",
                    lambda cfg, name, action="start": calls.append((name, action)) or "ok"):
            sh.dispatch("/services stop api")
        self.assertEqual(calls, [("litellm", "stop")])

    def test_services_no_name_toggles_docker_group(self):
        import osenv
        import stack
        sh, _ = _make_shell()
        calls = []
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "services_control", lambda cfg, action: calls.append(action) or "ok"):
            sh.dispatch("/services stop")
        self.assertEqual(calls, ["stop"])

    def test_services_docker_name_toggles_single_container(self):
        import osenv
        import stack
        sh, _ = _make_shell()
        calls = []
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "services_control",
                    lambda cfg, action, service=None: calls.append((action, service)) or "ok"):
            sh.dispatch("/services start searxng")
        self.assertEqual(calls, [("start", "searxng")])   # just this container, not the whole group

    def test_services_toggle_rerenders_dashboard(self):
        import osenv
        import stack
        sh, out = _make_shell()
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(stack, "service_control", lambda cfg, name, action="start": "ok"):
            sh.dispatch("/services start piper")
        self.assertIn("searxng", out.file.getvalue())    # the dashboard re-rendered after the toggle

    def test_services_unknown_name_reports(self):
        import osenv
        sh, out = _make_shell()
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False):
            sh.dispatch("/services start bogus")
        self.assertIn("unknown service", out.file.getvalue())

    def test_webui_opens_when_running(self):
        import osenv
        sh, _ = _make_shell()
        opened = []
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: True), \
             _patch(osenv, "open_url", lambda u: opened.append(u)):
            sh.dispatch("/webui")
        self.assertEqual(len(opened), 1)

    def test_webui_advises_when_down(self):
        import osenv
        sh, out = _make_shell()
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False), \
             _patch(osenv, "open_url", lambda u: None):
            sh.dispatch("/webui")
        self.assertIn("/up", out.file.getvalue())        # honest next step, no inline foreground block

    def test_services_dashboard_lists_every_service(self):
        import osenv
        import stack
        sh, out = _make_shell()
        with _patch(osenv, "is_port_in_use", lambda p, *a, **k: False):
            sh.dispatch("/services")
        text = out.file.getvalue()
        for s in stack.SERVICES:
            self.assertIn(s.get("label", s["name"]), text)   # every service in the cockpit table
        self.assertIn("start:", text)                        # down services show how to start

    def test_status_appends_the_dashboard(self):
        import bob_core
        import osenv
        sh, out = _make_shell()
        with _patch(bob_core, "check_litellm", lambda cfg: False), \
             _patch(osenv, "is_port_in_use", lambda p, *a, **k: False):
            sh.dispatch("/status")
        text = out.file.getvalue()
        self.assertIn("searxng", text)                       # dashboard rendered under the session table


class TestRenderLoop(unittest.TestCase):
    def test_streams_and_returns_final(self):
        sh, out = _make_shell()
        events = [
            {"type": "token", "text": "hel"},
            {"type": "token", "text": "lo"},
            {"type": "tool_call", "call_id": "0.0", "name": "web_search", "arguments": '{"q":1}'},
            {"type": "tool_result", "call_id": "0.0", "name": "web_search", "result": "hit"},
            {"type": "final", "result": "hello", "reason": "answer"},
        ]

        def factory(cancel, approve):
            for e in events:
                yield e

        result = sh._consume(factory, on_approval=lambda a: True)
        self.assertEqual(result, "hello")
        text = out.file.getvalue()
        self.assertIn("hello", text)        # tokens went through the console file
        self.assertIn("web_search", text)   # tool call rendered

    def test_history_records_only_a_real_answer(self):
        sh, _ = _make_shell()

        def factory(cancel, approve):
            yield {"type": "token", "text": "hi"}
            yield {"type": "final", "result": "hi", "reason": "answer"}

        # drive through the public _run_turn so history bookkeeping runs
        sh._consume = lambda fac, on_approval=None: "hi"  # short-circuit the thread machinery
        sh._run_turn("greet")
        self.assertEqual(sh.history[-2:], [
            {"role": "user", "content": "greet"},
            {"role": "assistant", "content": "hi"},
        ])

    def test_error_event_rendered_and_no_result(self):
        sh, out = _make_shell()

        def factory(cancel, approve):
            yield {"type": "error", "message": "LiteLLM proxy not reachable"}

        result = sh._consume(factory, on_approval=lambda a: True)
        self.assertIsNone(result)
        self.assertIn("LiteLLM proxy not reachable", out.file.getvalue())

    def test_silent_agency_hides_tool_previews(self):
        sh, out = _make_shell()
        sh.agency = "silent"

        def factory(cancel, approve):
            yield {"type": "tool_call", "call_id": "0.0", "name": "shell_run", "arguments": "{}"}
            yield {"type": "tool_result", "call_id": "0.0", "name": "shell_run", "result": "x"}
            yield {"type": "final", "result": "done", "reason": "answer"}

        sh._consume(factory, on_approval=lambda a: True)
        self.assertNotIn("shell_run", out.file.getvalue())


class TestStreamingThrottle(unittest.TestCase):
    """A growing answer is re-parsed at most a few times per second, not once per token, so a long
    stream stays ~linear instead of O(n^2). The complete buffer is still rendered at the segment end."""

    def test_reparse_throttled_but_final_complete(self):
        from rich.console import Console
        theme = _make_shell()[0].theme
        con = Console(file=io.StringIO(), force_terminal=True, no_color=True, width=80)
        r = shellmod._TurnRenderer(con, "show", theme)
        parses = {"n": 0}
        orig = r._renderable
        r._renderable = lambda text: (parses.__setitem__("n", parses["n"] + 1), orig(text))[1]
        r.begin()
        for _ in range(200):
            r.handle({"type": "token", "text": "word "})    # arrive faster than the refresh cadence
        r.handle({"type": "final", "result": "x", "reason": "answer"})
        r.close()
        self.assertLess(parses["n"], 50)             # throttled — nowhere near 200 parses
        self.assertGreaterEqual(parses["n"], 1)      # but the buffer was parsed at least once
        self.assertEqual(r.answer, "word " * 200)    # and the complete text is captured


class TestContextLabel(unittest.TestCase):
    """The bottom toolbar surfaces how full the context window is (estimated tokens, with a percent
    when a session budget is set)."""

    def test_counts_history_tokens(self):
        sh, _ = _make_shell()
        self.assertEqual(sh._context_label(), "~0 tok")          # empty history
        sh.history = [{"role": "user", "content": "hello world " * 20}]
        self.assertIn("tok", sh._context_label())
        self.assertNotEqual(sh._context_label(), "~0 tok")       # grows with content

    def test_shows_percent_with_budget(self):
        sh, _ = _make_shell()
        sh._max_tokens = 1000
        sh.history = [{"role": "user", "content": "word " * 100}]
        lbl = sh._context_label()
        self.assertIn("/1000 tok", lbl)
        self.assertIn("%", lbl)


class TestSpinnerTimer(unittest.TestCase):
    """The 'thinking'/'running' spinner shows a live elapsed timer, refreshed from the consume loop's
    idle poll, so a long wait never reads as a hang."""

    def _renderer(self):
        from rich.console import Console
        theme = _make_shell()[0].theme
        con = Console(file=io.StringIO(), force_terminal=True, no_color=True, width=80)
        return shellmod._TurnRenderer(con, "show", theme)

    def test_tick_shows_elapsed_seconds(self):
        r = self._renderer()
        r._start_spin("thinking")
        r._spin_start = time.monotonic() - 5          # pretend 5s have passed
        seen = {}
        r._status.update = lambda status=None, **k: seen.__setitem__("label", status)
        r.tick()
        r.close()
        self.assertIn("thinking", seen["label"])
        self.assertIn("5s", seen["label"])

    def test_tick_noop_under_a_second(self):
        r = self._renderer()
        r._start_spin("thinking")                     # just started (~0s)
        calls = {"n": 0}
        r._status.update = lambda *a, **k: calls.__setitem__("n", calls["n"] + 1)
        r.tick()
        r.close()
        self.assertEqual(calls["n"], 0)               # no premature '0s' flicker


class TestApprovalHandshake(unittest.TestCase):
    def _run(self, decision):
        sh, _ = _make_shell()
        seen = {"decision": None}

        def factory(cancel, approve):
            yield {"type": "approval_required", "call_id": "0.0", "tool": "shell_run",
                   "arguments": '{"command":"ls"}', "risk": "high"}
            granted = approve({"tool": "shell_run"})   # blocks until the main thread answers
            seen["decision"] = granted
            yield {"type": "tool_result", "call_id": "0.0", "name": "shell_run",
                   "result": "ran" if granted else "denied"}
            yield {"type": "final", "result": "ok", "reason": "answer"}

        result = sh._consume(factory, on_approval=lambda action: decision)
        return result, seen["decision"]

    def test_approve_round_trips_true(self):
        result, decision = self._run(True)
        self.assertEqual(result, "ok")
        self.assertTrue(decision)

    def test_deny_round_trips_false(self):
        result, decision = self._run(False)
        self.assertEqual(result, "ok")
        self.assertFalse(decision)

    def test_always_set_skips_prompt(self):
        sh, _ = _make_shell()
        sh._always.add("shell_run")
        # _approve returns True from the always-set WITHOUT importing/using prompt_toolkit.
        self.assertTrue(sh._approve({"tool": "shell_run", "arguments": "{}"}))


class TestDiffTranscript(unittest.TestCase):
    """A tool result that is a unified diff renders through render.diff_view; ordinary output keeps
    the one-line preview. Detection is conservative so normal text is never mistaken for a diff."""
    _DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old line\n+new line\n"

    def test_diff_result_renders_as_diff(self):
        sh, out = _make_shell()

        def factory(cancel, approve):
            yield {"type": "tool_result", "call_id": "0.0", "name": "file_edit", "result": self._DIFF}
            yield {"type": "final", "result": "done", "reason": "answer"}

        sh._consume(factory, on_approval=lambda a: True)
        text = out.file.getvalue()
        self.assertIn("+new line", text)
        self.assertIn("-old line", text)

    def test_plain_result_uses_preview(self):
        sh, out = _make_shell()

        def factory(cancel, approve):
            yield {"type": "tool_result", "call_id": "0.0", "name": "web_search",
                   "result": "just some ordinary text"}
            yield {"type": "final", "result": "done", "reason": "answer"}

        sh._consume(factory, on_approval=lambda a: True)
        self.assertIn("just some ordinary text", out.file.getvalue())

    def test_looks_like_diff_is_conservative(self):
        self.assertTrue(shellmod._looks_like_diff("@@ -1,2 +1,2 @@\n ctx"))
        self.assertTrue(shellmod._looks_like_diff("--- a/x\n+++ b/x\n"))
        self.assertFalse(shellmod._looks_like_diff("+1 for that idea"))   # stray + is not a diff
        self.assertFalse(shellmod._looks_like_diff("done"))
        self.assertFalse(shellmod._looks_like_diff(""))


class TestCtrlCExit(unittest.TestCase):
    """Two-stage exit: a Ctrl-C at the prompt (or one that cancels a turn) arms exit; a second
    consecutive prompt Ctrl-C leaves; any dispatched line disarms it."""

    def test_first_arms_second_exits(self):
        sh, out = _make_shell()
        self.assertFalse(sh._on_prompt_interrupt())      # first press: arm, don't exit
        self.assertTrue(sh._pending_exit)
        self.assertIn("again to exit", out.file.getvalue())
        self.assertTrue(sh._on_prompt_interrupt())       # second consecutive press: exit

    def test_dispatch_disarms(self):
        sh, _ = _make_shell()
        sh._pending_exit = True
        sh.dispatch("/help")
        self.assertFalse(sh._pending_exit)               # acting on the prompt clears the arm

    def test_turn_cancel_arms_exit(self):
        sh, _ = _make_shell()
        sh._pending_exit = False

        def factory(cancel, approve):
            yield {"type": "approval_required", "call_id": "0.0", "tool": "shell_run",
                   "arguments": "{}", "risk": "high"}
            yield {"type": "final", "result": "x", "reason": "answer"}

        def boom(_ev):
            raise KeyboardInterrupt      # user hits Ctrl-C at the approval prompt

        result = sh._consume(factory, on_approval=boom)
        self.assertIsNone(result)                        # cancelled → no answer
        self.assertTrue(sh._pending_exit)                # and the exit is armed


def _make_persistent_shell(tmpdir, owner="local", max_tokens=0):
    """A shell wired to a real (temp) SessionStore, for the persist/resume/budget tests."""
    import os
    from rich.console import Console
    from bob_session import SessionStore

    console = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
    store = SessionStore(os.path.join(tmpdir, "sessions.db"), default_owner=owner)
    cfg = fake_config()
    cfg["agent"]["defaultOwner"] = owner
    cfg["agent"]["maxSessionTokens"] = max_tokens
    sh = BobShell(cfg, FakeRegistry(), _FakeSkillReg(), console=console, sessions=store)
    return sh, store, console


class TestSessionNames(unittest.TestCase):
    """Sessions get a human name — auto from the first message, or set manually with /session name —
    and can be resumed / shown by that name."""

    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bob-sessname-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_first_turn_auto_names_from_the_message(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        sh._consume = lambda fac, on_approval=None: "ok"
        sh._run_turn("help me refactor the theme loader")
        self.assertEqual(store.get(sh.session_id)["name"], "help me refactor the theme loader")

    def test_long_message_name_is_clipped(self):
        self.assertEqual(shellmod._derive_session_name("x" * 100)[-1], "…")
        self.assertLessEqual(len(shellmod._derive_session_name("x" * 100)), 49)

    def test_pending_name_before_first_turn_is_applied(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        sh.dispatch("/session name refactor sprint")      # no row yet → queued
        self.assertIsNone(sh.session_id)
        sh._consume = lambda fac, on_approval=None: "ok"
        sh._run_turn("first message")                     # creation applies the queued name
        self.assertEqual(store.get(sh.session_id)["name"], "refactor sprint")

    def test_rename_active_session(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        sh._consume = lambda fac, on_approval=None: "ok"
        sh._run_turn("something")
        sh.dispatch("/session name my nice name")
        self.assertEqual(store.get(sh.session_id)["name"], "my nice name")

    def test_resume_by_name(self):
        _, store, _ = _make_persistent_shell(self.tmp)
        seed = store.create(owner_id="local")
        store.append_turn(seed["id"], "q", "a")
        store.set_name_owned(seed["id"], "local", "billing bug")
        sh, _, _ = _make_persistent_shell(self.tmp)
        sh.dispatch("/session resume billing")            # unique name substring
        self.assertEqual(sh.session_id, seed["id"])

    def test_list_shows_names(self):
        sh, store, out = _make_persistent_shell(self.tmp)
        s = store.create(owner_id="local")
        store.append_turn(s["id"], "hi", "yo")
        store.set_name_owned(s["id"], "local", "distinctive-label")
        sh.dispatch("/session list")
        self.assertIn("distinctive-label", out.file.getvalue())

    def test_resume_completion_offers_name_and_id(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        s = store.create(owner_id="local")
        store.append_turn(s["id"], "q", "a")
        store.set_name_owned(s["id"], "local", "alpha project")
        refs = sh._session_refs()                          # the /session resume value provider
        self.assertIn("alpha project", refs)
        self.assertIn(s["id"][:8], refs)


class TestSessionDelete(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bob-sessdel-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_delete_one_by_ref(self):
        sh, store, out = _make_persistent_shell(self.tmp)
        s = store.create(owner_id="local")
        store.append_turn(s["id"], "q", "a")
        sh.dispatch(f"/session delete {s['id'][:8]}")
        self.assertEqual(store.list_owned("local"), [])
        self.assertIn("deleted", out.file.getvalue())

    def test_delete_active_resets_to_pending(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        sh._consume = lambda fac, on_approval=None: "ok"
        sh._run_turn("hello")
        sh.dispatch(f"/session delete {sh.session_id}")
        self.assertIsNone(sh.session_id)                   # dropped the active one → pending
        self.assertEqual(sh.history, [])

    def test_delete_all_confirmed(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        for _ in range(3):
            store.append_turn(store.create(owner_id="local")["id"], "q", "a")
        sh._confirm = lambda *a, **k: True
        sh.dispatch("/session delete all")
        self.assertEqual(store.list_owned("local"), [])

    def test_delete_all_declined_keeps(self):
        sh, store, out = _make_persistent_shell(self.tmp)
        store.append_turn(store.create(owner_id="local")["id"], "q", "a")
        sh._confirm = lambda *a, **k: False
        sh.dispatch("/session delete all")
        self.assertEqual(len(store.list_owned("local")), 1)   # nothing removed
        self.assertIn("cancelled", out.file.getvalue())

    def test_delete_unknown_reports(self):
        sh, _, out = _make_persistent_shell(self.tmp)
        sh.dispatch("/session delete nope")
        self.assertIn("no such session", out.file.getvalue().lower())

    def test_delete_completion_offers_all_and_refs(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        s = store.create(owner_id="local")
        store.append_turn(s["id"], "q", "a")
        store.set_name_owned(s["id"], "local", "proj")
        refs = sh._completion_providers()[("/session", "delete")]()
        self.assertIn("all", refs)
        self.assertIn("proj", refs)


class TestReset(unittest.TestCase):
    def test_wipe_removes_data_and_strips_onboarding_marker(self):
        import json
        import os
        import shutil
        import tempfile
        from bob import kernel
        d = tempfile.mkdtemp(prefix="bob-reset-data-")
        cfgd = tempfile.mkdtemp(prefix="bob-reset-cfg-")
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(cfgd, ignore_errors=True))
        for name in ("sessions.db", "bob.db", "secrets.json", ".onboarded", "shell-history.txt"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x")
        cfg = os.path.join(cfgd, "user.json")
        with open(cfg, "w") as f:
            json.dump({"bob": {"onboardDeclined": True}, "litellmPort": 8081}, f)
        removed = kernel.reset_all_data(data_dir=d, user_cfg=cfg)   # the one shared wipe core
        self.assertEqual(os.listdir(d), [])                    # every data store gone
        with open(cfg) as f:
            left = json.load(f)
        self.assertNotIn("bob", left)                          # onboarding marker stripped → FTUE
        self.assertIn("litellmPort", left)                     # unrelated config preserved
        self.assertGreaterEqual(removed, 6)

    def test_slash_reset_confirmed_wipes_and_exits(self):
        from bob import kernel
        sh, _ = _make_shell()
        calls = {"n": 0}
        orig = kernel.reset_all_data
        kernel.reset_all_data = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), 5)[1]
        try:
            sh._confirm = lambda *a, **k: True
            self.assertFalse(sh.dispatch("/reset"))            # returns False → leaves the REPL
        finally:
            kernel.reset_all_data = orig
        self.assertEqual(calls["n"], 1)
        self.assertIsNone(sh.session_id)

    def test_slash_reset_declined_does_not_wipe(self):
        from bob import kernel
        sh, out = _make_shell()
        orig = kernel.reset_all_data

        def boom(*a, **k):
            raise AssertionError("must not wipe when declined")

        kernel.reset_all_data = boom
        try:
            sh._confirm = lambda *a, **k: False
            self.assertTrue(sh.dispatch("/reset"))             # declined → keep looping
        finally:
            kernel.reset_all_data = orig
        self.assertIn("cancelled", out.file.getvalue())

    def test_cli_reset_verb_yes_flag_wipes(self):
        from bob import cli, kernel
        calls = {"n": 0}
        orig = kernel.reset_all_data
        kernel.reset_all_data = lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), 3)[1]
        try:
            rc = cli._handle_reset(["--yes"])                  # non-interactive, confirmed via flag
        finally:
            kernel.reset_all_data = orig
        self.assertEqual(rc, 0)
        self.assertEqual(calls["n"], 1)

    def test_cli_reset_verb_wired_in_registry(self):
        from bob import cli, registry
        self.assertEqual(registry.by_name()["reset"]["handler"], "reset")
        self.assertIn("reset", cli._HANDLERS)


class TestSessionPersistence(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bob-wi6-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_first_turn_lazily_creates_and_persists(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        self.assertIsNone(sh.session_id)                 # nothing before the first turn
        self.assertEqual(store.list_owned("local"), [])
        sh._consume = lambda fac, on_approval=None: "the answer"
        sh._run_turn("a question")
        self.assertIsNotNone(sh.session_id)              # created on the first turn
        ids = store.list_owned("local")
        self.assertEqual(ids, [sh.session_id])
        got = store.get(sh.session_id)["history"]
        self.assertEqual(got, [
            {"role": "user", "content": "a question"},
            {"role": "assistant", "content": "the answer"},
        ])
        self.assertEqual(sh.history, got)                # live buffer mirrors the store

    def test_resume_restores_history_owner_scoped(self):
        # Seed a session directly in the store, then resume it in a fresh shell.
        _, store, _ = _make_persistent_shell(self.tmp)
        seed = store.create(owner_id="local")
        store.append_turn(seed["id"], "hello", "hi there")
        sh, _, out = _make_persistent_shell(self.tmp)     # same db path
        sh.dispatch(f"/session resume {seed['id']}")
        self.assertEqual(sh.session_id, seed["id"])
        self.assertEqual(sh.history, [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ])
        # A subsequent turn continues the SAME persisted session (no new row).
        sh._consume = lambda fac, on_approval=None: "more"
        sh._run_turn("again")
        self.assertEqual(store.list_owned("local"), [seed["id"]])
        self.assertEqual(len(store.get(seed["id"])["history"]), 4)

    def test_resume_by_8char_prefix(self):
        _, store, _ = _make_persistent_shell(self.tmp)
        seed = store.create(owner_id="local")
        store.append_turn(seed["id"], "q", "a")
        sh, _, _ = _make_persistent_shell(self.tmp)
        sh.dispatch(f"/session resume {seed['id'][:8]}")
        self.assertEqual(sh.session_id, seed["id"])

    def test_resume_other_owner_refused(self):
        _, store, _ = _make_persistent_shell(self.tmp)
        other = store.create(owner_id="alice")           # not "local"
        store.append_turn(other["id"], "secret", "shh")
        sh, _, out = _make_persistent_shell(self.tmp, owner="local")
        sh.dispatch(f"/session resume {other['id']}")
        self.assertIsNone(sh.session_id)                 # not adopted
        self.assertEqual(sh.history, [])
        self.assertIn("no such session", out.file.getvalue().lower())

    def test_list_only_shows_owner_sessions(self):
        _, store, _ = _make_persistent_shell(self.tmp)
        mine = store.create(owner_id="local")
        store.append_turn(mine["id"], "mine", "ok")
        theirs = store.create(owner_id="alice")
        store.append_turn(theirs["id"], "theirs", "ok")
        sh, _, out = _make_persistent_shell(self.tmp, owner="local")
        sh.dispatch("/session list")
        text = out.file.getvalue()
        self.assertIn(mine["id"][:8], text)
        self.assertNotIn(theirs["id"][:8], text)

    def test_over_budget_refuses_turn(self):
        sh, store, out = _make_persistent_shell(self.tmp, max_tokens=50)
        s = store.create(token_budget=50, owner_id="local")
        store.append_turn(s["id"], "x", "y", tokens_used=60)   # spent 60 > budget 50
        sh.session_id = s["id"]
        sh.history = store.get(s["id"])["history"]
        called = {"n": 0}
        sh._consume = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "z"
        sh._run_turn("keep going")
        self.assertEqual(called["n"], 0)                 # refused before the model ran
        self.assertIn("budget", out.file.getvalue().lower())

    def test_zero_budget_does_not_refuse(self):
        sh, store, _ = _make_persistent_shell(self.tmp, max_tokens=0)
        s = store.create(token_budget=0, owner_id="local")
        store.append_turn(s["id"], "x", "y", tokens_used=10_000)
        sh.session_id = s["id"]
        called = {"n": 0}
        sh._consume = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or "ok"
        sh._run_turn("continue")
        self.assertEqual(called["n"], 1)                 # 0 budget is unlimited — turn runs


class TestLifecycleHooks(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="bob-wi6c-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_exit_new_resume_fire_session_end_hook(self):
        sh, store, _ = _make_persistent_shell(self.tmp)
        seed = store.create(owner_id="local")
        store.append_turn(seed["id"], "q", "a")
        calls = []
        sh._on_session_end = lambda sid: calls.append(sid)

        sh.session_id = "aaaaaaaa"
        sh._on_exit()                                    # exit fires the hook with the current id
        sh.session_id = "cccccccc"
        sh.dispatch("/session new")                      # leaving a session fires it, then resets
        sh.session_id = "bbbbbbbb"
        sh.dispatch(f"/session resume {seed['id']}")     # so does resume, before switching
        self.assertEqual(calls, ["aaaaaaaa", "cccccccc", "bbbbbbbb"])

    def test_session_end_swallows_consolidation_errors(self):
        sh, store, out = _make_persistent_shell(self.tmp)
        sh.session_id = store.create(owner_id="local")["id"]

        def boom(_sid):
            raise RuntimeError("consolidation blew up")
        sh._consolidate_session = boom
        # Must not raise even though the seam's body throws.
        sh._on_exit()
        self.assertIn("consolidation skipped", out.file.getvalue().lower())

    def test_session_end_noop_without_session(self):
        sh, _, _ = _make_persistent_shell(self.tmp)
        hit = {"n": 0}
        sh._consolidate_session = lambda sid: hit.__setitem__("n", hit["n"] + 1)
        sh._on_session_end(None)                         # no session id -> no consolidation
        self.assertEqual(hit["n"], 0)

    def test_consolidate_calls_core_with_history_when_enabled(self):
        import bob_core
        sh, _, _ = _make_persistent_shell(self.tmp)
        sh.config["memory"] = {"enabled": True, "autoConsolidate": True}
        sh.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        captured = {}
        orig = bob_core.consolidate_session
        bob_core.consolidate_session = (lambda turns, config=None, owner=None, scope=None, session_id=None:
                                        captured.update(turns=turns, owner=owner, scope=scope,
                                                        session_id=session_id)
                                        or {"facts": 1})
        try:
            sh._consolidate_session("sid")
        finally:
            bob_core.consolidate_session = orig
        self.assertEqual(captured["turns"], sh.history)
        self.assertEqual(captured["owner"], sh.owner)
        self.assertEqual(captured["scope"], sh.scope)      # exit consolidation carries scope
        self.assertEqual(captured["session_id"], "sid")    # provenance stamp

    def test_consolidate_skipped_when_disabled(self):
        import bob_core
        sh, _, _ = _make_persistent_shell(self.tmp)
        sh.config["memory"] = {"enabled": False, "autoConsolidate": True}
        sh.history = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
        called = {"n": 0}
        orig = bob_core.consolidate_session
        bob_core.consolidate_session = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {}
        try:
            sh._consolidate_session("sid")
        finally:
            bob_core.consolidate_session = orig
        self.assertEqual(called["n"], 0)                 # gated off -> core never called


class TestTheme(unittest.TestCase):
    def test_theme_no_arg_is_readonly_inspector(self):
        # bare /theme shows the active theme + file path; it must NOT mutate the theme.
        sh, out = _make_shell()
        before = sh.theme
        sh.dispatch("/theme")
        self.assertIs(sh.theme, before)                 # unchanged
        self.assertIn("ui.json", out.file.getvalue())   # points the user at the editable file

    def test_theme_switches_preset_live(self):
        sh, out = _make_shell()
        sh.dispatch("/theme dark")
        self.assertEqual(sh._theme_preset, "dark")      # runtime override recorded
        self.assertIsNot(sh.theme, None)
        self.assertIn("theme → dark", out.file.getvalue())

    def test_theme_unknown_preset_warns_and_keeps(self):
        sh, out = _make_shell()
        before = sh.theme
        sh.dispatch("/theme neonpink")
        self.assertIs(sh.theme, before)                 # unchanged on a bad name
        self.assertIsNone(sh._theme_preset)
        self.assertIn("unknown theme", out.file.getvalue().lower())

    def test_theme_reload_drops_preset_override(self):
        sh, _ = _make_shell()
        sh.dispatch("/theme dark")
        self.assertEqual(sh._theme_preset, "dark")
        sh.dispatch("/theme reload")
        self.assertIsNone(sh._theme_preset)             # reload returns to the file's choice


class TestFirstRun(unittest.TestCase):
    def test_first_run_pending_fires_once(self):
        import os
        import tempfile
        sh, _ = _make_shell()
        flag = os.path.join(tempfile.mkdtemp(prefix="bob-onboard-"), ".onboarded")
        self.assertTrue(sh._first_run_pending(flag))    # first time
        self.assertFalse(sh._first_run_pending(flag))   # then never again


class TestGate(unittest.TestCase):
    def test_non_interactive_run_prints_help_not_shell(self):
        # Under the test runner stdout/stdin are captured, not a TTY.
        self.assertFalse(shellmod.is_interactive())
        # module-level run() must refuse and return 0 (help), never construct/enter the REPL.
        self.assertEqual(shellmod.run(fake_config()), 0)


if __name__ == "__main__":
    unittest.main()
