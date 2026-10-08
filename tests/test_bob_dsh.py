"""The Bob <-> DeepSeek Harness link: one owner per dsh key and one Python command surface."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import _common  # noqa: F401
import bob_core
import bob_dsh
import bob_memory


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

class TestImportSession(unittest.TestCase):
    def test_full_surface_import_is_idempotent_and_links_children(self):
        import sqlite3
        with tempfile.TemporaryDirectory(prefix="bob-dsh-db-") as d:
            db = Path(d) / "bob.db"
            cfg = _common.fake_config()
            payload = {
                "root_session_id": "root",
                "source": "dsh",
                "sessions": [
                    {
                        "session_id": "root", "parent_session_id": None, "cwd": "/project",
                        "events": [{"seq": 0, "type": "user/message", "data": {"role": "user"}}],
                        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
                    },
                    {
                        "session_id": "child", "parent_session_id": "root", "cwd": "/project",
                        "events": [{"seq": 0, "type": "assistant/message", "data": {"role": "assistant"}}],
                        "messages": [
                            {"role": "assistant", "content": [{"type": "text", "text": "working"}]},
                            {"role": "user", "source": {"kind": "tool", "callId": "c1"},
                             "content": [{"type": "tool-result", "toolCallId": "c1",
                                          "content": [{"type": "text", "text": "tool output"}]}]},
                        ],
                    },
                ],
            }
            with mock.patch.object(bob_core, "load_config", return_value=cfg), \
                 mock.patch.object(bob_core, "_get_db_path", return_value=db), \
                 mock.patch.object(bob_memory, "embed", return_value=None):
                first = bob_dsh.import_session(payload)
                second = bob_dsh.import_session(payload)
            self.assertEqual(first, {"sessions": 2, "turns": 3})
            self.assertEqual(second, {"sessions": 2, "turns": 3})
            conn = sqlite3.connect(str(db))
            try:
                rows = conn.execute(
                    "SELECT run_id, role, content, tool_name FROM transcript ORDER BY run_id, seq"
                ).fetchall()
                events = conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0]
                sessions = conn.execute("SELECT COUNT(*) FROM dsh_sessions").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(len(rows), 3)                       # no duplication on re-import
            self.assertIn(("dsh:root", "user", "hello", None), rows)
            self.assertIn(("dsh:child", "assistant", "working", None), rows)
            self.assertIn(("dsh:child", "tool", "[tool-result c1] tool output", ""), rows)
            self.assertEqual(events, 2)
            self.assertEqual(sessions, 2)


class TestBridgeInstall(_HomeMixin, unittest.TestCase):
    def test_bridge_status_uses_profile_package_and_patch(self):
        pkg = self.home / "profiles" / "web" / "package.json"
        pkg.write_text(json.dumps({"dependencies": {"bob-dsh-bridge": "file:/tmp"},
                                   "dsh": {"profile": {"bundles": ["bob-dsh-bridge"]}}}),
                       encoding="utf-8")
        patch = self.home / "profiles" / "web" / "cordis.patch.yml"
        patch.write_text("- insert:\n    - id: bob-dsh-bridge\n      name: bob-dsh-bridge\n",
                         encoding="utf-8")
        self.assertIn("on", bob_dsh.bridge_status("web"))
        self.assertIn("removed", bob_dsh.bridge_off("web"))
        self.assertIn("off", bob_dsh.bridge_status("web"))


class TestBridgePackage(unittest.TestCase):
    def test_native_bridge_reads_dsh_sessions_and_calls_bob(self):
        base = Path(bob_dsh.REPO) / "scripts" / "dsh_bridge"
        pkg = json.loads((base / "package.json").read_text(encoding="utf-8"))
        src = (base / "index.js").read_text(encoding="utf-8")
        self.assertEqual(pkg["name"], "bob-dsh-bridge")
        self.assertIn("agent/turn-stopping", src)
        self.assertIn("snapshotEvents", src)
        self.assertIn("deriveMessages", src)
        self.assertIn("import-session", src)
