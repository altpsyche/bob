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
        self.assertIn(sh.session_id, s)
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

    def test_session_new_and_clear(self):
        sh, _ = _make_shell()
        old = sh.session_id
        sh.history = [{"role": "user", "content": "x"}]
        sh.dispatch("/session new")
        self.assertNotEqual(sh.session_id, old)
        self.assertEqual(sh.history, [])
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
