"""The Bob <-> DeepSeek Harness link: one owner per dsh key and one Python command surface."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401
import bob_dsh


class _HomeMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bob-dsh-")
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / ".dsh"
        self.home.mkdir(parents=True)
        (self.home / "profiles" / "web").mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"DSH_HOME": str(self.home)})
        self._env.start()
        self.addCleanup(self._env.stop)


class TestLayerWriter(_HomeMixin, unittest.TestCase):
    def test_default_model_upsert_preserves_other_entries(self):
        patch = self.home / "profiles" / "web" / "cordis.patch.yml"
        patch.write_text(
            "- insert:\n"
            "    - id: user-other\n"
            "      name: 'user-plugin'\n"
            "      config:\n"
            "        keep: me\n"
            "- insert:\n"
            "    - id: agent-default-model\n"
            "      name: '@deepseek-ai/dsh-agent-default-model'\n"
            "      config:\n"
            "        provider: google\n"
            "        model: gemini\n",
            encoding="utf-8",
        )
        line = bob_dsh.set_default_model("web", "coder-deep", root=self.home)
        text = patch.read_text(encoding="utf-8")
        self.assertIn("agent-default-model", line)
        self.assertIn("provider: bob", text)
        self.assertIn("model: coder-deep", text)
        self.assertIn("user-other", text)
        self.assertNotIn("gemini", text)

    def test_mcp_entry_can_be_removed_without_touching_other_items(self):
        patch = self.home / "cordis.patch.yml"
        patch.write_text(
            "- insert:\n"
            "    - id: user-plugin\n"
            "      name: 'x'\n"
            "- insert:\n"
            "    - id: bob-tools\n"
            "      name: '@deepseek-ai/dsh-mcp-client'\n"
            "      config:\n"
            "        serverName: bob\n",
            encoding="utf-8",
        )
        self.assertTrue(bob_dsh._remove_plugin(patch, "bob-tools"))
        text = patch.read_text(encoding="utf-8")
        self.assertNotIn("bob-tools", text)
        self.assertIn("user-plugin", text)

    def test_hooks_entry_uses_one_home_owner(self):
        result = bob_dsh.hooks_on()
        self.assertIn("installed", result)
        patch = (self.home / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertIn("bob-hooks", patch)
        self.assertTrue((Path(bob_dsh.REPO) / "config" / "dsh" / "hooks.json").exists())


class TestHook(unittest.TestCase):
    def test_session_start_emits_additional_context(self):
        out = io.StringIO()
        with mock.patch.object(bob_dsh, "_hook_context", return_value="PROFILE-CTX"), \
             mock.patch.object(bob_dsh.sys, "stdout", out):
            code = bob_dsh.hook("session-start", json.dumps({"session_id": "s1", "cwd": "/p"}))
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertEqual(payload["hookSpecificOutput"]["additionalContext"], "PROFILE-CTX")

    def test_non_session_event_is_silent(self):
        out = io.StringIO()
        with mock.patch.object(bob_dsh.sys, "stdout", out):
            code = bob_dsh.hook("pre-tool-use", "{}")
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue(), "")


class TestStatus(_HomeMixin, unittest.TestCase):
    def test_doctor_reports_the_link(self):
        (self.home / "settings.yaml").write_text(
            "llm-pi-ai:\n  providers:\n    bob:\n      models:\n        - id: chat\n",
            encoding="utf-8")
        (self.home / ".credentials.yaml").write_text(
            "version: 1\n\nrefs:\n  BOB_LITELLM_KEY: 'sk-x'\n", encoding="utf-8")
        with mock.patch.object(bob_dsh, "dsh_bin", return_value="/usr/bin/dsh"), \
             mock.patch.object(bob_dsh, "dsh_version", return_value="0.1.5"):
            out = bob_dsh.doctor("web")
        self.assertIn("bob provider route", out)
        self.assertIn("bob credential", out)
        self.assertIn("default model", out)


if __name__ == "__main__":
    unittest.main()
