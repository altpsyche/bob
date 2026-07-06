"""POST-ONE Mission 1 — the entry model: auto-start (`_ensure_endpoint`) and the TUI's own lifecycle
slash-commands. All hermetic: no real servers, probes, or launches — check_litellm and the stack module
are monkeypatched."""
import io
import types
import unittest

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
import bob_core
from _common import FakeRegistry, fake_config

from bob import cli


class TestEnsureEndpoint(unittest.TestCase):
    """Auto-start on demand: no relaunch when up (the 401-reads-as-down regression), start + wait when
    down."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_stack = cli._stack

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        cli._stack = self._orig_stack

    def _fake_stack(self, counter):
        # Auto-start delegates to the one ensure_deps(inference=True) seam (core only), NOT the full
        # stack_up — so `bob chat` no longer silently starts WebUI/whisper. Returns (ok, lines).
        return lambda: types.SimpleNamespace(
            ensure_deps=lambda *a, **k: (counter.__setitem__("up", counter["up"] + 1) or True, []))

    def test_noop_when_already_up(self):
        # Regression guard: LiteLLM answers /v1/models with 401 when up; the old urlopen probe read that
        # as "down" and relaunched on every invocation. check_litellm is a TCP connect, so up => 0 starts.
        bob_core.check_litellm = lambda config=None: True
        counter = {"up": 0}
        cli._stack = self._fake_stack(counter)
        cli._ensure_endpoint(fake_config())
        self.assertEqual(counter["up"], 0)

    def test_starts_core_inference_when_down(self):
        bob_core.check_litellm = lambda config=None: False
        counter = {"up": 0}
        cli._stack = self._fake_stack(counter)
        cli._ensure_endpoint(fake_config())
        self.assertEqual(counter["up"], 1)   # ensure_inference called exactly once (waits internally)

    def test_launch_failure_is_advisory_not_fatal(self):
        bob_core.check_litellm = lambda config=None: False

        def boom():
            return types.SimpleNamespace(
                ensure_deps=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))

        cli._stack = boom
        # Must not raise — a launch failure prints a hint; the turn surfaces the real error later.
        cli._ensure_endpoint(fake_config())


class _FakeSkillReg:
    def __init__(self):
        self.skills = {}
        self.errors = []

    def list(self):
        return []


def _make_shell(**cfg_over):
    from rich.console import Console
    from bob.shell import BobShell

    console = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
    sh = BobShell(fake_config(**cfg_over), FakeRegistry(), _FakeSkillReg(), console=console)
    return sh, console


try:
    import rich  # noqa: F401
except ModuleNotFoundError as _e:  # pragma: no cover
    raise unittest.SkipTest(f"rich not installed: {_e}")

import stack  # scripts/tools on path via _common


class TestShellLifecycle(unittest.TestCase):
    """The TUI is the home base — lifecycle is reachable from inside it (/stop, /logs), and /help is the
    shell's OWN slash reference, not the CLI verb catalog."""

    def setUp(self):
        self._stop = stack.stack_stop
        self._logs = stack.stack_logs

    def tearDown(self):
        stack.stack_stop = self._stop
        stack.stack_logs = self._logs

    def test_slash_stop_calls_stack_stop(self):
        seen = {"stop": 0}
        stack.stack_stop = lambda config: seen.__setitem__("stop", 1) or "stopped"
        sh, out = _make_shell()
        self.assertTrue(sh.dispatch("/stop"))   # keeps looping
        self.assertEqual(seen["stop"], 1)
        self.assertIn("stopped", out.file.getvalue())

    def test_slash_logs_calls_stack_logs_with_count(self):
        seen = {}
        stack.stack_logs = lambda config, n: seen.update(n=n) or "log-tail"
        sh, out = _make_shell()
        sh.dispatch("/logs 12")
        self.assertEqual(seen["n"], 12)
        self.assertIn("log-tail", out.file.getvalue())

    def test_help_is_slash_reference_not_cli_catalog(self):
        sh, out = _make_shell()
        sh.dispatch("/help")
        text = out.file.getvalue()
        self.assertIn("/agent", text)          # a shell slash-command IS shown
        self.assertIn("/stop", text)           # the new lifecycle slash-command
        self.assertIn("bob help", text)        # points at the CLI catalog for outside-terminal use
        self.assertNotIn("setup-voice", text)  # a CLI-only verb is NOT dumped into the shell help


if __name__ == "__main__":
    unittest.main()
