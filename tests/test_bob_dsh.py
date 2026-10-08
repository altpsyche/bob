"""The Bob <-> DeepSeek Harness link: one owner per dsh key and one Python command surface."""
import json
import os
import tempfile
import types
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


    def test_realistic_bridge_fixture_smoke(self):
        """One root turn + tool call/result + a child subagent, imported through the bridge payload,
        is searchable in Bob's transcript and is idempotent on re-import."""
        import sqlite3
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "dsh_bridge_payload.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="bob-dsh-smoke-") as d:
            db = Path(d) / "bob.db"
            cfg = _common.fake_config()
            with mock.patch.object(bob_core, "load_config", return_value=cfg), \
                 mock.patch.object(bob_core, "_get_db_path", return_value=db), \
                 mock.patch.object(bob_memory, "embed", return_value=None):
                first = bob_dsh.import_session(fixture)
                second = bob_dsh.import_session(fixture)
                hits = bob_memory.transcript_search("assistant reply", db, owner="local")
            self.assertEqual(first, {"sessions": 2, "turns": 4})
            self.assertEqual(second, {"sessions": 2, "turns": 4})
            conn = sqlite3.connect(str(db))
            try:
                sessions = dict(conn.execute(
                    "SELECT session_id, parent_session_id FROM dsh_sessions").fetchall())
                events = conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0]
                turns = conn.execute(
                    "SELECT run_id, role, content, tool_name FROM transcript ORDER BY run_id, seq"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(sessions["root-7f3a"], None)
            self.assertEqual(sessions["child-91b2"], "root-7f3a")
            self.assertEqual(events, 12)                      # raw events are retained once
            self.assertEqual(len(turns), 4)
            self.assertIn(("dsh:root-7f3a", "user", "Fix the bridge import.", None), turns)
            self.assertIn(("dsh:root-7f3a", "assistant",
                           "assistant reply: working on the DSH bridge\n"
                           '[tool-call shell_run call_shell_1] {"command": "pytest tests/test_bob_dsh.py"}',
                           None), turns)
            self.assertIn(("dsh:root-7f3a", "tool", "[tool-result call_shell_1] 1 passed", "shell_run"),
                          turns)
            self.assertIn(("dsh:child-91b2", "assistant",
                           "subagent reply: checked the child session", None), turns)
            self.assertTrue(any(h["content"].startswith("assistant reply: working on the DSH bridge")
                                for h in hits), hits)


class TestSessions(unittest.TestCase):
    """`bob dsh sessions` reads and maintains the imported DSH surface without a second store."""

    def _db(self):
        d = tempfile.mkdtemp(prefix="bob-dsh-sessions-")
        self.addCleanup(__import__("shutil").rmtree, d, True)
        db = Path(d) / "bob.db"
        cfg = _common.fake_config()
        payload = json.loads((Path(__file__).parent / "fixtures" / "dsh_bridge_payload.json")
                             .read_text(encoding="utf-8"))
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db), \
             mock.patch.object(bob_memory, "embed", return_value=None):
            bob_dsh.import_session(payload)
        return db, cfg

    def test_list_and_show_describe_the_imported_session(self):
        db, cfg = self._db()
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db):
            listed = bob_dsh.sessions_list()
            shown = bob_dsh.sessions_show("root-7f3a")
        self.assertIn("root-7f3a", listed)
        self.assertIn("child-91b2", listed)
        self.assertIn("session root-7f3a", shown)
        self.assertIn("raw events: 8", shown)
        self.assertIn("working on the DSH bridge", shown)

    def test_show_and_consolidate_unknown_session_are_clean(self):
        db, cfg = self._db()
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db):
            self.assertIn("unknown DSH session", bob_dsh.sessions_show("missing"))
            self.assertIn("no imported turns", bob_dsh.sessions_consolidate("missing"))

    def test_consolidate_forwards_the_project_scope_and_provenance(self):
        db, cfg = self._db()
        calls = {}
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db), \
             mock.patch.object(bob_core, "consolidate_session",
                               side_effect=lambda turns, **kw: calls.update(
                                   turns=turns, **kw) or {"facts": 1, "summary": "s"}):
            out = bob_dsh.sessions_consolidate("root-7f3a")
        self.assertIn("consolidated root-7f3a", out)
        self.assertEqual(calls["scope"], str(Path("/home/dev/project").resolve()))
        self.assertEqual(calls["session_id"], "root-7f3a")
        self.assertEqual(len(calls["turns"]), 3)

    def test_forget_removes_the_imported_surface(self):
        db, cfg = self._db()
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db):
            out = bob_dsh.sessions_forget("root-7f3a")
            listed = bob_dsh.sessions_list()
            shown = bob_dsh.sessions_show("root-7f3a")
        self.assertIn("forgot DSH session root-7f3a", out)
        self.assertFalse(any(line.startswith("root-7f3a") for line in listed.splitlines()))
        self.assertIn("child-91b2", listed)                 # only the requested session is forgotten
        self.assertIn("unknown DSH session", shown)


class TestEnsureDsh(unittest.TestCase):
    def test_pinned_version_already_installed_is_a_noop(self):
        with mock.patch.object(bob_dsh, "pinned_dsh_version", return_value="0.1.5-rc.3"), \
             mock.patch.object(bob_dsh, "dsh_version", return_value="0.1.5-rc.3"):
            self.assertIn("already installed", bob_dsh.ensure_dsh())

    def test_missing_package_manager_reports_the_pinned_install_command(self):
        with mock.patch.object(bob_dsh, "pinned_dsh_version", return_value="0.1.5-rc.3"), \
             mock.patch.object(bob_dsh, "dsh_version", return_value=""), \
             mock.patch.object(bob_dsh.shutil, "which", return_value=None):
            out = bob_dsh.ensure_dsh()
        self.assertIn("@deepseek-ai/dsh@0.1.5-rc.3", out)
        self.assertIn("pnpm", out)


class TestTrustTiers(unittest.TestCase):
    """DSH trust is a per-project/global tier resolved from the live registry, plus the old explicit
    mcpAllowTools list; a built-in PreToolUse hook enforces it on unattended surfaces."""

    def _reg(self):
        reg = _common.FakeRegistry(mutating_tools={"file_write", "memory_store"},
                                   approval_required_tools={"shell_run"})
        reg.remote_tools = {"mcp:remote:do_thing"}
        return reg

    def test_tiers_are_ordered_and_all_covers_every_gate(self):
        reg = self._reg()
        tiers = bob_dsh.trust_tiers(reg)
        self.assertEqual(tiers["read"], set())
        self.assertEqual(tiers["write"], {"file_write", "memory_store"})
        self.assertEqual(tiers["execute"], {"file_write", "memory_store", "shell_run"})
        self.assertEqual(tiers["all"],
                         {"file_write", "memory_store", "shell_run", "mcp:remote:do_thing",
                          "spawn_agent", "schedule_run"})
        self.assertTrue(tiers["write"] < tiers["execute"] < tiers["all"])

    def test_project_tier_overrides_global_and_manual_tools_merge(self):
        reg = self._reg()
        cfg = {"agent": {"mcpAllowTools": ["manual_tool"],
                         "dshTrust": {"global": "write",
                                      "projects": {str(Path("/proj").resolve()): "all"}}}}
        global_allow = bob_dsh.resolve_dsh_allow(cfg, cwd="/elsewhere", registry=reg)
        project_allow = bob_dsh.resolve_dsh_allow(cfg, cwd="/proj", registry=reg)
        self.assertEqual(global_allow, {"manual_tool", "file_write", "memory_store"})
        self.assertEqual(project_allow, {"manual_tool", "file_write", "memory_store", "shell_run",
                                         "mcp:remote:do_thing", "spawn_agent", "schedule_run"})

    def test_no_tier_configuration_is_the_old_flat_list(self):
        reg = self._reg()
        cfg = {"agent": {"mcpAllowTools": ["shell_run"]}}
        self.assertEqual(bob_dsh.resolve_dsh_allow(cfg, cwd="/proj", registry=reg), {"shell_run"})
        self.assertIsNone(bob_dsh.make_trust_hook(cfg, reg))

    def test_pre_tool_use_hook_denies_gated_tools_outside_the_tier(self):
        reg = self._reg()
        cfg = {"agent": {"dshTrust": {"global": "write"}}}
        hook = bob_dsh.make_trust_hook(cfg, reg)
        unattended = types.SimpleNamespace(unattended_allow=frozenset({"file_write"}))
        self.assertIsNone(hook("file_write", "{}", unattended))
        self.assertEqual(hook("shell_run", "{}", unattended), {"decision": "deny"})
        attended = types.SimpleNamespace(unattended_allow=None)
        self.assertIsNone(hook("shell_run", "{}", attended))

    def test_trust_tools_writes_and_clears_scopes(self):
        with tempfile.TemporaryDirectory(prefix="bob-dsh-trust-") as d:
            path = Path(d) / "config" / "user.json"
            with mock.patch.object(bob_dsh, "_trust_config_path", return_value=path):
                self.assertIn("tier write set", bob_dsh.trust_tools(tier="write", scope="global"))
                self.assertIn("tier all set", bob_dsh.trust_tools(tier="all", scope="project", project="/proj"))
                self.assertIn("trusted shell_run", bob_dsh.trust_tools(["shell_run"], scope="global"))
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["agent"]["dshTrust"]["global"], "write")
                self.assertEqual(data["agent"]["dshTrust"]["projects"][str(Path("/proj").resolve())], "all")
                self.assertEqual(data["agent"]["mcpAllowTools"], ["shell_run"])
                self.assertIn("cleared trust tier", bob_dsh.trust_tools(off=True, scope="project", project="/proj"))
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertNotIn(str(Path("/proj").resolve()), data["agent"]["dshTrust"]["projects"])

    def test_registry_build_installs_the_pre_tool_use_policy(self):
        from tool_registry import ToolRegistry
        cfg = _common.fake_config()
        cfg["agent"]["dshTrust"] = {"global": "read"}
        sentinel = lambda *a, **k: None  # noqa: E731 — identity check only
        with mock.patch.object(bob_dsh, "make_trust_hook", return_value=sentinel):
            reg = ToolRegistry.from_config(cfg, quiet=True)
        self.assertIn(sentinel, reg.hooks["PreToolUse"])

    def test_mcp_dispatch_uses_the_resolved_tier_allowlist(self):
        import bob_mcp_server as mcp
        reg = self._reg()
        cfg = {"agent": {"dshTrust": {"global": "write"}}}
        seen = {}
        with mock.patch("bob_permissions.run_gated",
                        side_effect=lambda *a, **k: seen.update(k) or "ran"):
            out = mcp.dispatch(reg, "file_write", {}, cfg)
        self.assertEqual(out, "ran")
        self.assertEqual(seen["allow_unattended"], {"file_write", "memory_store"})


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
        self.assertIn("Promise.all", src)                 # async DSH surfaces are awaited
        self.assertIn("parentSessionId", src)             # both parent-session header spellings
        self.assertIn("Array.isArray", src)               # a sync/absent list surfaces cleanly
