"""Phase 2 service model: n8n native (opt-in, Node-version guarded) + the generic guided Docker install
for the docker-kind services (searxng/langfuse). All routed through the one service_control."""
import sys
import unittest
from unittest import mock

import _common
import osenv
import stack

CFG = _common.fake_config()


class TestN8nNative(unittest.TestCase):
    def test_node_missing_skips_with_hint(self):
        with mock.patch.object(stack, "_node_version", return_value=None):
            out = stack._start_n8n_bg(CFG)
        self.assertIn("Node.js not found", out)

    def test_node_below_floor_skips_cleanly(self):
        # n8n is opt-in, so an out-of-range Node skips with an upgrade hint, never a broken install.
        with mock.patch.object(stack, "_node_version", return_value=(18, 19)):
            out = stack._start_n8n_bg(CFG)
        self.assertIn("Node 20.19", out)
        self.assertIn("Upgrade Node", out)

    def test_installs_on_demand_then_starts(self):
        # node in range, port free, binary absent -> npm_local_install runs, then start_detached.
        with mock.patch.object(stack, "_node_version", return_value=(20, 19)), \
             mock.patch.object(osenv, "is_port_in_use", side_effect=[False, True]), \
             mock.patch.object(stack, "_n8n_exe") as exe, \
             mock.patch.object(osenv, "npm_local_install", return_value=True) as npm, \
             mock.patch.object(osenv, "start_detached", return_value=4321) as sd, \
             mock.patch.object(stack, "_poll", return_value=True):
            exe.return_value = mock.Mock()
            exe.return_value.exists.side_effect = [False, True]   # absent pre-install, present after
            out = stack._start_n8n_bg(CFG)
        npm.assert_called_once()
        sd.assert_called_once()
        self.assertIn("n8n:", out)

    def test_routed_through_service_control_as_native(self):
        # `bob services n8n start` -> service_control routes n8n to its registry-bound native start fn
        # (not the docker path). The start fn is bound on the SERVICES entry, so patch it there.
        with mock.patch.dict(stack._svc("n8n"), {"start": lambda cfg: "n8n up"}):
            out = stack.service_control(CFG, "n8n", "start")
        self.assertEqual(out, "n8n up")


class TestGuidedDockerInstall(unittest.TestCase):
    def test_docker_service_non_tty_returns_hint_no_prompt(self):
        # A docker-kind service (langfuse) with no Docker, non-interactive -> a clear hint, never blocks.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "docker_present", return_value=False), \
             mock.patch.object(osenv, "docker_install_hint", return_value="install docker"), \
             mock.patch.object(sys.stdin, "isatty", return_value=False):
            out = stack.service_control(CFG, "langfuse", "start")
        self.assertIn("Docker", out)

    def test_guided_install_runs_package_seam_on_yes(self):
        # Interactive 'yes' -> the generic path installs Docker through the package-manager seam.
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "docker_present", side_effect=[False, True]), \
             mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="y"), \
             mock.patch.object(osenv, "install_package") as install, \
             mock.patch.object(stack.shutil, "which", return_value=None), \
             mock.patch.object(stack, "_compose_base", return_value=(["docker", "compose", "-f", "x"], "")), \
             mock.patch.object(stack, "_write_compose_env"), \
             mock.patch.object(stack, "_prepare_docker_service"), \
             mock.patch.object(stack, "_poll", return_value=True), \
             mock.patch.object(stack.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="", stderr="")):
            out = stack.service_control(CFG, "langfuse", "start")
        install.assert_called_once_with("docker")
        self.assertIn("langfuse", out)

    def test_guided_install_declined_reports_and_stops(self):
        with mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(osenv, "docker_present", return_value=False), \
             mock.patch.object(osenv, "docker_install_hint", return_value="install docker"), \
             mock.patch.object(sys.stdin, "isatty", return_value=True), \
             mock.patch("builtins.input", return_value="n"), \
             mock.patch.object(osenv, "install_package") as install:
            out = stack.service_control(CFG, "searxng", "start")
        install.assert_not_called()
        self.assertIn("Install Docker", out)


if __name__ == "__main__":
    unittest.main()
