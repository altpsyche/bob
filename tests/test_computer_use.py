"""Computer-use OS input seam: the backend is chosen by session type + availability, the argv is built
correctly per backend, and absence or an unsupported OS degrades to a clear RuntimeError. No real input
is ever injected (shutil.which / os_name are mocked; the run path is never reached)."""
import os
import unittest
from unittest import mock

import json
import shutil
import tempfile
from pathlib import Path

import _common  # noqa: F401
import bob_vision
import computer
import osenv


class TestInputBackendSelection(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def _force_session(self, kind):
        for k in ("WAYLAND_DISPLAY", "XDG_SESSION_TYPE", "DISPLAY"):
            os.environ.pop(k, None)
        if kind == "wayland":
            os.environ["WAYLAND_DISPLAY"] = "wayland-0"
            os.environ["XDG_SESSION_TYPE"] = "wayland"
        else:
            os.environ["XDG_SESSION_TYPE"] = "x11"
            os.environ["DISPLAY"] = ":0"

    def test_input_backend_selects_x11_when_xdotool_present(self):
        self._force_session("x11")
        with mock.patch("osenv.shutil.which", side_effect=lambda t: "/usr/bin/xdotool" if t == "xdotool" else None):
            self.assertEqual(osenv._input_backend(), "xdotool")

    def test_input_backend_selects_wayland_when_ydotool_present(self):
        self._force_session("wayland")
        with mock.patch("osenv.shutil.which", side_effect=lambda t: "/usr/bin/ydotool" if t == "ydotool" else None):
            self.assertEqual(osenv._input_backend(), "ydotool")

    def test_input_raises_when_no_backend(self):
        self._force_session("x11")
        with mock.patch("osenv.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                osenv.input_click(10, 20)

    def test_input_unsupported_on_macos(self):
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "macos"}):
            with self.assertRaises(RuntimeError) as ctx:
                osenv.input_click(1, 2)
        self.assertIn("macOS", str(ctx.exception))


class TestInputCommandBuilders(unittest.TestCase):
    def test_xdotool_click_moves_then_clicks_at_coords(self):
        cmds = osenv._linux_input_commands("xdotool", "click", x=812, y=344, button="left")
        self.assertEqual(len(cmds), 1)
        argv = cmds[0]
        self.assertIn("mousemove", argv)
        self.assertIn("812", argv)
        self.assertIn("344", argv)
        self.assertIn("click", argv)

    def test_ydotool_click_is_move_then_click(self):
        cmds = osenv._linux_input_commands("ydotool", "click", x=5, y=6, button="right")
        self.assertEqual(len(cmds), 2)
        self.assertEqual(cmds[0][0], "ydotool")
        self.assertIn("click", cmds[1])

    def test_wtype_refuses_mouse(self):
        with self.assertRaises(RuntimeError):
            osenv._linux_input_commands("wtype", "click", x=1, y=2)


class TestCoordinateMapping(unittest.TestCase):
    def test_scale_roundtrip_within_rounding(self):
        for w, h in [(1920, 1080), (3840, 2160), (1366, 768), (1000, 1000)]:
            scale = bob_vision.scale_factor(w, h)
            self.assertLessEqual(scale, 1.0)
            # a point given in model space maps to screen and back within a pixel
            for mx, my in [(0, 0), (100, 50), (int(w * scale) - 1, int(h * scale) - 1)]:
                sx, sy = bob_vision.to_screen((mx, my), scale)
                bx, by = bob_vision.to_model((sx, sy), scale)
                self.assertLessEqual(abs(bx - mx), 1, (w, h, mx, my))
                self.assertLessEqual(abs(by - my), 1, (w, h, mx, my))

    def test_large_screen_click_maps_beyond_image_bounds(self):
        # a 1920x1080 screen scaled to fit 1280 long edge: a click near the model-image edge must land
        # near the real-screen edge, not at the same small pixel (the coordinate bug this guards).
        scale = bob_vision.scale_factor(1920, 1080)
        self.assertLess(scale, 1.0)
        sx, sy = bob_vision.to_screen((1279, 719), scale)
        self.assertGreater(sx, 1900)
        self.assertGreater(sy, 1000)

    def test_no_upscale_when_screen_small(self):
        self.assertEqual(bob_vision.scale_factor(800, 600), 1.0)


class TestVirtualDisplay(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_virtual_display_target_selected_by_default(self):
        os.environ["BOB_VIRTUAL_DISPLAY"] = ":99"
        os.environ["DISPLAY"] = ":0"
        self.assertEqual(osenv.computer_display("virtual"), ":99")

    def test_host_display_requires_explicit_optin(self):
        os.environ["DISPLAY"] = ":0"
        os.environ.pop("BOB_VIRTUAL_DISPLAY", None)
        self.assertEqual(osenv.computer_display("host"), ":0")
        # default (virtual) must NOT silently drive the host session
        self.assertIsNone(osenv.computer_display("virtual"))

    def test_virtual_display_absent_reports_cleanly(self):
        os.environ.pop("BOB_VIRTUAL_DISPLAY", None)
        self.assertIsNone(osenv.computer_display("virtual"))


class TestScreenshotTool(unittest.TestCase):
    def test_screenshot_tool_absent_when_computer_use_off(self):
        self.assertFalse(computer.enabled({"agent": {}}))
        self.assertFalse(computer.enabled({"agent": {"computerUse": {"enabled": False}}}))
        self.assertTrue(computer.enabled({"agent": {"computerUse": {"enabled": True}}}))

    def test_screenshot_returns_image_contract_and_records_scale(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host", "maxLongEdge": 1280}}})
        with mock.patch("bob_vision.capture_screen", return_value="/tmp/x.png"), \
             mock.patch("bob_vision.resize_for_control", return_value=("/tmp/x-scaled.png", 0.5, (1280, 720))):
            out = computer.DISPATCH["computer_screenshot"]()
        payload = json.loads(out)
        self.assertEqual(payload["__images__"], ["/tmp/x-scaled.png"])
        self.assertIn("1280x720", payload["text"])
        self.assertEqual(computer._last_scale, 0.5)

    def test_screenshot_degrades_when_no_capture_backend(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host"}}})
        with mock.patch("bob_vision.capture_screen", side_effect=RuntimeError("no screenshot tool")):
            out = computer.DISPATCH["computer_screenshot"]()
        self.assertIn("unavailable", out.lower())

    def test_screenshot_requires_approval(self):
        self.assertTrue(computer.REQUIRES_APPROVAL)


class TestInputActions(unittest.TestCase):
    def setUp(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host"}}})
        computer._last_scale = 1.0
        computer._screenshotted = False

    def _screenshot_at(self, scale, dims=(1280, 720)):
        with mock.patch("bob_vision.capture_screen", return_value="/tmp/x.png"), \
             mock.patch("bob_vision.resize_for_control", return_value=("/tmp/s.png", scale, dims)):
            computer.DISPATCH["computer_screenshot"]()

    def test_click_declared_mutating(self):
        for name in ("computer_click", "computer_type", "computer_key", "computer_scroll", "computer_move"):
            self.assertIn(name, computer.MUTATING_TOOLS)

    def test_click_without_prior_screenshot_refused(self):
        out = computer.DISPATCH["computer_click"](coordinate=[10, 20])
        self.assertIn("screenshot first", out.lower())

    def test_click_maps_model_coords_to_screen_before_backend(self):
        self._screenshot_at(0.5)   # screen is 2x the image the model saw
        seen = {}
        with mock.patch("osenv.input_click", side_effect=lambda x, y, b: seen.update(x=x, y=y, b=b)):
            out = computer.DISPATCH["computer_click"](coordinate=[100, 50], button="left")
        self.assertEqual((seen["x"], seen["y"]), (200, 100))   # mapped up by 1/scale
        self.assertIn("200", out)

    def test_backend_selected_by_session_type(self):
        # a Wayland session with ydotool present builds a ydotool argv, not xdotool
        cmds = osenv._linux_input_commands("ydotool", "click", x=1, y=2, button="left")
        self.assertEqual(cmds[0][0], "ydotool")

    def test_preview_renders_mapped_coords(self):
        computer._last_scale = 0.5
        preview = computer.PREVIEW["computer_click"]({"coordinate": [100, 50], "button": "left"})
        self.assertIn("model(100,50)", preview)
        self.assertIn("screen(200,100)", preview)

    def test_preview_type_does_not_echo_text(self):
        preview = computer.PREVIEW["computer_type"]({"text": "hunter2 secret"})
        self.assertNotIn("hunter2", preview)
        self.assertIn("character", preview)

    def test_input_unavailable_degrades_gracefully(self):
        self._screenshot_at(1.0)
        with mock.patch("osenv.input_click", side_effect=RuntimeError("no backend")):
            out = computer.DISPATCH["computer_click"](coordinate=[1, 2])
        self.assertIn("unavailable", out.lower())


class TestKillSwitchAndRateLimit(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bob-cu-")
        self.halt = Path(self.dir) / "computer_use.halt"
        self._patch = mock.patch("computer.halt_path", return_value=self.halt)
        self._patch.start()
        computer._action_times = []
        computer._screenshotted = True
        computer._last_scale = 1.0

    def tearDown(self):
        self._patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_halt_sentinel_blocks_action(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host"}}})
        computer.set_halt(True)
        called = []
        with mock.patch("osenv.input_click", side_effect=lambda *a: called.append(a)):
            out = computer.DISPATCH["computer_click"](coordinate=[1, 2])
        self.assertIn("halted", out.lower())
        self.assertEqual(called, [])   # backend never reached

    def test_rate_limit_refuses_past_budget(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host", "maxActionsPerMinute": 2}}})
        with mock.patch("osenv.input_click", return_value=None):
            r1 = computer.DISPATCH["computer_click"](coordinate=[1, 1])
            r2 = computer.DISPATCH["computer_click"](coordinate=[2, 2])
            r3 = computer.DISPATCH["computer_click"](coordinate=[3, 3])
        self.assertIn("clicked", r1)
        self.assertIn("clicked", r2)
        self.assertIn("rate limit", r3.lower())

    def test_notify_called_on_action(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host"}}})
        fired = []
        with mock.patch("osenv.notify", side_effect=lambda t, b: fired.append((t, b))), \
             mock.patch("osenv.input_key", return_value=None):
            computer.DISPATCH["computer_key"](keys="Return")
        self.assertTrue(fired)

    def test_set_halt_toggles_sentinel(self):
        self.assertFalse(computer.is_halted())
        computer.set_halt(True)
        self.assertTrue(computer.is_halted())
        computer.set_halt(False)
        self.assertFalse(computer.is_halted())


class TestDisplayTarget(unittest.TestCase):
    """agent.computerUse.display is enforced: 'virtual' (the default) never reaches the host session."""

    def setUp(self):
        self._env = dict(os.environ)
        self.dir = tempfile.mkdtemp(prefix="bob-cu-disp-")
        self._halt = mock.patch("computer.halt_path", return_value=Path(self.dir) / "halt")
        self._halt.start()
        self._linux = mock.patch("osenv.os_name", return_value="linux")
        self._linux.start()
        computer._action_times = []
        computer._screenshotted = True
        computer._last_scale = 1.0

    def tearDown(self):
        self._linux.stop()
        self._halt.stop()
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _host_seams_forbidden(self):
        boom = AssertionError("host seam reached")
        return (mock.patch("osenv.input_click", side_effect=boom),
                mock.patch("osenv.input_type", side_effect=boom),
                mock.patch("bob_vision.capture_screen", side_effect=boom))

    def test_default_is_virtual_and_refuses_without_virtual_display(self):
        os.environ.pop("BOB_VIRTUAL_DISPLAY", None)
        computer.configure({"agent": {"computerUse": {"enabled": True}}})   # no display key
        a, b, c = self._host_seams_forbidden()
        with a, b, c, mock.patch("subprocess.run", side_effect=AssertionError("no command may run")):
            click = computer.DISPATCH["computer_click"](coordinate=[1, 2])
            typed = computer.DISPATCH["computer_type"](text="x")
            shot = computer.DISPATCH["computer_screenshot"]()
        for out in (click, typed, shot):
            self.assertIn("BOB_VIRTUAL_DISPLAY", out)

    def test_virtual_display_drives_only_that_display_via_xdotool(self):
        os.environ["BOB_VIRTUAL_DISPLAY"] = ":99"
        os.environ["DISPLAY"] = ":0"
        os.environ["WAYLAND_DISPLAY"] = "wayland-0"
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "virtual"}}})
        calls = []
        a, b, c = self._host_seams_forbidden()
        with a, b, c, mock.patch("computer.shutil.which", return_value="/usr/bin/xdotool"), \
             mock.patch("computer.subprocess.run",
                        side_effect=lambda argv, **kw: calls.append((argv, kw["env"]))):
            out = computer.DISPATCH["computer_click"](coordinate=[5, 6])
        self.assertIn("clicked", out)
        self.assertTrue(calls)
        for argv, env in calls:
            self.assertEqual(argv[0], "xdotool")
            self.assertEqual(env["DISPLAY"], ":99")
            self.assertNotIn("WAYLAND_DISPLAY", env)

    def test_virtual_display_without_xdotool_refuses(self):
        # ydotool/wtype inject into the host session regardless of DISPLAY, so they are never used.
        os.environ["BOB_VIRTUAL_DISPLAY"] = ":99"
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "virtual"}}})
        a, b, c = self._host_seams_forbidden()
        with a, b, c, mock.patch("computer.shutil.which",
                                 side_effect=lambda t: "/usr/bin/ydotool" if t == "ydotool" else None), \
             mock.patch("computer.subprocess.run", side_effect=AssertionError("must not run")):
            out = computer.DISPATCH["computer_key"](keys="Return")
        self.assertIn("xdotool", out)

    def test_virtual_screenshot_captures_virtual_display(self):
        os.environ["BOB_VIRTUAL_DISPLAY"] = ":99"
        computer.configure({"agent": {"computerUse": {"enabled": True}}})
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, env=kw["env"])
            Path(argv[-1]).write_bytes(b"png")

        with mock.patch("bob_vision.capture_screen", side_effect=AssertionError("host capture")), \
             mock.patch("computer.shutil.which", side_effect=lambda t: "/usr/bin/scrot" if t == "scrot" else None), \
             mock.patch("computer.subprocess.run", side_effect=fake_run), \
             mock.patch("bob_vision.resize_for_control", return_value=("/tmp/s.png", 1.0, (10, 10))):
            out = computer.DISPATCH["computer_screenshot"]()
        self.assertIn("__images__", out)
        self.assertEqual(seen["argv"][0], "scrot")
        self.assertEqual(seen["env"]["DISPLAY"], ":99")
        Path(seen["argv"][-1]).unlink(missing_ok=True)

    def test_virtual_refused_off_linux(self):
        os.environ["BOB_VIRTUAL_DISPLAY"] = ":99"
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "virtual"}}})
        with mock.patch("osenv.os_name", return_value="windows"), \
             mock.patch("osenv.input_click", side_effect=AssertionError("host seam reached")):
            out = computer.DISPATCH["computer_click"](coordinate=[1, 2])
        self.assertIn("only on Linux", out)

    def test_unknown_display_mode_refused(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "desktop"}}})
        with mock.patch("osenv.input_click", side_effect=AssertionError("host seam reached")):
            out = computer.DISPATCH["computer_click"](coordinate=[1, 2])
        self.assertIn("not recognized", out)

    def test_host_mode_uses_host_seam(self):
        computer.configure({"agent": {"computerUse": {"enabled": True, "display": "host"}}})
        with mock.patch("osenv.input_click", return_value=None) as click:
            out = computer.DISPATCH["computer_click"](coordinate=[3, 4])
        self.assertIn("clicked", out)
        click.assert_called_once_with(3, 4, "left")


class TestUnattendedInterlock(unittest.TestCase):
    def test_computer_use_absent_in_unattended_run_without_optin(self):
        cfg = {"agent": {"unattended": True, "computerUse": {"enabled": True}}}
        self.assertFalse(computer.enabled(cfg))

    def test_allow_computer_flag_enables_in_attended_run(self):
        # attended run with computer-use on -> available
        self.assertTrue(computer.enabled({"agent": {"computerUse": {"enabled": True}}}))
        # unattended but explicitly allowed -> available
        cfg = {"agent": {"unattended": True, "computerUse": {"enabled": True, "allowUnattended": True}}}
        self.assertTrue(computer.enabled(cfg))

    def test_task_runner_marks_unattended(self):
        import bob_task_runner
        cfg = _common.fake_config()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg["agent"]["checkpointDbPath"] = str(tmp / "checkpoints.db")   # the durable run row lands here
        with _common.stubbed_llm(factory=lambda: _common.scripted_client(["done"])):
            bob_task_runner.run_task(cfg, "u1", "local", goal="hi")
        self.assertTrue((tmp / "checkpoints.db").exists())
        self.assertTrue(cfg["agent"]["unattended"])
        self.assertFalse(cfg["agent"].get("computerUse", {}).get("allowUnattended", False))


if __name__ == "__main__":
    unittest.main()
