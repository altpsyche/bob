"""fabric_run (scripts/tools/fabric.py): runs the resolved binary (repo bin/ with the OS-correct name,
else PATH), and a pattern name can never become a fabric option."""
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401  (puts scripts/ + scripts/tools on sys.path)
import fabric
import osenv


class TestResolve(unittest.TestCase):
    def test_repo_binary_uses_os_exe_name(self):
        for os_name, name in (("linux", "fabric"), ("windows", "fabric.exe")):
            with mock.patch.object(osenv, "os_name", return_value=os_name):
                self.assertEqual(osenv.bin_exe("fabric").name, name)

    def test_repo_binary_preferred_over_path(self):
        staged = mock.Mock(exists=lambda: True, __str__=lambda s: "/repo/bin/fabric")
        with mock.patch.object(osenv, "bin_exe", return_value=staged), \
             mock.patch("shutil.which", return_value="/usr/bin/fabric"):
            self.assertEqual(fabric.resolve_fabric(), "/repo/bin/fabric")

    def test_path_fallback_and_absent(self):
        missing = Path("/nonexistent/bin/fabric")
        with mock.patch.object(osenv, "bin_exe", return_value=missing), \
             mock.patch("shutil.which", return_value="/usr/bin/fabric"):
            self.assertEqual(fabric.resolve_fabric(), "/usr/bin/fabric")
        with mock.patch.object(osenv, "bin_exe", return_value=missing), \
             mock.patch("shutil.which", return_value=None):
            self.assertEqual(fabric.resolve_fabric(), "")


class TestRun(unittest.TestCase):
    def setUp(self):
        self._saved = fabric._fabric_bin
        fabric._fabric_bin = "/repo/bin/fabric"

    def tearDown(self):
        fabric._fabric_bin = self._saved

    def test_runs_the_resolved_path(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="ok", stderr="")) as run:
            self.assertEqual(fabric._fabric_run("summarize", "text"), "ok")
        self.assertEqual(run.call_args[0][0], ["/repo/bin/fabric", "--pattern", "summarize",
                                               "--vendor", "LiteLLM", "--model", "coder"])

    def test_vendor_and_model_match_fabric_setup(self):
        # The agent pins the vendor/model `bob fabric-setup` writes, whatever DEFAULT_VENDOR the user chose.
        import build
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="ok", stderr="")) as run:
            fabric._fabric_run("summarize", "text")
        argv = run.call_args[0][0]
        self.assertEqual(argv[argv.index("--vendor") + 1], build._FABRIC_VENDOR)
        self.assertEqual(argv[argv.index("--model") + 1], build._FABRIC_MODEL)

    def test_option_shaped_pattern_refused(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("must not run")):
            for bad in ("--help", "-o/tmp/x", "../../etc", "a b", ""):
                self.assertIn("invalid pattern", fabric._fabric_run(bad, "text"), bad)

    def test_not_found_message(self):
        fabric._fabric_bin = ""
        self.assertIn("fabric not found", fabric._fabric_run("summarize", "x"))


if __name__ == "__main__":
    unittest.main()
