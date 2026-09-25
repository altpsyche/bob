"""Lifecycle capabilities (scripts/tools/stack.py).

The launch/stop primitives are validated end-to-end against a real service elsewhere; here we cover the
pure logic hermetically — the ps table, the teardown bookkeeping, the bounded log read, per-service
status/stop, the config-regen bridge, and the agent-tool surface — mocking osenv/subprocess so nothing
touches real processes, ports, or Docker."""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
from bob import registry

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts" / "tools"))
import stack  # noqa: E402
import osenv  # noqa: E402

CFG = {"port": 8080, "litellmPort": 8081, "sttPort": 8082, "ttsPort": 8083, "webuiPort": 3000,
       "langfusePort": 3001, "searxngPort": 8888, "n8nPort": 5678, "voice": {"enabled": False}}

# The key-drift helpers rewrite the real repo's generated configs and Open WebUI's db (generate.REPO and
# stack.REPO), so they are stubbed for the whole module; the tests of them call the saved originals
# against a temp tree. Plain attribute swaps, not mock.patch.start(): _LogsMixin's patch.stopall would
# undo those after its first test.
_REAL_SYNC_KEY_CONFIGS = stack._sync_key_configs
_REAL_WEBUI_SYNC_KEY = stack._webui_sync_key


def setUpModule():
    stack._sync_key_configs = lambda config: []
    stack._webui_sync_key = lambda config: ""


def tearDownModule():
    stack._sync_key_configs = _REAL_SYNC_KEY_CONFIGS
    stack._webui_sync_key = _REAL_WEBUI_SYNC_KEY


class TestStackToolSurface(unittest.TestCase):
    def test_down_alias_removed(self):
        # "one clean way, no silly alias." `down` is no longer a command.
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


class TestEndpointOwnership(unittest.TestCase):
    """endpoint_tracked_pid: does this stack own the running endpoint? (`bob update` restarts only
    what it owns; a foreground `bob serve` writes no pidfile.)"""

    def test_live_pidfile_is_owned(self):
        with mock.patch.object(stack, "_read_pid", return_value=100), \
             mock.patch.object(osenv, "pid_alive", return_value=True):
            self.assertEqual(stack.endpoint_tracked_pid(), 100)

    def test_no_pidfile_is_unowned(self):
        # A foreground `bob serve` — running, but nothing tracks it.
        with mock.patch.object(stack, "_read_pid", return_value=None):
            self.assertIsNone(stack.endpoint_tracked_pid())

    def test_stale_pidfile_is_unowned(self):
        with mock.patch.object(stack, "_read_pid", return_value=100), \
             mock.patch.object(osenv, "pid_alive", return_value=False):
            self.assertIsNone(stack.endpoint_tracked_pid())


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
        with mock.patch.object(stack, "_http_json", return_value={"data": [{"id": "ponder"}]}), \
             mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda p, *a, **k: p in (8082, 8888)):
            out = stack.stack_status(CFG)
        self.assertIn("[running]", out)
        self.assertRegex(out, r"ponder\s+.*\bloaded\b")
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
                         [s["name"] for s in stack.SERVICES if s.get("kind") == "native"])

    def test_health_lists_every_registered_service(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=False):
            out = "\n".join(stack._service_health_lines(CFG))
        for s in stack.SERVICES:
            self.assertIn(s.get("label", s["name"]), out)   # every service shows up in the dashboard

    def test_every_service_has_a_start_hint(self):
        for s in stack.SERVICES:
            self.assertTrue(s.get("hint"), f"{s['name']} needs a start hint for the actionable dashboard")

    def test_down_lines_show_start_hint_up_lines_show_url(self):
        # actionable: down services show how to start them; up services show their URL.
        # docker present so the compose services render as startable (the n/a path is tested below).
        with mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False):
            down = "\n".join(stack._service_health_lines(CFG))
        self.assertIn("→ start: bob services searxng start", down)   # per-service hints now
        self.assertIn("→ start: bob services n8n start", down)
        self.assertIn("→ start: bob whisper start", down)
        with mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(osenv, "is_port_in_use", return_value=True):
            up = "\n".join(stack._service_health_lines(CFG))
        self.assertIn("http://localhost:8081", up)           # litellm URL shown when up
        self.assertNotIn("→ start:", up)

    def test_docker_services_show_unavailable_without_docker(self):
        # #5a — on a box with no docker, the compose services (searxng/n8n/langfuse) can't start at
        # all: report them as n/a with the reason + install hint, NOT a misleading "start:" line.
        with mock.patch.object(osenv, "docker_present", return_value=False), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False):
            out = "\n".join(stack._service_health_lines(CFG))
            snap = {r["name"]: r for r in stack.service_snapshot(CFG)}
        self.assertIn("n/a", out)
        self.assertIn("needs Docker", out)
        # non-docker down services still show their normal start hint (only docker ones go n/a)
        self.assertIn("→ start: bob whisper start", out)
        self.assertTrue(snap["searxng"]["unavailable"])     # docker service -> unavailable
        self.assertFalse(snap["whisper"]["unavailable"])    # non-docker service -> not affected


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
    """The llama-swap launch (exe path, config path, --listen addr, LLAMA_LOCAL_ROOT) lives in ONE
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


class TestEnsureDeps(unittest.TestCase):
    """The one 'bring up exactly the deps this command needs' seam: inference (chat/agent/shell)
    and stt (the /voice preflight), each idempotent and composed from the single-service ops."""

    def test_inference_only_composes_ensure_inference(self):
        with mock.patch.object(stack, "ensure_inference", return_value=(True, ["inf up"])) as ei:
            ok, lines = stack.ensure_deps(CFG, inference=True)
        ei.assert_called_once()
        self.assertTrue(ok)
        self.assertIn("inf up", lines)

    def test_stt_noop_when_already_running(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=True), \
             mock.patch.object(stack, "service_control") as sc:
            ok, lines = stack.ensure_deps(CFG, stt=True)
        sc.assert_not_called()                       # idempotent — whisper already up
        self.assertTrue(any("already running" in ln for ln in lines))

    def test_stt_starts_whisper_when_down(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack, "service_control", return_value="starting") as sc:
            stack.ensure_deps(CFG, stt=True)
        sc.assert_called_once_with(CFG, "whisper", "start")

    def test_inference_and_stt_together(self):
        with mock.patch.object(stack, "ensure_inference", return_value=(True, [])), \
             mock.patch.object(osenv, "is_port_in_use", return_value=True), \
             mock.patch.object(stack, "service_control") as sc:
            ok, _ = stack.ensure_deps(CFG, inference=True, stt=True)
        self.assertTrue(ok)
        sc.assert_not_called()


class TestSearxngOnDemand(unittest.TestCase):
    """On-demand SearXNG: bring up JUST the container when a tool needs it (web_search / music_play)."""

    def test_noop_when_already_up(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=True), \
             mock.patch.object(stack.subprocess, "run") as run:
            ok, msg = stack.ensure_searxng(CFG)
        self.assertTrue(ok)
        run.assert_not_called()                     # already reachable — no docker call
        self.assertIn("already running", msg)

    def test_starts_single_container_and_waits(self):
        # docker present, port down at both checks, up after _poll.
        seq = iter([False, False, True, True, True])
        repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        self.addCleanup(shutil.rmtree, repo, True)
        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda *a, **k: next(seq)), \
             mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(stack, "_compose_base", return_value=(["docker", "compose", "-f", "x"], "")), \
             mock.patch.object(stack, "_write_compose_env"), \
             mock.patch.object(stack.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            ok, _msg = stack.ensure_searxng(CFG)
        self.assertTrue(ok)
        argv = run.call_args[0][0]
        self.assertEqual(argv[-3:], ["up", "-d", "searxng"])   # single service, not the whole group

    def test_graceful_when_no_docker(self):
        # No docker + non-interactive: the generic guided-install path returns a clear hint, never blocks.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "docker_present", return_value=False), \
             mock.patch.object(sys.stdin, "isatty", return_value=False):
            ok, msg = stack.ensure_searxng(CFG)
        self.assertFalse(ok)
        self.assertIn("Docker", msg)

    def test_services_control_scopes_to_one_service(self):
        with mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack, "_compose_base", return_value=(["docker", "compose", "-f", "x"], "")), \
             mock.patch.object(stack, "_write_compose_env"), \
             mock.patch.object(stack, "_poll", return_value=True), \
             mock.patch.object(stack.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="ok", stderr="")) as run:
            stack.services_control(CFG, "start", service="searxng")
            start_argv = run.call_args[0][0]
            stack.services_control(CFG, "stop", service="searxng")
            stop_argv = run.call_args[0][0]
        self.assertIn("searxng", start_argv)
        self.assertEqual(stop_argv[-2:], ["stop", "searxng"])   # stop one, not `down` the group

    def test_ensure_deps_search_composes_ensure_searxng(self):
        with mock.patch.object(stack, "ensure_searxng", return_value=(True, "up")) as es:
            ok, lines = stack.ensure_deps(CFG, search=True)
        es.assert_called_once()
        self.assertTrue(ok)
        self.assertIn("up", lines)

    def test_ensure_searxng_is_thin_alias(self):
        # searxng auto-start now delegates to the generic ensure_service — no bespoke code.
        with mock.patch.object(stack, "ensure_service", return_value=(True, "aliased")) as es:
            ok, msg = stack.ensure_searxng(CFG)
        es.assert_called_once_with(CFG, "searxng")
        self.assertTrue(ok)
        self.assertEqual(msg, "aliased")

    def test_ensure_service_generic_docker_service(self):
        # A docker service (langfuse) auto-starts through the SAME path with NO new code:
        # its container name + port come from the SERVICES registry entry.
        seq = iter([False, False, True, True, True])
        repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        self.addCleanup(shutil.rmtree, repo, True)
        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda *a, **k: next(seq)), \
             mock.patch.object(osenv, "docker_present", return_value=True), \
             mock.patch.object(stack, "_compose_base", return_value=(["docker", "compose", "-f", "x"], "")), \
             mock.patch.object(stack, "_write_compose_env"), \
             mock.patch.object(stack.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run:
            ok, _msg = stack.ensure_service(CFG, "langfuse")
        self.assertTrue(ok)
        self.assertEqual(run.call_args[0][0][-3:], ["up", "-d", "langfuse"])   # single service, from registry

    def test_ensure_service_starts_native_daemon(self):
        # ensure_service is generic: a native daemon (whisper) routes to its registry start fn, not docker.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack, "service_control", return_value="starting") as sc:
            ok, msg = stack.ensure_service(CFG, "whisper")
        sc.assert_called_once_with(CFG, "whisper", "start")
        self.assertEqual(msg, "starting")

    def test_ensure_service_unknown_name(self):
        ok, msg = stack.ensure_service(CFG, "nope")
        self.assertFalse(ok)
        self.assertIn("Unknown service", msg)


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
        # The regen bridge runs the Python generators; _regen_configs is a
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


class _LogsMixin:
    def setUp(self):
        self.logs = Path(tempfile.mkdtemp(prefix="bob-logs-"))
        self.addCleanup(__import__("shutil").rmtree, self.logs, True)
        mock.patch.object(stack, "_logs_dir", return_value=self.logs).start()
        self.addCleanup(mock.patch.stopall)


class TestBindHost(_LogsMixin, unittest.TestCase):
    """Loopback by default (the laptop moves between networks); one top-level bindHost key opts into LAN."""

    def _litellm_argv(self, cfg):
        seen = {}
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "ensure_secret", return_value="s"), \
             mock.patch.object(osenv, "start_detached", side_effect=lambda argv, **k: seen.update(argv=argv, **k) or 1):
            stack._start_litellm_bg(cfg)
        return seen

    def test_litellm_binds_loopback_by_default(self):
        seen = self._litellm_argv(CFG)
        self.assertEqual(seen["argv"][seen["argv"].index("--host") + 1], "127.0.0.1")

    def test_bind_host_opts_into_lan(self):
        seen = self._litellm_argv({**CFG, "bindHost": "0.0.0.0"})
        self.assertEqual(seen["argv"][seen["argv"].index("--host") + 1], "0.0.0.0")

    def test_litellm_gets_the_generated_langfuse_keys(self):
        seen = self._litellm_argv(CFG)
        self.assertEqual(seen["env"]["LANGFUSE_PUBLIC_KEY"], "s")
        self.assertIn("LANGFUSE_HOST", seen["env"])

    def test_webui_binds_loopback_with_a_generated_secret(self):
        seen = {}
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "secret", return_value="sk-local"), \
             mock.patch.object(osenv, "ensure_secret", return_value="generated") as es, \
             mock.patch.object(osenv, "start_detached", side_effect=lambda argv, **k: seen.update(argv=argv, **k) or 7):
            line, started = stack._start_webui_bg(CFG)
        self.assertTrue(started)
        self.assertEqual(seen["argv"][seen["argv"].index("--host") + 1], "127.0.0.1")
        self.assertEqual(seen["env"]["WEBUI_SECRET_KEY"], "generated")
        es.assert_any_call("webuiSecret")

    def _voice_env(self, start, cfg):
        seen = {}
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "bin_exe", return_value=Path(__file__)), \
             mock.patch.object(stack, "_poll", return_value=True), \
             mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(stack, "_read_pid", return_value=None), \
             mock.patch.object(osenv, "start_detached", side_effect=lambda argv, **k: seen.update(k) or 5):
            start(cfg)
        return seen["env"]

    def test_voice_servers_stay_on_loopback_when_bind_host_opens_the_lan(self):
        """The voice servers take no key: bindHost alone never exposes them."""
        lan = {**CFG, "bindHost": "0.0.0.0"}
        self.assertEqual(self._voice_env(stack._start_stt_bg, lan)["STT_HOST"], "127.0.0.1")
        self.assertEqual(self._voice_env(stack._start_piper_bg, lan)["PIPER_HOST"], "127.0.0.1")

    def test_voice_bind_host_opts_them_in(self):
        cfg = {**CFG, "bindHost": "0.0.0.0", "voiceBindHost": "0.0.0.0"}
        self.assertEqual(self._voice_env(stack._start_stt_bg, cfg)["STT_HOST"], "0.0.0.0")
        self.assertEqual(self._voice_env(stack._start_piper_bg, cfg)["PIPER_HOST"], "0.0.0.0")

    def test_llama_swap_stays_on_loopback_even_with_lan(self):
        with mock.patch.object(osenv, "bin_exe", return_value=Path("/x/llama-swap")):
            _exe, argv, _env, _port = stack._swap_launch({**CFG, "bindHost": "0.0.0.0"})
        self.assertIn("127.0.0.1:8080", argv)


class TestEndpointAlwaysEnsuresLiteLLM(_LogsMixin, unittest.TestCase):
    """A healthy llama-swap next to a crashed LiteLLM must restart LiteLLM, and success means the proxy port
    answers: `bob up` must never report 'running' while the port clients call is dead."""

    def test_swap_up_litellm_down_restarts_litellm(self):
        with mock.patch.object(osenv, "bin_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=lambda p, *a, **k: p == 8080), \
             mock.patch.object(stack, "_start_litellm_bg", return_value="LiteLLM proxy (PID 9)") as lit, \
             mock.patch.object(stack, "_http_ok", return_value=True), \
             mock.patch.object(stack, "_poll", side_effect=lambda check, **k: check()):
            ok, lines = stack._start_endpoint_bg(CFG)
        lit.assert_called_once()
        self.assertFalse(ok)                       # proxy never answered -> not ok
        self.assertTrue(any("LiteLLM proxy did not come up" in ln for ln in lines))

    def test_ok_only_when_proxy_answers(self):
        with mock.patch.object(osenv, "bin_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "is_port_in_use", return_value=True), \
             mock.patch.object(stack, "_start_litellm_bg", return_value="LiteLLM already running (PID 9)"), \
             mock.patch.object(stack, "_http_ok", return_value=True), \
             mock.patch.object(stack, "_poll", side_effect=lambda check, **k: check()):
            ok, _ = stack._start_endpoint_bg(CFG)
        self.assertTrue(ok)


class TestRestart(_LogsMixin, unittest.TestCase):
    def _restart(self, webui_up, ports_free=True):
        order = []
        (self.logs / "llama-swap.pid").write_text("100")
        (self.logs / "open-webui.pid").write_text("300")
        busy = {"state": True}

        def port(p, *a, **k):
            if p == 3000:
                return webui_up and busy["state"]
            return busy["state"] and not ports_free

        with mock.patch.object(osenv, "pid_alive", side_effect=lambda pid: pid == 300 and webui_up or pid == 100), \
             mock.patch.object(osenv, "stop_process_tree", side_effect=lambda pid: order.append(("stop", pid))), \
             mock.patch.object(osenv, "stop_processes_by_name", return_value=[]), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=port), \
             mock.patch.object(stack, "_poll", side_effect=lambda check, **k: (busy.update(state=False) if ports_free else None) or check()), \
             mock.patch.object(stack, "_ensure_configs", return_value=""), \
             mock.patch.object(stack, "ensure_inference",
                               side_effect=lambda c: order.append(("start", "core")) or (True, ["up"])), \
             mock.patch.object(stack, "_start_webui_bg",
                               side_effect=lambda c: order.append(("start", "webui")) or ("webui up", True)):
            out = stack.stack_restart(CFG)
        return out, order

    def test_webui_restarted_when_it_was_running(self):
        out, order = self._restart(webui_up=True)
        self.assertIn(("start", "webui"), order)
        self.assertLess(order.index(("stop", 100)), order.index(("start", "core")))   # stop completes first

    def test_webui_left_down_when_it_was_down(self):
        _out, order = self._restart(webui_up=False)
        self.assertNotIn(("start", "webui"), order)

    def test_aborts_when_ports_stay_busy(self):
        out, order = self._restart(webui_up=False, ports_free=False)
        self.assertIn("Restart aborted", out)
        self.assertNotIn(("start", "core"), order)


class TestUpDoesNotDuplicateWebui(_LogsMixin, unittest.TestCase):
    def test_second_up_leaves_running_webui_alone(self):
        (self.logs / "open-webui.pid").write_text("300")
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "pid_alive", return_value=True), \
             mock.patch.object(osenv, "start_detached") as sd:
            line, started = stack._start_webui_bg(CFG)
        sd.assert_not_called()
        self.assertFalse(started)
        self.assertIn("already running", line)
        self.assertEqual((self.logs / "open-webui.pid").read_text(), "300")


class TestServicePortAccessor(unittest.TestCase):
    def test_agent_ports_read_from_the_agent_section(self):
        cfg = {**CFG, "agent": {"agentPort": 9999, "mcpPort": 9998}}
        self.assertEqual(stack.service_port(cfg, "agentPort"), 9999)
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "docker_present", return_value=False):
            rows = {r["name"]: r for r in stack.service_snapshot(cfg)}
        self.assertEqual(rows["agent-api"]["port"], 9999)       # the port the server actually binds
        self.assertEqual(rows["mcp-http"]["port"], 9998)
        self.assertEqual(rows["litellm"]["port"], 8081)


class TestComposeSecrets(unittest.TestCase):
    COMPOSE = Path(stack.REPO) / "tools" / "compose" / "docker-compose.yml"

    def test_no_literal_secrets_and_every_port_on_bind_host(self):
        import yaml
        text = self.COMPOSE.read_text()
        for literal in ("admin123", "bob-langfuse-secret", "bob-salt", "local-bob-n8n-key", "bob-searxng"):
            self.assertNotIn(literal, text)
        doc = yaml.safe_load(text)
        for name, svc in doc["services"].items():
            for port in svc.get("ports", []):
                self.assertTrue(str(port).startswith("${BIND_HOST}:"), f"{name}: {port}")

    def test_searxng_settings_carry_no_secret(self):
        text = (Path(stack.REPO) / "config" / "searxng" / "settings.yml").read_text()
        self.assertNotIn("bob-searxng", text)
        body = "\n".join(ln for ln in text.splitlines() if not ln.strip().startswith("#"))
        self.assertNotIn("secret_key:", body)    # supplied by SEARXNG_SECRET at start

    def test_env_file_holds_ports_only_and_up_gets_secrets(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(osenv, "ensure_secret", side_effect=lambda name, **k: f"gen-{name}"):
            stack._write_compose_env(CFG)
            env = stack._compose_env(CFG, secrets=True)
        body = (repo / "tools" / "compose" / ".env").read_text()
        self.assertIn("BIND_HOST=127.0.0.1", body)
        self.assertNotIn("gen-", body)                                  # no secret ever on disk here
        self.assertEqual(env["LANGFUSE_SALT"], "gen-langfuseSalt")
        self.assertEqual(env["SEARXNG_SECRET"], "gen-searxngSecret")

    def test_existing_postgres_keeps_its_password(self):
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, repo, True)
        (repo / "tools" / "langfuse-data").mkdir(parents=True)
        (repo / "tools" / "langfuse-data" / "PG_VERSION").write_text("17")
        calls = {}

        def fake_secret(name, **k):
            calls[name] = k
            return "x"

        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(osenv, "ensure_secret", side_effect=fake_secret):
            stack._compose_env(CFG, secrets=True)
        self.assertEqual(calls["langfusePostgresPassword"].get("legacy"), "langfuse")


class TestN8nSecrets(unittest.TestCase):
    def test_existing_data_dir_key_is_adopted(self):
        import json
        data = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, data, True)
        (data / ".n8n").mkdir()
        (data / ".n8n" / "config").write_text(json.dumps({"encryptionKey": "the-key-its-data-uses"}))
        with mock.patch.object(osenv, "ensure_secret", side_effect=lambda name, legacy=None, **k: legacy or "new"):
            self.assertEqual(stack._n8n_encryption_key(data), "the-key-its-data-uses")
            self.assertEqual(stack._n8n_encryption_key(data / "fresh"), "new")

    def test_version_falls_back_to_the_defaults_pin_not_latest(self):
        from bob_core import load_defaults
        pin = load_defaults()["runtime"]["agent"]["n8nVersion"]
        self.assertEqual(stack._n8n_version({}), pin)
        self.assertNotEqual(stack._n8n_version({}), "latest")


class TestLiteLLMKeyDrift(_LogsMixin, unittest.TestCase):
    """The proxy reads its master key from LITELLM_MASTER_KEY at start; a proxy still running with an
    older key (it answers 401 to Bob's) is restarted, and stale generated configs are regenerated."""

    def test_litellm_start_passes_the_key_in_its_environment(self):
        seen = {}
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "ensure_secret", return_value="s"), \
             mock.patch.object(stack, "_litellm_key", return_value="sk-bob-now"), \
             mock.patch.object(osenv, "start_detached", side_effect=lambda argv, **k: seen.update(k) or 1):
            stack._start_litellm_bg(CFG)
        self.assertEqual(seen["env"]["LITELLM_MASTER_KEY"], "sk-bob-now")

    def test_litellm_is_never_started_without_a_master_key(self):
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(stack, "_litellm_key", return_value=""), \
             mock.patch.object(osenv, "start_detached") as start:
            line = stack._start_litellm_bg(CFG)
        start.assert_not_called()
        self.assertIn("not started", line)

    def _ensure(self, rejected, pid_alive=True, managed=((41, "litellm"),)):
        import bob_core
        (self.logs / "litellm.pid").write_text("41")
        stopped = []
        with mock.patch.object(bob_core, "check_litellm", return_value=True), \
             mock.patch.object(bob_core, "litellm_key_rejected", return_value=rejected), \
             mock.patch.object(stack, "_sync_key_configs", return_value=["Regenerated config/x"]), \
             mock.patch.object(osenv, "pid_alive", return_value=pid_alive), \
             mock.patch.object(osenv, "find_managed_processes", return_value=list(managed)), \
             mock.patch.object(osenv, "stop_process_tree", side_effect=stopped.append), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack, "_start_endpoint_bg", return_value=(True, ["started"])) as start:
            ok, lines = stack.ensure_inference(CFG)
        return ok, lines, stopped, start

    def test_an_accepting_proxy_is_left_alone(self):
        ok, lines, stopped, start = self._ensure(rejected=False)
        self.assertTrue(ok)
        start.assert_not_called()
        self.assertEqual(stopped, [])
        self.assertIn("Regenerated config/x", lines)   # the config sync runs even when up

    def test_a_rejecting_proxy_is_restarted(self):
        ok, lines, stopped, start = self._ensure(rejected=True)
        self.assertEqual(stopped, [41])
        start.assert_called_once()
        self.assertTrue(ok)
        self.assertTrue(any("outdated key" in ln for ln in lines))

    def test_a_rejecting_proxy_bob_did_not_start_is_reported(self):
        ok, lines, stopped, start = self._ensure(rejected=True, pid_alive=False)
        self.assertFalse(ok)
        start.assert_not_called()
        self.assertTrue(any("not started by Bob" in ln for ln in lines))

    def test_a_stale_pidfile_naming_another_process_is_not_killed(self):
        # After a reboot logs/litellm.pid can name a live, unrelated process: it is not Bob's LiteLLM.
        ok, lines, stopped, start = self._ensure(rejected=True, managed=[(99, "litellm")])
        self.assertEqual(stopped, [])
        self.assertFalse(ok)
        start.assert_not_called()
        self.assertTrue(any("not started by Bob" in ln for ln in lines))

    def test_sync_key_configs_delegates_to_the_generator(self):
        import generate
        with mock.patch.object(generate, "refresh_stale_key_files", return_value=["r"]) as r, \
             mock.patch.object(generate, "refresh_dsh_credential", return_value="") as d:
            self.assertEqual(_REAL_SYNC_KEY_CONFIGS(CFG), ["r"])
        r.assert_called_once_with(CFG)
        d.assert_called_once_with(CFG)

    def test_sync_key_configs_refreshes_dsh_on_every_start(self):
        import generate
        with mock.patch.object(generate, "refresh_stale_key_files", return_value=[]), \
             mock.patch.object(generate, "refresh_dsh_credential", return_value="dsh: updated BOB_LITELLM_KEY"):
            self.assertEqual(_REAL_SYNC_KEY_CONFIGS(CFG), ["dsh: updated BOB_LITELLM_KEY"])
        with mock.patch.object(generate, "refresh_stale_key_files", return_value=[]), \
             mock.patch.object(generate, "refresh_dsh_credential", side_effect=OSError("denied")):
            self.assertIn("dsh", _REAL_SYNC_KEY_CONFIGS(CFG)[0])

    def test_webui_start_syncs_its_stored_key_first(self):
        import generate
        repo = Path(tempfile.mkdtemp(prefix="bob-webui-repo-"))
        with mock.patch.object(stack, "REPO", repo), \
             mock.patch.object(generate, "webui_sync_key", return_value="Open WebUI: updated") as w:
            self.assertEqual(_REAL_WEBUI_SYNC_KEY(CFG), "Open WebUI: updated")
        w.assert_called_once_with(repo / "tools" / "webui-data" / "webui.db", CFG)
        with mock.patch.object(osenv, "venv_exe", return_value=Path(__file__)), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "ensure_secret", return_value="g"), \
             mock.patch.object(stack, "_webui_sync_key", return_value="Open WebUI: updated"), \
             mock.patch.object(osenv, "start_detached", return_value=7):
            line, started = stack._start_webui_bg(CFG)
        self.assertTrue(started)
        self.assertIn("Open WebUI: updated", line)


class TestLiteLLMKeyProbe(unittest.TestCase):
    """bob_core.litellm_key_rejected: only a 401/403 to Bob's key counts; no answer is not a mismatch."""

    def _serve(self, code):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_codes(self):
        import bob_core
        for code, want in ((401, True), (403, True), (200, False), (500, False)):
            port = self._serve(code)
            self.assertIs(bob_core.litellm_key_rejected({"litellmPort": port, "litellmKey": "k"}), want, code)

    def test_nothing_listening_is_not_a_rejection(self):
        import socket
        import bob_core
        with socket.socket() as sk:
            sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]
        self.assertFalse(bob_core.litellm_key_rejected({"litellmPort": port, "litellmKey": "k"}))


if __name__ == "__main__":
    unittest.main()
