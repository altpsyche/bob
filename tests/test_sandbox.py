"""OS-level sandbox: policy resolvers, backend selection, arg-builder shape (pure, cross-OS),
shell wiring (mocked), and real-confinement integration tests guarded by backend availability."""
import shutil
import subprocess
import unittest

import _common  # noqa: F401 — puts scripts/ + scripts/tools/ on sys.path
import osenv
import sandbox


class TestPolicyResolvers(unittest.TestCase):
    def test_mode_default_off(self):
        self.assertEqual(sandbox.sandbox_mode({}), sandbox.SANDBOX_OFF)
        self.assertEqual(sandbox.sandbox_mode({"agent": {}}), sandbox.SANDBOX_OFF)

    def test_mode_on_case_insensitive(self):
        self.assertEqual(sandbox.sandbox_mode({"agent": {"sandbox": "on"}}), sandbox.SANDBOX_ON)
        self.assertEqual(sandbox.sandbox_mode({"agent": {"sandbox": "ON"}}), sandbox.SANDBOX_ON)

    def test_mode_garbage_is_off(self):
        self.assertEqual(sandbox.sandbox_mode({"agent": {"sandbox": "yes-please"}}),
                         sandbox.SANDBOX_OFF)

    def test_limits_defaults(self):
        lim = sandbox.sandbox_limits({})
        self.assertEqual(lim["cpu_seconds"], 30)
        self.assertEqual(lim["memory_mb"], 2048)
        self.assertFalse(lim["network"])
        self.assertEqual(lim["allow_roots"], [str(osenv.REPO)])   # None -> repo workspace

    def test_limits_explicit(self):
        lim = sandbox.sandbox_limits({"agent": {"sandboxLimits": {
            "cpuSeconds": 5, "memoryMB": 256, "allowRoots": ["/work"], "network": True}}})
        self.assertEqual(lim["cpu_seconds"], 5)
        self.assertEqual(lim["memory_mb"], 256)
        self.assertTrue(lim["network"])
        self.assertEqual(lim["allow_roots"], ["/work"])

    def test_limits_explicit_empty_roots(self):
        lim = sandbox.sandbox_limits({"agent": {"sandboxLimits": {"allowRoots": []}}})
        self.assertEqual(lim["allow_roots"], [])   # explicit [] -> only tmpfs /tmp is writable


class TestBackendSelection(unittest.TestCase):
    def setUp(self):
        self._is_win = osenv.is_windows
        self._which = sandbox.shutil.which

    def tearDown(self):
        osenv.is_windows = self._is_win
        sandbox.shutil.which = self._which

    def _linux_with(self, present):
        osenv.is_windows = lambda: False
        sandbox.shutil.which = lambda name: f"/usr/bin/{name}" if name in present else None

    def test_prefers_bwrap(self):
        self._linux_with({"bwrap", "nsjail", "unshare"})
        self.assertEqual(sandbox.backend_name(), "bwrap")

    def test_falls_back_nsjail_then_unshare(self):
        self._linux_with({"nsjail", "unshare"})
        self.assertEqual(sandbox.backend_name(), "nsjail")
        self._linux_with({"unshare"})
        self.assertEqual(sandbox.backend_name(), "unshare")

    def test_none_when_nothing(self):
        self._linux_with(set())
        self.assertIsNone(sandbox.backend_name())
        self.assertFalse(sandbox.available())

    def test_run_sandboxed_fails_closed_without_backend(self):
        self._linux_with(set())
        with self.assertRaises(sandbox.SandboxUnavailable):
            sandbox.run_sandboxed(["echo", "hi"], limits={})


class TestArgBuilders(unittest.TestCase):
    """Pure argv shape — no execution, so these run on every OS."""

    def test_bwrap_no_network_by_default(self):
        argv = sandbox._bwrap_argv(["sh", "-c", "echo hi"],
                                   {"allow_roots": [], "network": False}, cwd=None)
        self.assertEqual(argv[0], "bwrap")
        self.assertIn("--unshare-net", argv)
        self.assertEqual(argv[-3:], ["sh", "-c", "echo hi"])   # command trails
        self.assertIn("--", argv)

    def test_bwrap_network_opt_in(self):
        argv = sandbox._bwrap_argv(["true"], {"allow_roots": [], "network": True}, cwd=None)
        self.assertNotIn("--unshare-net", argv)

    def test_bwrap_binds_allow_roots(self):
        from pathlib import Path
        root = str(Path(osenv.REPO).resolve())   # match the builder's own resolve()
        argv = sandbox._bwrap_argv(["true"], {"allow_roots": [root], "network": False}, cwd=None)
        self.assertIn("--bind", argv)
        self.assertIn(root, argv)          # workspace is rw-bound
        # $HOME is never bound -> secrets absent. (We only assert repo present; home absence is
        # structural — no --bind for it is generated.)

    def test_nsjail_shape(self):
        argv = sandbox._nsjail_argv(["true"], {"allow_roots": [], "network": False,
                                               "cpu_seconds": 7, "memory_mb": 64}, cwd=None)
        self.assertEqual(argv[0], "nsjail")
        self.assertIn("--rlimit_cpu", argv)
        self.assertEqual(argv[-1], "true")

    def test_unshare_shape(self):
        argv = sandbox._unshare_argv(["true"], {"network": False})
        self.assertEqual(argv[0], "unshare")
        self.assertIn("--net", argv)       # empty net ns -> no external network
        self.assertEqual(argv[-1], "true")


class TestRunCommand(unittest.TestCase):
    """sandbox.run_command is the shared seam: sandboxed when on, in-process when off, fail-closed."""

    def setUp(self):
        self._orig_run = sandbox.subprocess.run
        self._orig_sb = sandbox.run_sandboxed

    def tearDown(self):
        sandbox.subprocess.run = self._orig_run
        sandbox.run_sandboxed = self._orig_sb

    def test_off_uses_subprocess(self):
        calls = {"run": 0, "sb": 0}
        sandbox.subprocess.run = lambda argv, **kw: (calls.__setitem__("run", calls["run"] + 1)
                                                     or subprocess.CompletedProcess(argv, 0, "OUT", ""))
        sandbox.run_sandboxed = lambda *a, **k: calls.__setitem__("sb", calls["sb"] + 1)
        r = sandbox.run_command(["echo", "hi"], {"agent": {"sandbox": "off"}})
        self.assertEqual(r.stdout, "OUT")
        self.assertEqual(calls, {"run": 1, "sb": 0})

    def test_on_uses_sandbox(self):
        seen = {}
        sandbox.run_sandboxed = lambda argv, **kw: (seen.__setitem__("argv", argv)
                                                    or subprocess.CompletedProcess(argv, 0, "SB", ""))
        sandbox.subprocess.run = lambda *a, **k: self.fail("in-process used under sandbox=on")
        r = sandbox.run_command(["echo", "hi"], {"agent": {"sandbox": "on"}})
        self.assertEqual(r.stdout, "SB")
        self.assertEqual(seen["argv"], ["echo", "hi"])

    def test_on_fails_closed_without_backend(self):
        def boom(*a, **k):
            raise sandbox.SandboxUnavailable("no backend")

        sandbox.run_sandboxed = boom
        with self.assertRaises(sandbox.SandboxUnavailable):
            sandbox.run_command(["echo", "hi"], {"agent": {"sandbox": "on"}})


class TestShellWiring(unittest.TestCase):
    """shell._shell_run routes through sandbox.run_command; off is in-process, on is sandboxed,
    and an unavailable backend under sandbox=on is refused (never a silent unsandboxed run)."""

    def setUp(self):
        import shell
        self.shell = shell
        self._orig_cfg = shell._cfg
        self._orig_rc = sandbox.run_command

    def tearDown(self):
        self.shell._cfg = self._orig_cfg
        sandbox.run_command = self._orig_rc

    def test_off_returns_output(self):
        self.shell._cfg = {"agent": {"sandbox": "off"}}
        sandbox.run_command = lambda argv, cfg, **kw: subprocess.CompletedProcess(argv, 0, "OUT", "")
        self.assertEqual(self.shell._shell_run("whatever"), "OUT")

    def test_on_returns_sandbox_output(self):
        seen = {}
        self.shell._cfg = {"agent": {"sandbox": "on"}}

        def fake_rc(argv, cfg, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "SBOUT", "")

        sandbox.run_command = fake_rc
        out = self.shell._shell_run("echo hi")
        self.assertEqual(out, "SBOUT")
        self.assertIn("echo hi", seen["argv"][-1])

    def test_on_fails_closed_without_backend(self):
        def boom(*a, **k):
            raise sandbox.SandboxUnavailable("no backend")

        self.shell._cfg = {"agent": {"sandbox": "on"}}
        sandbox.run_command = boom
        out = self.shell._shell_run("echo hi")
        self.assertIn("refused", out)
        self.assertIn("no backend", out)


@unittest.skipUnless(shutil.which("bwrap"), "bwrap not present — real-confinement tests skipped")
class TestLinuxConfinement(unittest.TestCase):
    """Real bwrap runs — filesystem deny-by-default + rlimit kill. Only where bwrap exists."""

    def _run(self, cmd, limits):
        argv = ["sh", "-c", cmd]
        return sandbox.run_sandboxed(argv, timeout=15, limits=limits)

    def test_cannot_write_outside_allow_roots(self):
        # /etc is ro-bound; a write there must fail (non-zero exit).
        r = self._run("echo x > /etc/bob_pwned 2>&1", {"allow_roots": [], "network": False,
                                                        "cpu_seconds": 10, "memory_mb": 512})
        self.assertNotEqual(r.returncode, 0)

    def test_home_secrets_absent(self):
        # $HOME is never bound -> ~/.ssh doesn't exist in the sandbox namespace.
        r = self._run("test -e ~/.ssh && echo FOUND || echo ABSENT",
                      {"allow_roots": [], "network": False, "cpu_seconds": 10, "memory_mb": 512})
        self.assertIn("ABSENT", (r.stdout or "") + (r.stderr or ""))


if __name__ == "__main__":
    unittest.main()
