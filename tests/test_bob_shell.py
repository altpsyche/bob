"""NE2 (WI-5) — the interactive shell (scripts/bob/shell.py), exercised headlessly: splash string,
slash-command parsing, the event render loop against a FAKE run_agent_events (tokens/tool_call/
tool_result/final/error), the NE0 approval handshake (worker thread ↔ main thread), and the isatty
gate. A prompt_toolkit REPL can't be validated without a real TTY, so live keystroke behaviour (Ctrl-C
cancel, the real approval prompt) is the separate manual acceptance — here we prove everything under
it."""
import io
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
        self.assertIsNone(sh.session_id)       # WI-6: no persisted row until the first turn
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


class TestVoiceMode(unittest.TestCase):
    """ONE-B4 — /voice loop glue: mic→STT→_run_turn→TTS, faked end to end. The turn path itself is the
    same _run_turn the text tests cover; here we prove the round-trip wiring, exit conditions, and edges."""

    def _voice_shell(self, transcripts):
        """A shell whose bob_voice is stubbed: listen() pops from `transcripts` (a KeyboardInterrupt or
        RuntimeError value is raised instead of returned), _run_turn echoes, speak records what it spoke."""
        import bob_voice
        sh, out = _make_shell()
        spoken, turns = [], []
        seq = list(transcripts)

        def fake_listen(config, silence_sec=None):
            if not seq:
                raise KeyboardInterrupt      # nothing left → simulate Ctrl-C to leave
            item = seq.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        sh._run_turn = lambda g: (turns.append(g), f"reply to {g}")[1]
        self._patches = [
            _patch(bob_voice, "stt_ready", lambda cfg: True),
            _patch(bob_voice, "listen", fake_listen),
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

    def test_blank_transcript_keeps_listening(self):
        sh, _out, spoken, turns = self._voice_shell(["", "  ", "real"])
        sh.dispatch("/voice")
        self.assertEqual(turns, ["real"])         # empties skipped, no turn/speak for them

    def test_stt_down_autostarts_whisper(self):
        # New behavior: /voice auto-starts whisper (like chat's auto-start) instead of telling the user
        # to run `bob whisper`. Here the start "succeeds" (stt_ready flips True), so the loop proceeds.
        import bob_voice
        import stack
        sh, out = _make_shell()
        ready = iter([False, True])          # down at preflight, up after whisper_control
        started = []
        with _patch(bob_voice, "stt_ready", lambda cfg: next(ready)), \
             _patch(stack, "service_control", lambda cfg, name, action="start": started.append(action) or "ok"), \
             _patch(bob_voice, "listen", lambda cfg, silence_sec=None: (_ for _ in ()).throw(KeyboardInterrupt())):
            sh.dispatch("/voice")
        self.assertEqual(started, ["start"])           # it tried to bring STT up itself
        self.assertNotIn("bob whisper", out.file.getvalue())

    def test_stt_still_down_after_autostart_points_to_setup(self):
        import bob_voice
        import stack
        sh, out = _make_shell()
        with _patch(bob_voice, "stt_ready", lambda cfg: False), \
             _patch(stack, "service_control", lambda cfg, name, action="start": "ok"):
            sh.dispatch("/voice")
        self.assertIn("bob setup-voice", out.file.getvalue())   # honest next step when it can't start

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
    """S5 — lifecycle controls from inside the shell (the cockpit): /up, /restart, /webui, all routed
    to the one stack.* core so you never drop to raw `bob` verbs to manage the system."""

    def test_up_routes_to_stack_up(self):
        import stack
        sh, out = _make_shell()
        calls = {}
        with _patch(stack, "stack_up", lambda cfg, open_browser=True, with_services=False:
                    calls.update(ob=open_browser, ws=with_services) or "brought up"):
            sh.dispatch("/up")
        self.assertEqual((calls["ob"], calls["ws"]), (True, False))
        self.assertIn("brought up", out.file.getvalue())

    def test_up_flags_parsed(self):
        import stack
        sh, _ = _make_shell()
        calls = {}
        with _patch(stack, "stack_up", lambda cfg, open_browser=True, with_services=False:
                    calls.update(ob=open_browser, ws=with_services) or "x"):
            sh.dispatch("/up --with-services --no-open")
        self.assertEqual((calls["ob"], calls["ws"]), (False, True))

    def test_restart_routes_to_stack_restart(self):
        import stack
        sh, out = _make_shell()
        with _patch(stack, "stack_restart", lambda cfg: "restarted"):
            sh.dispatch("/restart")
        self.assertIn("restarted", out.file.getvalue())

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


def _make_persistent_shell(tmpdir, owner="local", max_tokens=0):
    """A shell wired to a real (temp) SessionStore, for the WI-6 persist/resume/budget tests."""
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
        self.assertEqual(captured["scope"], sh.scope)      # MEM-7: exit consolidation carries scope
        self.assertEqual(captured["session_id"], "sid")    # MEM-10: provenance stamp

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
    def test_theme_command_is_readonly_inspector(self):
        # /theme shows the active theme + file path; it must NOT mutate the theme (no live levers).
        sh, out = _make_shell()
        before = sh.theme
        sh.dispatch("/theme")
        self.assertIs(sh.theme, before)                 # unchanged
        self.assertIn("ui.json", out.file.getvalue())   # points the user at the editable file


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
