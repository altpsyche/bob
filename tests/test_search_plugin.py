"""search_code / `bob search` (plugins/search): the query can never become a search-tool option, the
searched path goes through the fsguard allowlist + secrets denylist, and the install hint fits the OS."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401  (puts scripts/ + scripts/tools on sys.path)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plugins.search import invoke as search  # noqa: E402
import osenv  # noqa: E402


def _load_tool():
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "plugins" / "search" / "tool.py"
    spec = importlib.util.spec_from_file_location("bob_tool_search_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestArgv(unittest.TestCase):
    def test_rg_query_behind_e_and_options_end_before_path(self):
        with mock.patch("shutil.which", side_effect=lambda t: "/usr/bin/rg" if t == "rg" else None):
            cmd, _ = search._search_argv("--pre=sh", ".py")
        i = cmd.index("-e")
        self.assertEqual(cmd[i + 1], "--pre=sh")
        self.assertEqual(cmd[-2:], ["--", "."])
        self.assertNotIn("--pre=sh", cmd[:i])

    def test_grep_fallback_is_hardened_too(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(osenv, "is_windows", return_value=False):
            cmd, _ = search._search_argv("--include=x", None)
        self.assertEqual(cmd[0], "grep")
        self.assertEqual(cmd[cmd.index("-e") + 1], "--include=x")
        self.assertEqual(cmd[-2:], ["--", "."])

    def test_findstr_fallback_uses_literal_and_no_cmd_shell(self):
        with mock.patch("shutil.which", return_value=None), \
             mock.patch.object(osenv, "is_windows", return_value=True):
            cmd, _ = search._search_argv("a & del x", None)
        self.assertEqual(cmd[0], "findstr")                 # no `cmd /c` re-parsing the query
        self.assertIn("/c:a & del x", cmd)

    def test_bad_extension_rejected(self):
        self.assertIn("invalid extension", search.run_rg("x", ".", "--pre=sh"))


class TestInjectionEndToEnd(unittest.TestCase):
    """Runs the real search tool on this host (rg or grep): a flag-shaped query is only ever text."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-search-"))
        (self.dir / "a.txt").write_text("hello --pre=touch\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_pre_flag_query_executes_nothing(self):
        if not (shutil.which("rg") or shutil.which("grep")) or os.name == "nt":
            self.skipTest("needs rg/grep and a POSIX shell script")
        marker = self.dir / "PWNED"
        script = self.dir / "pre.sh"
        script.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat "$1"\n', encoding="utf-8")
        script.chmod(0o755)
        # As an option, `--pre=<script>` would make rg run the script on every file.
        out = search.run_rg(f"--pre={script}", str(self.dir), None)
        self.assertFalse(marker.exists())
        self.assertIn("no matches", out)
        hit = search.run_rg("--pre=touch", str(self.dir), None)
        self.assertIn("a.txt", hit)


class TestToolGuard(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="bob-search-root-"))
        self.out = Path(tempfile.mkdtemp(prefix="bob-search-out-"))
        (self.root / "notes.txt").write_text("needle here\n", encoding="utf-8")
        (self.root / "config.json").write_text('{"litellmKey": "needle-SECRET"}\n', encoding="utf-8")
        (self.root / "logs").mkdir()
        (self.root / "logs" / "a.log").write_text("needle in log\n", encoding="utf-8")
        (self.out / "x.txt").write_text("needle outside\n", encoding="utf-8")
        self.tool = _load_tool()
        self.tool.configure({"agent": {"allowedReadPaths": [str(self.root)]}})

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.out, ignore_errors=True)

    def test_path_outside_allowlist_refused(self):
        with mock.patch.object(search, "run_rg", side_effect=AssertionError("must not search")):
            self.assertIn("Access denied", self.tool._search_code("needle", str(self.out)))
            self.assertIn("Access denied", self.tool._search_code("needle", "../"))

    def test_secret_dir_refused(self):
        home = Path(tempfile.mkdtemp(prefix="bob-search-home-"))
        try:
            (home / ".ssh").mkdir()
            self.tool.configure({"agent": {"allowedReadPaths": [str(home)]}})
            with mock.patch.object(self.tool, "_home", return_value=home), \
                 mock.patch.object(search, "run_rg", side_effect=AssertionError("must not search")):
                self.assertIn("sensitive", self.tool._search_code("x", str(home / ".ssh")))
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_no_allowlist_refused(self):
        self.tool.configure({"agent": {}})
        self.assertIn("no allowedReadPaths", self.tool._search_code("needle"))

    def test_matches_in_denied_files_withheld(self):
        if not (shutil.which("rg") or shutil.which("grep")):
            self.skipTest("no rg/grep on this host")
        with mock.patch("bob_core.check_litellm", return_value=False):
            out = self.tool._search_code("needle", ".")
        self.assertIn("notes.txt", out)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("config.json", out)
        self.assertNotIn("in log", out)

    def test_filter_handles_nul_and_context_separators(self):
        out = search._filter_denied("a.txt\x001:x\n--\nconfig.json\x002:k\n", "\0", "/r",
                                    lambda p: p.name == "config.json")
        self.assertEqual(out, "a.txt:1:x\n--")


class TestInstallHint(unittest.TestCase):
    def test_windows_hint_is_winget(self):
        with mock.patch.object(osenv, "os_name", return_value="windows"):
            self.assertIn("winget", search.rg_install_hint())

    def test_linux_hint_uses_detected_manager(self):
        with mock.patch.object(osenv, "os_name", return_value="linux"), \
             mock.patch.object(osenv, "linux_package_manager", return_value="apt"):
            hint = search.rg_install_hint()
        self.assertIn("apt-get", hint)
        self.assertIn("ripgrep", hint)
        self.assertNotIn("winget", hint)

    def test_macos_hint_is_brew(self):
        with mock.patch.object(osenv, "os_name", return_value="macos"):
            self.assertIn("brew", search.rg_install_hint())

    def test_missing_tool_message_carries_hint(self):
        d = tempfile.mkdtemp(prefix="bob-search-nf-")
        try:
            with mock.patch("subprocess.run", side_effect=FileNotFoundError()), \
                 mock.patch.object(search, "rg_install_hint", return_value="HINT"):
                self.assertIn("HINT", search.run_rg("x", d, None))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
