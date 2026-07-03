"""O5 — OS-level sandbox: policy resolvers, backend selection, arg-builder shape (pure, cross-OS),
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


class TestShellWiring(unittest.TestCase):
    """shell._execute routes to the sandbox only when on; off is in-process (pre-O5)."""

    def setUp(self):
        import shell
        self.shell = shell
        self._orig_cfg = shell._cfg
        self._orig_run = shell.subprocess.run
        self._orig_sb = shell.sandbox.run_sandboxed

    def tearDown(self):
        self.shell._cfg = self._orig_cfg
        self.shell.subprocess.run = self._orig_run
        self.shell.sandbox.run_sandboxed = self._orig_sb

    def test_off_uses_in_process(self):
        calls = {"run": 0, "sb": 0}

        def fake_run(argv, **kw):
            calls["run"] += 1
            return subprocess.CompletedProcess(argv, 0, stdout="OUT", stderr="")

        self.shell._cfg = {"agent": {"sandbox": "off"}}
        self.shell.subprocess.run = fake_run
        self.shell.sandbox.run_sandboxed = lambda *a, **k: calls.__setitem__("sb", calls["sb"] + 1)
        out = self.shell._shell_run("whatever")
        self.assertEqual(out, "OUT")
        self.assertEqual(calls, {"run": 1, "sb": 0})

    def test_on_uses_sandbox(self):
        seen = {}

        def fake_sb(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="SBOUT", stderr="")

        self.shell._cfg = {"agent": {"sandbox": "on"}}
        self.shell.sandbox.run_sandboxed = fake_sb
        self.shell.subprocess.run = lambda *a, **k: self.fail("in-process run used under sandbox=on")
        out = self.shell._shell_run("echo hi")
        self.assertEqual(out, "SBOUT")
        self.assertTrue(seen["argv"][-1].endswith("echo hi") or "echo hi" in seen["argv"][-1])

    def test_on_fails_closed_without_backend(self):
        def boom(*a, **k):
            raise sandbox.SandboxUnavailable("no backend")

        self.shell._cfg = {"agent": {"sandbox": "on"}}
        self.shell.sandbox.run_sandboxed = boom
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
