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
    LIFECYCLE = ["up", "serve", "restart", "stop", "ps", "logs", "webui",
                 "litellm", "whisper", "piper", "services"]

    def test_verbs_flipped_to_python_with_handlers(self):
        by_name = registry.by_name()
        for verb in self.LIFECYCLE:
            entry = by_name[verb]
            self.assertEqual(entry["runtime"], "python", f"{verb} runtime")
            self.assertIn(entry["handler"], cli._HANDLERS, f"{verb} handler")

    def test_down_alias_removed(self):
        # D1 — "one clean way, no silly alias." `down` is no longer a command.
        self.assertNotIn("down", registry.by_name())

    def test_status_stays_pwsh_until_slice4(self):
        self.assertEqual(registry.by_name()["status"]["runtime"], "pwsh")

    def test_agent_tools_registered_and_mutating(self):
        self.assertEqual(set(stack.DISPATCH), {
            "stack_up", "stack_stop", "stack_restart", "stack_ps", "stack_logs",
            "litellm_control", "whisper_control", "piper_control", "services_control"})
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
    def test_regen_noop_without_pwsh(self):
        with mock.patch.object(stack.shutil, "which", return_value=None):
            self.assertFalse(stack._regen_configs())

    def test_ensure_configs_ok_when_yaml_present(self):
        # The real repo has config/llama-swap.yaml locally; regen is best-effort.
        with mock.patch.object(stack, "_regen_configs", return_value=False):
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
