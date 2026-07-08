"""Vision doors on the loop: cli describe/screenshot handlers + bob_vision capture/resize.
Fake run_agent + monkeypatch; no model call and no real screen capture."""
import os
import tempfile
import unittest
from unittest import mock

import _common
import bob_core
import bob_loop
import bob_vision
from bob import cli


def _tmp_png() -> str:
    fd, path = tempfile.mkstemp(suffix=".png")
    os.write(fd, b"\x89PNG\r\n")
    os.close(fd)
    return path


class TestDescribeHandler(unittest.TestCase):
    def setUp(self):
        self._orig_run_agent = bob_loop.run_agent
        self._orig_load = bob_core.load_config
        self._orig_resize = bob_vision.resize_image
        bob_core.load_config = lambda: _common.fake_config()
        bob_vision.resize_image = lambda p, max_dim=1024: p   # deterministic, no Pillow dependency
        self.captured = {}

        def fake_run_agent(goal, config, role=None, agency=None, stream=False,
                           no_tools=False, max_tokens=None, images=None, **kw):
            self.captured = {"goal": goal, "role": role, "stream": stream,
                             "no_tools": no_tools, "images": images}
            return ("ok", False)

        bob_loop.run_agent = fake_run_agent
        self.img = _tmp_png()

    def tearDown(self):
        bob_loop.run_agent = self._orig_run_agent
        bob_core.load_config = self._orig_load
        bob_vision.resize_image = self._orig_resize
        try:
            os.remove(self.img)
        except OSError:
            pass

    def test_describe_routes_vision_with_image(self):
        rc = cli._handle_describe([self.img, "what", "is", "this"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.captured["role"], "vision")
        self.assertEqual(self.captured["images"], [self.img])
        self.assertTrue(self.captured["no_tools"])
        self.assertEqual(self.captured["goal"], "what is this")

    def test_describe_default_prompt(self):
        cli._handle_describe([self.img])
        self.assertEqual(self.captured["goal"], "Describe this image.")

    def test_describe_pro_flag(self):
        cli._handle_describe([self.img, "--pro"])
        self.assertEqual(self.captured["role"], "vision-pro")

    def test_describe_missing_file_returns_1(self):
        rc = cli._handle_describe(["/no/such/file.png"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.captured, {})   # loop never invoked

    def test_describe_no_positional_prints_usage(self):
        self.assertEqual(cli._handle_describe(["--pro"]), 1)


class TestScreenshotHandler(unittest.TestCase):
    def setUp(self):
        self._orig_run_agent = bob_loop.run_agent
        self._orig_load = bob_core.load_config
        self._orig_resize = bob_vision.resize_image
        self._orig_capture = bob_vision.capture_screen
        bob_core.load_config = lambda: _common.fake_config()
        bob_vision.resize_image = lambda p, max_dim=1024: p
        self.captured = {}

        def fake_run_agent(goal, config, role=None, agency=None, stream=False,
                           no_tools=False, max_tokens=None, images=None, **kw):
            self.captured = {"goal": goal, "role": role, "images": images}
            return ("ok", False)

        bob_loop.run_agent = fake_run_agent

    def tearDown(self):
        bob_loop.run_agent = self._orig_run_agent
        bob_core.load_config = self._orig_load
        bob_vision.resize_image = self._orig_resize
        bob_vision.capture_screen = self._orig_capture

    def test_screenshot_captures_then_describes_and_cleans_up(self):
        shot = _tmp_png()
        bob_vision.capture_screen = lambda: shot
        rc = cli._handle_screenshot(["what's", "here"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.captured["role"], "vision")
        self.assertEqual(self.captured["images"], [shot])
        self.assertEqual(self.captured["goal"], "what's here")
        self.assertFalse(os.path.exists(shot))   # temp capture cleaned up

    def test_screenshot_capture_failure_returns_1(self):
        def boom():
            raise RuntimeError("no screenshot tool found")

        bob_vision.capture_screen = boom
        self.assertEqual(cli._handle_screenshot([]), 1)


class TestBobVision(unittest.TestCase):
    def test_capture_no_tool_raises_on_linux(self):
        # the OS branch goes through osenv.os_name(), so BOB_FORCE_OS drives it in tests
        # (no dependency on the real sys.platform).
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "linux"}), \
             mock.patch.object(bob_vision.shutil, "which", lambda _t: None):
            with self.assertRaises(RuntimeError):
                bob_vision.capture_screen()

    def test_capture_routes_to_pillow_branch_off_linux(self):
        # Forcing macOS routes to the Pillow ImageGrab path, NOT the Linux tool lookup -- prove the
        # seam picked the right branch: the Linux branch is the only one that calls shutil.which.
        with mock.patch.dict(os.environ, {"BOB_FORCE_OS": "macos"}), \
             mock.patch.object(bob_vision.shutil, "which", mock.MagicMock(return_value="/nope")) as which:
            try:
                bob_vision.capture_screen()   # PIL may be absent or headless; either way, no which()
            except Exception:
                pass
            which.assert_not_called()

    def test_resize_without_pil_returns_original(self):
        try:
            import PIL  # noqa: F401
            self.skipTest("Pillow installed — the no-PIL passthrough isn't exercised here")
        except ImportError:
            self.assertEqual(bob_vision.resize_image("/some/image.png"), "/some/image.png")


if __name__ == "__main__":
    unittest.main()
