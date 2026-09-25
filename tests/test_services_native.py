"""Phase 2 service model: n8n native (opt-in, Node-version guarded) + the generic guided Docker install
for the docker-kind services (searxng/langfuse). All routed through the one service_control."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common
import osenv
import stack

CFG = _common.fake_config()


class TestN8nNative(unittest.TestCase):
    def setUp(self):
        repo = Path(tempfile.mkdtemp(prefix="bob-repo-"))
        self.addCleanup(shutil.rmtree, repo, True)
        p = mock.patch.object(stack, "REPO", repo)   # tools/n8n-data lands in the temp repo
        p.start()
        self.addCleanup(p.stop)

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
             mock.patch.object(stack, "_n8n_sync_credential", return_value="") as sync, \
             mock.patch.object(stack, "_poll", return_value=True):
            exe.return_value = mock.Mock()
            exe.return_value.exists.side_effect = [False, True]   # absent pre-install, present after
            out = stack._start_n8n_bg(CFG)
        npm.assert_called_once()
        sd.assert_called_once()
        self.assertIn("n8n:", out)
        # The credential import runs before launch with the exact env n8n starts with.
        self.assertEqual(sync.call_args[0][1], sd.call_args.kwargs["env"])
        self.assertEqual(sync.call_args[0][2], stack.REPO / "tools" / "n8n-data")

    def test_credential_warning_is_non_fatal(self):
        with mock.patch.object(stack, "_node_version", return_value=(22, 0)), \
             mock.patch.object(osenv, "is_port_in_use", return_value=False), \
             mock.patch.object(stack, "_n8n_exe") as exe, \
             mock.patch.object(osenv, "start_detached", return_value=4321) as sd, \
             mock.patch.object(stack, "_n8n_sync_credential", return_value="warning: nope"), \
             mock.patch.object(stack, "_poll", return_value=True):
            exe.return_value = mock.Mock(exists=mock.Mock(return_value=True))
            out = stack._start_n8n_bg(CFG)
        sd.assert_called_once()
        self.assertIn("n8n:", out)
        self.assertIn("warning: nope", out)


class TestN8nCredential(unittest.TestCase):
    """`Bob LiteLLM` is imported through `n8n import:credentials` with n8n's own env, only on a change."""

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(prefix="bob-n8n-data-"))
        self.addCleanup(shutil.rmtree, self.data, True)
        self.exe = self.data / "n8n"
        self.env = {"N8N_USER_FOLDER": str(self.data), "N8N_ENCRYPTION_KEY": "enc-key"}
        self.seen = []

    def _fake_run(self, rc=0):
        def run(argv, **kw):
            path = Path(argv[2].split("=", 1)[1])
            mode = stat.S_IMODE(path.stat().st_mode)
            self.seen.append({"argv": argv, "env": kw.get("env"), "path": path, "mode": mode,
                              "payload": json.loads(path.read_text(encoding="utf-8"))})
            return mock.Mock(returncode=rc, stdout="", stderr="boom" if rc else "")
        return run

    def _sync(self, key="sk-one", rc=0):
        with mock.patch("bob_core._litellm_key", return_value=key), \
             mock.patch.object(stack.subprocess, "run", side_effect=self._fake_run(rc)):
            return stack._n8n_sync_credential(self.exe, self.env, self.data, CFG)

    def test_imports_with_n8n_env_and_removes_temp_file(self):
        self.assertEqual(self._sync(), "")
        self.assertEqual(len(self.seen), 1)
        call = self.seen[0]
        self.assertEqual(call["argv"][:2], [str(self.exe), "import:credentials"])
        self.assertTrue(call["argv"][2].startswith("--input="))
        self.assertEqual(call["env"]["N8N_USER_FOLDER"], str(self.data))
        self.assertEqual(call["env"]["N8N_ENCRYPTION_KEY"], "enc-key")
        self.assertEqual(call["payload"], [{"id": "bob-litellm", "name": "Bob LiteLLM", "type": "httpHeaderAuth",
                                            "data": {"name": "Authorization", "value": "Bearer sk-one"}}])
        if os.name != "nt":
            self.assertEqual(call["mode"], 0o600)
        self.assertFalse(call["path"].exists())                          # plaintext removed after import
        marker = self.data / stack._N8N_CRED_MARKER
        self.assertNotIn("sk-one", marker.read_text(encoding="utf-8"))   # a hash, never the key

    def test_unchanged_key_skips_and_changed_key_reimports(self):
        self._sync("sk-one")
        self._sync("sk-one")
        self.assertEqual(len(self.seen), 1)
        self._sync("sk-two")
        self.assertEqual(len(self.seen), 2)
        self.assertEqual(self.seen[1]["payload"][0]["data"]["value"], "Bearer sk-two")
        self.env["N8N_ENCRYPTION_KEY"] = "other"                          # a new n8n key re-encrypts too
        self._sync("sk-two")
        self.assertEqual(len(self.seen), 3)

    def test_failed_import_warns_and_retries_next_start(self):
        out = self._sync(rc=1)
        self.assertIn("warning", out)
        self.assertIn("boom", out)
        self.assertFalse(self.seen[0]["path"].exists())
        self.assertFalse((self.data / stack._N8N_CRED_MARKER).exists())
        self._sync()
        self.assertEqual(len(self.seen), 2)

    def test_missing_binary_is_a_warning(self):
        with mock.patch("bob_core._litellm_key", return_value="k"), \
             mock.patch.object(stack.subprocess, "run", side_effect=OSError("no exe")):
            out = stack._n8n_sync_credential(self.exe, self.env, self.data, CFG)
        self.assertIn("warning", out)
        self.assertEqual([p for p in self.data.iterdir() if p.name.startswith("bob-n8n-cred-")], [])
        with mock.patch("bob_core._litellm_key", return_value="k"), \
             mock.patch.object(stack.subprocess, "run", side_effect=subprocess.TimeoutExpired("n8n", 1)):
            self.assertIn("warning", stack._n8n_sync_credential(self.exe, self.env, self.data, CFG))


class TestN8nWorkflows(unittest.TestCase):
    WF = Path(_common.REPO) / "tools" / "n8n-workflows"

    def _nodes(self, name):
        return json.loads((self.WF / name).read_text(encoding="utf-8"))["nodes"]

    def test_litellm_calls_use_the_credential_not_env(self):
        found = 0
        for f in sorted(self.WF.glob("*.json")):
            self.assertNotIn("$env", f.read_text(encoding="utf-8"), f.name)
            for node in self._nodes(f.name):
                url = str(node.get("parameters", {}).get("url", ""))
                if ":8081/" not in url:
                    continue
                found += 1
                self.assertEqual(node["parameters"]["authentication"], "genericCredentialType")
                self.assertEqual(node["parameters"]["genericAuthType"], "httpHeaderAuth")
                self.assertEqual(node["credentials"],
                                 {"httpHeaderAuth": {"id": stack._N8N_CRED_ID, "name": stack._N8N_CRED_NAME}})
        self.assertGreaterEqual(found, 2)

    def test_vision_webhook_reads_the_post_body(self):
        nodes = {n["name"]: n for n in self._nodes("vision-describe.json")}
        self.assertEqual(nodes["Webhook"]["typeVersion"], 2)
        body = nodes["LLM: Describe Image"]["parameters"]["body"]
        self.assertIn("$json.body.image", body)
        self.assertIn("$json.body.prompt", body)

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
