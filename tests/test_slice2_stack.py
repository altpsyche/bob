"""ONE-C Slice 2 — lifecycle capabilities (scripts/tools/stack.py).

The launch/stop primitives are validated end-to-end against a real service elsewhere; here we cover the
pure logic hermetically — the ps table, the teardown bookkeeping, the bounded log read, per-service
status/stop, the config-regen bridge, and the registry wiring — mocking osenv/subprocess so nothing
touches real processes, ports, or Docker."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
from bob import cli, registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import stack  # noqa: E402
import osenv  # noqa: E402

CFG = {"port": 8080, "litellmPort": 8081, "sttPort": 8082, "ttsPort": 8083, "webuiPort": 3000,
       "langfusePort": 3001, "searxngPort": 8888, "n8nPort": 5678, "voice": {"enabled": False}}


class TestRegistryWiring(unittest.TestCase):
    LIFECYCLE = ["up", "serve", "restart", "stop", "status", "ps", "logs", "webui",
                 "litellm", "whisper", "piper", "services"]

    def test_verbs_flipped_to_python_with_handlers(self):
        by_name = registry.by_name()
        for verb in self.LIFECYCLE:
            entry = by_name[verb]
            self.assertTrue(entry.get("handler"), f"{verb} runtime")
            self.assertIn(entry["handler"], cli._HANDLERS, f"{verb} handler")

    def test_down_alias_removed(self):
        # D1 — "one clean way, no silly alias." `down` is no longer a command.
        self.assertNotIn("down", registry.by_name())

    def test_agent_tools_registered_and_mutating(self):
        self.assertEqual(set(stack.DISPATCH), {
            "stack_up", "stack_stop", "stack_restart", "stack_status", "stack_ps", "stack_logs",
            "litellm_control", "whisper_control", "piper_control", "services_control"})
        self.assertNotIn("stack_status", stack.MUTATING_TOOLS)  # read-only
        # ps/logs are read-only; the rest mutate.
        self.assertNotIn("stack_ps", stack.MUTATING_TOOLS)
        self.assertNotIn("stack_logs", stack.MUTATING_TOOLS)
        self.assertIn("stack_up", stack.MUTATING_TOOLS)
        self.assertIn("stack_stop", stack.MUTATING_TOOLS)


class TestPs(unittest.TestCase):
    def test_table_shows_running_dead_and_absent(self):
        pids = {"llama-swap": 100, "litellm": 200}  # others absent
        stats = {100: {"rss_mb": 50, "uptime": "0:01:00"}, 200: None}  # 200 = stale
        with mock.patch.object(stack, "_read_pid", side_effect=lambda s: pids.get(s)), \
             mock.patch.object(osenv, "process_stats", side_effect=lambda p: stats.get(p)):
            out = stack.stack_ps(CFG)
        self.assertIn("llama-swap", out)
        self.assertIn("50 MB", out)
        self.assertRegex(out, r"llama-swap\s+100\s+50 MB\s+0:01:00\s+running")
        self.assertRegex(out, r"litellm\s+200\s+--\s+--\s+dead \(stale PID file\)")
        self.assertRegex(out, r"whisper\s+--\s+--\s+--\s+not running")


class TestStatus(unittest.TestCase):
    def test_endpoint_down_bails(self):
        # stack orchestration polls via stdlib urllib now (no requests dep) — mock the _http_json helper.
        with mock.patch.object(stack, "_http_json", side_effect=OSError("down")):
            self.assertEqual(stack.stack_status(CFG), "Endpoint not running. Start with: bob serve")

    def test_endpoint_up_marks_loaded_and_probes_voice(self):
        with mock.patch.object(stack, "_http_json", return_value={"data": [{"id": "planner"}]}), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda p, *a, **k: p == 8082):
            out = stack.stack_status(CFG)
        self.assertIn("[running]", out)
        self.assertRegex(out, r"planner\s+.*\bloaded\b")
        self.assertRegex(out, r"coder\s+.*\bunloaded\b")
        self.assertIn("whisper", out)
        self.assertIn("UP (port 8082)", out)     # stt up
        self.assertIn("down (port 8083)", out)   # tts down


class TestWebuiForeground(unittest.TestCase):
    """`bob webui` must not crash on a bind error when WebUI is already up (e.g. from `bob up`)."""

    def _webui_exe(self):
        exe = mock.Mock()
        exe.exists.return_value = True
        return exe

    def test_already_running_points_and_skips_serve(self):
        with mock.patch.object(osenv, "venv_exe", return_value=self._webui_exe()), \
             mock.patch.object(osenv, "is_port_in_use", return_value=True), \
             mock.patch.object(osenv, "open_url") as open_url, \
             mock.patch.object(stack.subprocess, "run") as run:
            rc = stack.webui_foreground(CFG)
        self.assertEqual(rc, 0)
        run.assert_not_called()            # no doomed foreground bind on the occupied port
        open_url.assert_called_once()      # pointed the user at the running instance

    def test_serves_when_port_free(self):
        with mock.patch.object(osenv, "venv_exe", return_value=self._webui_exe()), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            rc = stack.webui_foreground(CFG)
        self.assertEqual(rc, 0)
        run.assert_called_once()           # free port -> actually serve in the foreground


class TestStop(unittest.TestCase):
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp(prefix="bob-logs-"))
        self._patch = mock.patch.object(stack, "_logs_dir", return_value=self.logs)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_stops_named_and_pidfile_services_and_cleans_pids(self):
        (self.logs / "litellm.pid").write_text("200")
        (self.logs / "llama-swap.pid").write_text("100")
        with mock.patch.object(osenv, "stop_processes_by_name", return_value=["llama-swap"]) as sk, \
             mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "stop_process_tree") as tree, \
             mock.patch.object(stack.shutil, "which", return_value=None):  # no docker
            out = stack.stack_stop(CFG)
        sk.assert_called_once_with(stack._NAME_KILL)
        self.assertIn("Stopped:", out)
        self.assertIn("llama-swap", out)
        self.assertIn("litellm", out)
        tree.assert_called()  # litellm pid tree-killed
        self.assertFalse((self.logs / "litellm.pid").exists())   # pidfiles cleaned
        self.assertFalse((self.logs / "llama-swap.pid").exists())

    def test_nothing_running(self):
        with mock.patch.object(osenv, "stop_processes_by_name", return_value=[]), \
             mock.patch.object(osenv, "pid_alive", return_value=False), \
             mock.patch.object(stack.shutil, "which", return_value=None):
            out = stack.stack_stop(CFG)
        self.assertEqual(out, "Nothing was running.")

    def test_open_webui_reaped_by_name_without_pidfile(self):
        # Regression: WebUI must be name-killed even with NO open-webui.pid (a prior stop unlinks it),
        # else a reparented WebUI keeps holding :3000 and no later `bob stop` can find it.
        self.assertIn("open-webui", stack._NAME_KILL)
        with mock.patch.object(osenv, "stop_processes_by_name", return_value=["open-webui"]) as sk, \
             mock.patch.object(osenv, "pid_alive", return_value=False), \
             mock.patch.object(stack.shutil, "which", return_value=None):
            out = stack.stack_stop(CFG)   # no open-webui.pid on disk
        sk.assert_called_once_with(stack._NAME_KILL)
        self.assertIn("open-webui", out)


class TestLogs(unittest.TestCase):
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp(prefix="bob-logs-"))
        mock.patch.object(stack, "_logs_dir", return_value=self.logs).start()
        self.addCleanup(mock.patch.stopall)

    def test_bounded_tail(self):
        (self.logs / "llama-swap.log").write_text("\n".join(f"line{i}" for i in range(100)))
        out = stack.stack_logs(CFG, lines=10)
        self.assertIn("line99", out)
        self.assertIn("line90", out)
        self.assertNotIn("line89", out)

    def test_missing_log_hint(self):
        out = stack.stack_logs(CFG)
        self.assertIn("No log file yet", out)


class TestServiceControl(unittest.TestCase):
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp(prefix="bob-logs-"))
        mock.patch.object(stack, "_logs_dir", return_value=self.logs).start()
        self.addCleanup(mock.patch.stopall)

    def test_status_running(self):
        (self.logs / "litellm.pid").write_text("200")
        with mock.patch.object(osenv, "process_stats", return_value={"rss_mb": 10, "uptime": "0:00:30"}):
            out = stack.litellm_control(CFG, "status")
        self.assertIn("running", out)
        self.assertIn("PID=200", out)
        self.assertIn(":8081", out)

    def test_status_not_running(self):
        self.assertIn("not running", stack.litellm_control(CFG, "status"))

    def test_stop_when_alive(self):
        (self.logs / "whisper.pid").write_text("300")
        with mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "stop_process_tree") as tree:
            out = stack.whisper_control(CFG, "stop")
        tree.assert_called_once()
        self.assertIn("stopped", out)
        self.assertFalse((self.logs / "whisper.pid").exists())


class TestConfigBridge(unittest.TestCase):
    def test_regen_delegates_to_python_generators(self):
        # ONE-C Slice 6 — the regen bridge runs the Python generators (no pwsh); _regen_configs is a
        # thin delegate to bob_models.regenerate_configs.
        import bob_models
        with mock.patch.object(bob_models, "regenerate_configs", return_value=True) as rc:
            self.assertTrue(stack._regen_configs())
        rc.assert_called_once()

    def test_ensure_configs_ok_when_yaml_present(self):
        # Isolated: config/llama-swap.yaml is gitignored (absent on a fresh checkout / in CI), so stand up
        # a temp repo with the file present and assert regen-not-needed returns "". (Was flaky — it relied
        # on the committed-locally-but-gitignored file existing.)
        repo = Path(tempfile.mkdtemp(prefix="bob-cfgok-"))
        (repo / "config").mkdir()
        (repo / "config" / "llama-swap.yaml").write_text("models: {}\n", encoding="utf-8")
        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(stack, "_regen_configs", return_value=False):
            self.assertEqual(stack._ensure_configs(), "")

    def test_ensure_configs_errors_when_missing(self):
        empty = Path(tempfile.mkdtemp(prefix="bob-norepo-"))
        with mock.patch.object(stack, "REPO", empty), \
             mock.patch.object(stack, "_regen_configs", return_value=False):
            err = stack._ensure_configs()
        self.assertIn("llama-swap.yaml", err)
        self.assertIn("gen", err.lower())


if __name__ == "__main__":
    unittest.main()
