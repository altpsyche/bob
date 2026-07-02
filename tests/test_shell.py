"""NB3/NE0 — shell_run runs in the OS-native shell (osenv.default_shell). Approval is now handled by
the agent loop's event-driven approve callback (shell declares REQUIRES_APPROVAL), NOT a blocking
stdin prompt inside the tool — so shell_run works under the TUI/server, not only a console."""
import unittest
from unittest import mock

import _common  # noqa: F401 — puts scripts/tools on sys.path
import osenv
import shell


class TestShellRun(unittest.TestCase):
    def test_declares_requires_approval(self):
        # NE0: the loop reads this flag (ToolRegistry.approval_required_tools) to gate the tool.
        self.assertTrue(getattr(shell, "REQUIRES_APPROVAL", False))

    def test_runs_without_a_stdin_prompt(self):
        # The tool must NOT prompt on stdin anymore; input() being called would be a regression.
        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _no_input(*a, **k):
            raise AssertionError("shell_run must not call input(); approval is loop-level now")

        with mock.patch("builtins.input", side_effect=_no_input), \
             mock.patch("shell.subprocess.run", return_value=_R()):
            self.assertEqual(shell._shell_run("echo hi"), "ok")

    def test_builds_os_native_argv(self):
        captured = {}

        class _R:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def _fake_run(argv, **kw):
            captured["argv"] = argv
            return _R()

        with mock.patch("shell.subprocess.run", side_effect=_fake_run):
            out = shell._shell_run("echo hi")
        self.assertEqual(out, "ok")
        # wiring: default_shell() prefix + the command string, whatever the host OS
        self.assertEqual(captured["argv"], osenv.default_shell() + ["echo hi"])


if __name__ == "__main__":
    unittest.main()
