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
    def test_endpoint_down_still_shows_services(self):
        # Regression: status must NOT bail when inference is down — the whole point is to see the rest
        # (SearXNG/n8n/WebUI) in one place. Endpoint reads [down] but the Services table still renders.
        with mock.patch.object(stack, "_http_json", side_effect=OSError("down")), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False):
            out = stack.stack_status(CFG)
        self.assertIn("[down]", out)
        self.assertIn("Services", out)
        for svc in ("endpoint", "whisper", "webui", "searxng", "n8n", "langfuse", "agent-api"):
            self.assertIn(svc, out)

    def test_endpoint_up_marks_loaded_and_shows_full_service_table(self):
        with mock.patch.object(stack, "_http_json", return_value={"data": [{"id": "planner"}]}), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda p, *a, **k: p in (8082, 8888)):
            out = stack.stack_status(CFG)
        self.assertIn("[running]", out)
        self.assertRegex(out, r"planner\s+.*\bloaded\b")
        self.assertRegex(out, r"coder\s+.*\bunloaded\b")
        # Full system view: every component is listed, with per-port up/down.
        self.assertRegex(out, r"UP\s+whisper\s+:8082")    # stt up
        self.assertRegex(out, r"UP\s+searxng\s+:8888")    # searxng up
        self.assertRegex(out, r"down\s+n8n\s+:5678")      # n8n down
        self.assertRegex(out, r"down\s+webui\s+:3000")    # webui down


class TestServiceRegistry(unittest.TestCase):
    """SERVICES is the ONE source of truth — the name-kill list, the ps list, and the health table are
    all derived from it, not maintained as separate copies."""

    def test_derived_lists_come_from_registry(self):
        self.assertEqual(stack._NAME_KILL,
                         [p for s in stack.SERVICES for p in s.get("procnames", ())])
        self.assertEqual(stack._PS_SERVICES,
                         [s["name"] for s in stack.SERVICES if s.get("pidfile")])

    def test_health_lists_every_registered_service(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=False):
            out = "\n".join(stack._service_health_lines(CFG))
        for s in stack.SERVICES:
            self.assertIn(s.get("label", s["name"]), out)   # every service shows up in the dashboard

    def test_every_service_has_a_start_hint(self):
        for s in stack.SERVICES:
            self.assertTrue(s.get("hint"), f"{s['name']} needs a start hint for the actionable dashboard")

    def test_down_lines_show_start_hint_up_lines_show_url(self):
        # S4 — actionable: down services show how to start them; up services show their URL.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False):
            down = "\n".join(stack._service_health_lines(CFG))
        self.assertIn("→ start: bob services start", down)   # e.g. searxng/n8n/langfuse when down
        self.assertIn("→ start: bob whisper start", down)
        with mock.patch.object(osenv, "is_port_in_use", return_value=True):
            up = "\n".join(stack._service_health_lines(CFG))
        self.assertIn("http://localhost:8081", up)           # litellm URL shown when up
        self.assertNotIn("→ start:", up)


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


class TestSwapLaunchSpec(unittest.TestCase):
    """S2 — the llama-swap launch (exe path, config path, --listen addr, LLAMA_LOCAL_ROOT) lives in ONE
    place (_swap_launch), consumed by both the background and foreground starts so they can't drift."""

    def test_spec_shape(self):
        with mock.patch.object(osenv, "bin_exe", return_value=Path("/x/llama-swap")):
            exe, argv, env_add, port = stack._swap_launch(CFG)
        self.assertEqual(port, 8080)
        self.assertTrue(argv[0].endswith("llama-swap"))
        self.assertIn("--config", argv)
        self.assertIn("--listen", argv)
        self.assertIn("127.0.0.1:8080", argv)
        self.assertIn("LLAMA_LOCAL_ROOT", env_add)

    def test_both_starts_funnel_through_the_one_spec(self):
        # _swap_launch runs BEFORE the exe-exists guard in both callers, so with the binary absent each
        # path returns early (no real process/port touched) yet still proves it used the shared spec.
        with mock.patch.object(osenv, "bin_exe", return_value=Path("/nonexistent/llama-swap")), \
             mock.patch.object(stack, "_swap_launch", wraps=stack._swap_launch) as spec:
            ok, lines = stack._start_endpoint_bg(CFG)
            self.assertFalse(ok)
            self.assertTrue(spec.called)                      # background start used _swap_launch
            spec.reset_mock()
            with mock.patch.object(stack, "_ensure_configs", return_value=""):
                rc = stack.serve_foreground(CFG)
            self.assertEqual(rc, 1)
            self.assertTrue(spec.called)                      # foreground start used the SAME spec


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
            out = stack.service_control(CFG, "litellm", "status")
        self.assertIn("running", out)
        self.assertIn("PID=200", out)
        self.assertIn(":8081", out)

    def test_status_not_running(self):
        self.assertIn("not running", stack.service_control(CFG, "litellm", "status"))

    def test_unknown_service_is_reported(self):
        self.assertIn("Unknown service", stack.service_control(CFG, "nope", "status"))

    def test_stop_when_alive(self):
        (self.logs / "whisper.pid").write_text("300")
        with mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "stop_process_tree") as tree:
            out = stack.service_control(CFG, "whisper", "stop")
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
