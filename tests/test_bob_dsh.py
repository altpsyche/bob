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

    def test_default_model_is_a_top_level_override_not_a_second_row(self):
        # dsh-base already inserts agent-default-model; the user layer overrides it by id, the form a
        # live profile carries. An insert would add a duplicate id, which dsh refuses to load.
        patch = self.home / "profiles" / "web" / "cordis.patch.yml"
        patch.write_text(
            "# Your patch layer\n"
            "- id: agent-default-model\n"
            "  name: \"@deepseek-ai/dsh-agent-default-model\"\n"
            "  config:\n"
            "    provider: google\n"
            "    model: gemini\n",
            encoding="utf-8",
        )
        bob_dsh.set_default_model("web", "coder-deep", root=self.home)
        import yaml
        data = yaml.safe_load(patch.read_text(encoding="utf-8"))
        self.assertEqual(data, [{"id": "agent-default-model", "name": "@deepseek-ai/dsh-agent-default-model",
                                 "config": {"provider": "bob", "model": "coder-deep"}}])
        self.assertEqual(bob_dsh._default_model(self.home, "web"), "coder-deep")

    def test_edits_on_a_dump_config_seed_stay_valid_yaml(self):
        # `dsh --dump-config` seeds a new patch file with an empty flow sequence.
        import yaml
        patch = self.home / "profiles" / "web" / "cordis.patch.yml"
        patch.write_text("# Your patch layer for this dsh profile\n[]\n", encoding="utf-8")
        bob_dsh.set_default_model("web", "coder-deep", root=self.home)
        bob_dsh._upsert_plugin(patch, "bob-dsh-bridge",
                               bob_dsh._plugin_entry_lines("bob-dsh-bridge", "bob-dsh-bridge", ["bobCommand: bob"]))
        data = yaml.safe_load(patch.read_text(encoding="utf-8"))
        self.assertEqual([d.get("id") for d in data], ["agent-default-model", None])
        self.assertEqual(data[1]["insert"][0]["id"], "bob-dsh-bridge")
        # removing every entry leaves an array, not an empty document
        bob_dsh._remove_plugin(patch, "bob-dsh-bridge")
        bob_dsh._remove_plugin(patch, "agent-default-model")
        self.assertEqual(yaml.safe_load(patch.read_text(encoding="utf-8")), [])

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
            self.assertEqual(first, {"sessions": 2, "turns": 3, "new_turns": 3, "skipped": 0})
            self.assertEqual(second, {"sessions": 2, "turns": 3, "new_turns": 0, "skipped": 0})
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
            self.assertEqual(first, {"sessions": 2, "turns": 4, "new_turns": 4, "skipped": 0})
            self.assertEqual(second, {"sessions": 2, "turns": 4, "new_turns": 0, "skipped": 0})
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
        self.assertIn("forgot DSH session root-7f3a and 1 child session(s)", out)
        self.assertEqual(listed, "no imported DSH sessions")   # the child goes with its root
        self.assertIn("unknown DSH session", shown)

    def test_forgotten_sessions_stay_forgotten_on_a_resent_snapshot(self):
        import sqlite3
        db, cfg = self._db()
        payload = json.loads((Path(__file__).parent / "fixtures" / "dsh_bridge_payload.json")
                             .read_text(encoding="utf-8"))
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=db), \
             mock.patch.object(bob_memory, "embed", return_value=None):
            bob_dsh.sessions_forget("root-7f3a")
            result = bob_dsh.import_session(payload)
        self.assertEqual(result["sessions"], 0)
        conn = sqlite3.connect(str(db))
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM transcript").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0], 0)
        finally:
            conn.close()


class TestInstallLink(unittest.TestCase):
    def test_install_covers_route_credential_tools_bridge_and_default_model(self):
        import generate
        cfg = _common.fake_config()
        with tempfile.TemporaryDirectory(prefix="bob-dsh-install-") as d:
            root = Path(d) / ".dsh"
            root.mkdir()
            with mock.patch.object(bob_dsh, "home", return_value=root), \
                 mock.patch.object(bob_dsh, "ensure_dsh", return_value="dsh package ready"), \
                 mock.patch.object(bob_dsh, "ensure_home", return_value="dsh home ready"), \
                 mock.patch.object(bob_core, "load_config", return_value=cfg), \
                 mock.patch.object(generate, "configure"), \
                 mock.patch.object(generate, "gen_dsh", return_value="routes"), \
                 mock.patch.object(generate, "_install_dsh_settings", return_value="settings") as settings, \
                 mock.patch.object(generate, "_install_dsh_credential", return_value="credential") as credential, \
                 mock.patch.object(bob_dsh, "_install_mcp", return_value="mcp") as mcp, \
                 mock.patch.object(bob_dsh, "bridge_on", return_value="bridge") as bridge, \
                 mock.patch.object(bob_dsh, "set_default_model", return_value="model") as default:
                out = bob_dsh.install(tools=True, bridge=True, use_default=True)
        self.assertIn("Installed bob dsh link", out)
        settings.assert_called_once_with(root)
        credential.assert_called_once_with(root)
        mcp.assert_called_once_with(root)
        bridge.assert_called_once_with("web")
        default.assert_called_once_with("web", bob_dsh.DEFAULT_MODEL, root)


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


class TestEnsureHome(unittest.TestCase):
    def test_initializes_the_profile_noninteractively(self):
        with tempfile.TemporaryDirectory(prefix="bob-dsh-home-") as d:
            root = Path(d) / ".dsh"
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                (root / "profiles" / "web").mkdir(parents=True, exist_ok=True)
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(bob_dsh, "home", return_value=root), \
                 mock.patch.object(bob_dsh, "dsh_bin", return_value="/usr/bin/dsh"), \
                 mock.patch.object(bob_dsh.subprocess, "run", side_effect=fake_run):
                out = bob_dsh.ensure_home()
            self.assertIn("initialized dsh home", out)
            self.assertEqual(calls[0][1:3], ["--profile", "web"])

    def test_existing_home_is_left_alone(self):
        with tempfile.TemporaryDirectory(prefix="bob-dsh-home-") as d:
            root = Path(d) / ".dsh"
            (root / "profiles" / "web").mkdir(parents=True)
            with mock.patch.object(bob_dsh, "home", return_value=root), \
                 mock.patch.object(bob_dsh.subprocess, "run") as run:
                out = bob_dsh.ensure_home()
            self.assertIn("already initialized", out)
            run.assert_not_called()


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
            with mock.patch.object(bob_dsh, "_user_config_path", return_value=path):
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


def _session(sid, texts, parent=None, cwd="/project", events=None):
    return {"session_id": sid, "parent_session_id": parent, "cwd": cwd,
            "events": events if events is not None else [{"seq": i, "type": "x"} for i in range(len(texts))],
            "messages": [{"role": "user", "content": [{"type": "text", "text": t}]} for t in texts]}


class TestIncrementalImport(unittest.TestCase):
    """dsh_import_sessions only writes and embeds what a snapshot adds, and survives bad descriptors."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bob-dsh-inc-")
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "bob.db"
        self.embedded = []
        patcher = mock.patch.object(bob_memory, "embed",
                                    side_effect=lambda text: self.embedded.append(text) or [0.1, 0.2])
        patcher.start()
        self.addCleanup(patcher.stop)

    def _import(self, sessions, scope_for=lambda cwd: cwd):
        return bob_memory.dsh_import_sessions(sessions, self.db, scope_for=scope_for)

    def _dump(self):
        import sqlite3
        conn = sqlite3.connect(str(self.db))
        try:
            return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall()
                    for t in ("transcript", "dsh_events", "dsh_sessions")}
        finally:
            conn.close()

    def test_same_payload_twice_changes_nothing_and_embeds_nothing(self):
        payload = [_session("s1", ["one", "two"])]
        first = self._import(payload)
        self.assertEqual(self.embedded, ["one", "two"])
        before = self._dump()
        second = self._import(payload)
        self.assertEqual(self.embedded, ["one", "two"])      # no new embed calls
        self.assertEqual(self._dump(), before)               # not even updated_at moves
        self.assertEqual((first["new_turns"], second["new_turns"]), (2, 0))

    def test_a_grown_snapshot_embeds_only_the_new_turns(self):
        self._import([_session("s1", ["one", "two"])])
        self.embedded.clear()
        result = self._import([_session("s1", ["one", "two", "three"])])
        self.assertEqual(self.embedded, ["three"])
        self.assertEqual(result["new_turns"], 1)
        rows = self._dump()["transcript"]
        self.assertEqual([r[6] for r in rows], ["one", "two", "three"])
        self.assertEqual(len(self._dump()["dsh_events"]), 3)

    def test_a_rewritten_history_is_reimported_from_the_divergence(self):
        self._import([_session("s1", ["one", "two"])])
        self.embedded.clear()
        self._import([_session("s1", ["one", "summary"])])
        self.assertEqual(self.embedded, ["summary"])
        self.assertEqual([r[6] for r in self._dump()["transcript"]], ["one", "summary"])

    def test_malformed_sessions_are_skipped_not_fatal(self):
        bad_seq = _session("bad-seq", ["x"], events=[{"seq": "abc", "type": "x"}])
        bad_source = _session("bad-source", ["y"])
        bad_source["messages"][0]["source"] = "tool"
        with self.assertLogs("bob.memory", level="WARNING"):
            result = self._import(["not a dict", bad_seq, bad_source, {"cwd": "/p"},
                                   _session("good", ["fine"])])
        self.assertEqual(result["skipped"], 4)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual([r[6] for r in self._dump()["transcript"]], ["fine"])

    def test_events_without_seq_do_not_collide(self):
        events = [{"type": "a"}, {"type": "b"}, {"seq": 0, "type": "c"}]
        self._import([_session("s1", ["one"], events=events)])
        self._import([_session("s1", ["one"], events=events)])
        stored = sorted((r[2], r[3]) for r in self._dump()["dsh_events"])
        self.assertEqual(stored, [(-2, "b"), (-1, "a"), (0, "c")])

    def test_scope_is_the_project_key_not_the_raw_cwd(self):
        repo = Path(self._tmp.name) / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "sub").mkdir()
        cfg = _common.fake_config()
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(bob_core, "_get_db_path", return_value=self.db):
            bob_dsh.import_session({"sessions": [_session("s1", ["one"], cwd=str(repo / "sub"))]})
        row = self._dump()["transcript"][0]
        self.assertEqual(row[3], str(repo.resolve()))        # scope column
        self.assertEqual(self._dump()["dsh_sessions"][0][2], str(repo / "sub"))   # raw cwd kept


class TestUserConfigWrites(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bob-dsh-cfg-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "user.json"
        env = mock.patch.dict(os.environ, {"BOB_USER_CONFIG": str(self.path)})
        env.start()
        self.addCleanup(env.stop)

    def test_mcp_flag_honors_bob_user_config_and_keeps_other_keys(self):
        self.path.write_text(json.dumps({"agent": {"maxSteps": 3}, "x": 1}), encoding="utf-8")
        out = bob_dsh._set_agent_flags(mcpEnabled=True)
        self.assertIn(str(self.path), out)
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data, {"agent": {"maxSteps": 3, "mcpEnabled": True}, "x": 1})

    def test_unparseable_overlay_is_refused_and_left_untouched(self):
        self.path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(bob_dsh.ConfigWriteRefused):
            bob_dsh._set_agent_flags(mcpEnabled=True)
        self.assertIn("trust not changed", bob_dsh.trust_tools(tier="write"))
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{ not json")

    def test_tools_off_turns_off_dsh_tools_only(self):
        root = Path(self._tmp.name) / ".dsh"
        root.mkdir()
        (root / "cordis.patch.yml").write_text("- insert:\n    - id: bob-tools\n      name: x\n",
                                               encoding="utf-8")
        with mock.patch.object(bob_dsh, "home", return_value=root):
            out = bob_dsh.tools_off()
        self.assertIn("removed", out)
        agent = json.loads(self.path.read_text(encoding="utf-8"))["agent"]
        self.assertIs(agent["dshTools"], False)
        self.assertNotIn("mcpEnabled", agent)      # Bob's MCP server stays on for other clients
        import generate
        self.assertFalse(generate.dsh_tools_enabled({"agent": dict(agent, mcpEnabled=True)}))
        self.assertTrue(generate.dsh_tools_enabled({"agent": {"mcpEnabled": True}}))
        self.assertFalse(generate.dsh_tools_enabled({"agent": {"dshTools": True}}))

    def test_uninstall_turns_the_link_off_and_install_turns_it_back_on(self):
        root = Path(self._tmp.name) / ".dsh"
        (root / "profiles" / "web").mkdir(parents=True)
        with mock.patch.object(bob_dsh, "home", return_value=root):
            bob_dsh.uninstall()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIs(data["agent"]["dshEnabled"], False)
        self.assertNotIn("mcpEnabled", data["agent"])        # uninstall leaves the MCP server flag alone
        self.assertFalse(bob_dsh.link_enabled({"agent": data["agent"]}))
        with mock.patch.object(bob_dsh, "install", return_value="Installed bob dsh link"), \
             mock.patch.object(bob_dsh, "link_enabled", return_value=False), \
             mock.patch("builtins.print"):
            self.assertEqual(bob_dsh.main(["install"]), 0)
        self.assertIs(json.loads(self.path.read_text(encoding="utf-8"))["agent"]["dshEnabled"], True)

    def test_generate_skips_dsh_when_the_link_is_off(self):
        import generate
        with mock.patch.object(generate, "_bob_cfg", return_value={"agent": {"dshEnabled": False}}), \
             mock.patch.object(generate, "_install_dsh_settings") as settings:
            out = generate.install_dsh()
        self.assertIn("skipped", out)
        settings.assert_not_called()


class TestTrustTierShape(unittest.TestCase):
    def test_write_tier_excludes_scheduling_and_core_blocks(self):
        reg = _common.FakeRegistry(
            mutating_tools={"file_write", "profile_switch", "memory_block", "schedule_add",
                            "schedule_enable", "schedule_disable", "schedule_remove", "schedule_future"},
            approval_required_tools={"shell_run"})
        reg.remote_tools = set()
        tiers = bob_dsh.trust_tiers(reg)
        self.assertEqual(tiers["write"], {"file_write", "profile_switch"})
        self.assertIn("schedule_add", tiers["execute"])
        self.assertIn("memory_block", tiers["execute"])

    def test_project_tier_matches_subdirectories_and_most_specific_wins(self):
        reg = _common.FakeRegistry(mutating_tools={"file_write"}, approval_required_tools={"shell_run"})
        reg.remote_tools = set()
        cfg = {"agent": {"dshTrust": {"global": "read", "projects": {
            str(Path("/proj").resolve()): "all", str(Path("/proj/sub").resolve()): "write"}}}}
        self.assertIn("shell_run", bob_dsh.resolve_dsh_allow(cfg, cwd="/proj/other/deep", registry=reg))
        self.assertEqual(bob_dsh.resolve_dsh_allow(cfg, cwd="/proj/sub/x", registry=reg), {"file_write"})
        self.assertEqual(bob_dsh.resolve_dsh_allow(cfg, cwd="/projector", registry=reg), set())

    def test_project_match_is_case_insensitive_where_paths_are(self):
        reg = _common.FakeRegistry(mutating_tools={"file_write"})
        reg.remote_tools = set()
        cfg = {"agent": {"dshTrust": {"projects": {"/Work/Proj": "write"}}}}
        with mock.patch.object(bob_dsh.os.path, "normcase", side_effect=str.lower):
            self.assertEqual(bob_dsh.resolve_dsh_allow(cfg, cwd="/work/proj/src", registry=reg),
                             {"file_write"})


class TestEnsureDshVersions(unittest.TestCase):
    def _run(self, have, which, exe="/usr/lib/node_modules/.bin/dsh", install_missing=True):
        runs = []
        with mock.patch.object(bob_dsh, "pinned_dsh_version", return_value="0.1.5-rc.3"), \
             mock.patch.object(bob_dsh, "dsh_bin", return_value=exe), \
             mock.patch.object(bob_dsh, "dsh_version", return_value=have), \
             mock.patch.object(bob_dsh.shutil, "which", side_effect=lambda n: which.get(n)), \
             mock.patch.object(bob_dsh.subprocess, "run",
                               side_effect=lambda argv, **k: runs.append(argv) or mock.Mock(returncode=0)):
            out = bob_dsh.ensure_dsh(install_missing=install_missing)
        return out, runs

    def test_newer_installed_dsh_is_not_downgraded(self):
        for have in ("dsh 0.1.5", "0.1.6-rc.1", "0.2.0"):
            out, runs = self._run(have, {"npm": "/usr/bin/npm", "pnpm": "/usr/bin/pnpm"})
            self.assertIn("already installed", out)
            self.assertEqual(runs, [])

    def test_older_npm_install_is_upgraded_with_npm_not_pnpm(self):
        out, runs = self._run("0.1.5-rc.2", {"npm": "/usr/bin/npm", "pnpm": "/usr/bin/pnpm"})
        self.assertIn("upgraded", out)
        self.assertEqual(runs, [["/usr/bin/npm", "install", "-g", "@deepseek-ai/dsh@0.1.5-rc.3"]])

    def test_older_pnpm_install_is_upgraded_with_pnpm(self):
        out, runs = self._run("0.1.4", {"npm": "/usr/bin/npm", "pnpm": "/usr/bin/pnpm"},
                              exe="/home/u/.local/share/pnpm/dsh")
        self.assertEqual(runs[0][:2], ["/usr/bin/pnpm", "add"])

    def test_refresh_never_installs_a_missing_dsh(self):
        out, runs = self._run("", {"pnpm": "/usr/bin/pnpm"}, exe="", install_missing=False)
        self.assertIn("skipped", out)
        self.assertEqual(runs, [])


class TestBridgeOn(_HomeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.patch = self.home / "profiles" / "web" / "cordis.patch.yml"

    def test_no_dsh_binary_is_reported_and_nothing_is_written(self):
        with mock.patch.object(bob_dsh, "dsh_bin", return_value=""):
            out = bob_dsh.bridge_on("web")
        self.assertIn("dsh binary not found", out)
        self.assertNotIn("installed bob-dsh-bridge", out)
        self.assertFalse(self.patch.exists())

    def test_failed_plugin_add_is_reported_and_nothing_is_written(self):
        with mock.patch.object(bob_dsh, "dsh_bin", return_value="/usr/bin/dsh"), \
             mock.patch.object(bob_dsh.subprocess, "run",
                               return_value=mock.Mock(returncode=1, stdout="", stderr="ERR_PNPM boom")):
            out = bob_dsh.bridge_on("web")
        self.assertIn("dsh plugin add failed: ERR_PNPM boom", out)
        self.assertFalse(self.patch.exists())

    def test_success_records_the_absolute_bob_command(self):
        with mock.patch.object(bob_dsh, "dsh_bin", return_value="/usr/bin/dsh"), \
             mock.patch.object(bob_dsh, "_bridge_command", return_value=r"C:\Users\me\scoop\shims\bob.cmd"), \
             mock.patch.object(bob_dsh.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="done", stderr="")):
            out = bob_dsh.bridge_on("web")
        self.assertIn("installed bob-dsh-bridge", out)
        import yaml
        items = yaml.safe_load(self.patch.read_text(encoding="utf-8"))
        cfg = items[0]["insert"][0]["config"]
        self.assertEqual(cfg["bobCommand"], r"C:\Users\me\scoop\shims\bob.cmd")

    def test_bridge_is_a_plugin_dependency_not_a_profile_bundle(self):
        # dsh refuses to boot a profile listing a bundle with no `dsh.bundle` ("declares no dsh.bundle"),
        # so the bridge is never a bundle and an entry left by an earlier install is removed.
        pkg = self.home / "profiles" / "web" / "package.json"
        pkg.write_text(json.dumps({"dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "bob-dsh-bridge"]}}}),
                       encoding="utf-8")
        with mock.patch.object(bob_dsh, "dsh_bin", return_value="/usr/bin/dsh"), \
             mock.patch.object(bob_dsh.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="done", stderr="")):
            bob_dsh.bridge_on("web")
        data = json.loads(pkg.read_text(encoding="utf-8"))
        self.assertEqual(data["dsh"]["profile"]["bundles"], ["@deepseek-ai/dsh-base"])
        self.assertIn("bob-dsh-bridge", data["dependencies"])
        self.assertTrue(bob_dsh._bridge_installed(self.home, "web"))

    def test_bridge_command_is_an_absolute_path(self):
        self.assertTrue(Path(bob_dsh._bridge_command()).is_absolute()
                        or bob_dsh._bridge_command().endswith("bob.cmd"))


class TestRefresh(_HomeMixin, unittest.TestCase):
    """`bob update` refreshes what is present and re-adds nothing the user turned off."""

    def _refresh(self, tools=False):
        import generate
        with mock.patch.object(bob_dsh, "ensure_dsh", return_value="dsh ok") as ensure, \
             mock.patch.object(bob_dsh, "ensure_home") as ensure_home, \
             mock.patch.object(bob_core, "load_config", return_value=_common.fake_config()), \
             mock.patch.object(generate, "configure"), \
             mock.patch.object(generate, "gen_dsh"), \
             mock.patch.object(generate, "_install_dsh_settings", return_value="settings") as settings, \
             mock.patch.object(generate, "_install_dsh_credential", return_value="credential"), \
             mock.patch.object(bob_dsh, "_install_mcp", return_value="mcp") as mcp, \
             mock.patch.object(bob_dsh, "bridge_on", return_value="bridge") as bridge, \
             mock.patch.object(bob_dsh, "_route_models", return_value={"coder-quick", "coder-deep"}), \
             mock.patch.object(bob_dsh, "set_default_model", return_value="model") as default:
            out = bob_dsh.refresh(tools=tools)
        ensure.assert_called_once_with(install_missing=False)
        ensure_home.assert_not_called()
        return out, settings, mcp, bridge, default

    def _write_patch(self, provider, model):
        (self.home / "profiles" / "web" / "cordis.patch.yml").write_text(
            "- id: agent-default-model\n  name: x\n  config:\n"
            f"    provider: {provider}\n    model: {model}\n", encoding="utf-8")

    def test_turned_off_parts_stay_off(self):
        self._write_patch("google", "gemini")             # a non-Bob default model
        _out, settings, mcp, bridge, default = self._refresh(tools=False)
        settings.assert_not_called()                       # no route present: not added
        mcp.assert_not_called()
        bridge.assert_not_called()
        default.assert_not_called()

    def test_present_parts_are_refreshed_and_the_mode_is_kept(self):
        (self.home / "settings.yaml").write_text(
            "llm-pi-ai:\n  providers:\n    bob:\n      models: [{id: chat-quick}]\n", encoding="utf-8")
        (self.home / "profiles" / "web" / "package.json").write_text(
            json.dumps({"dependencies": {"bob-dsh-bridge": "file:/x"}}), encoding="utf-8")
        (self.home / "profiles" / "web" / "cordis.patch.yml").write_text(
            "- id: agent-default-model\n  name: x\n  config:\n    provider: bob\n    model: chat-quick\n"
            "- insert:\n    - id: bob-dsh-bridge\n      name: bob-dsh-bridge\n", encoding="utf-8")
        with mock.patch.object(bob_dsh, "_choose_model", side_effect=lambda mode: f"coder-{mode}"):
            _out, settings, mcp, bridge, default = self._refresh(tools=True)
        settings.assert_called_once()
        mcp.assert_called_once()
        bridge.assert_called_once_with("web")
        default.assert_called_once_with("web", "coder-quick", self.home)   # quick stays quick

    def test_a_served_bob_model_is_left_as_is(self):
        self._write_patch("bob", "coder-quick")
        *_rest, default = self._refresh()
        default.assert_not_called()

    def test_install_keeps_the_current_quick_mode(self):
        import generate
        self._write_patch("bob", "coder-quick")
        with mock.patch.object(bob_dsh, "ensure_dsh", return_value=""), \
             mock.patch.object(bob_dsh, "ensure_home", return_value=""), \
             mock.patch.object(bob_core, "load_config", return_value=_common.fake_config()), \
             mock.patch.object(generate, "configure"), mock.patch.object(generate, "gen_dsh"), \
             mock.patch.object(generate, "_install_dsh_settings", return_value="s"), \
             mock.patch.object(generate, "_install_dsh_credential", return_value="c"), \
             mock.patch.object(bob_dsh, "bridge_on", return_value="b"), \
             mock.patch.object(bob_dsh, "_choose_model", side_effect=lambda mode: f"coder-{mode}"), \
             mock.patch.object(bob_dsh, "set_default_model", side_effect=OSError("unreadable")) as default:
            out = bob_dsh.install(use_default=True)
        default.assert_called_once_with("web", "coder-quick", self.home)
        self.assertIn("default model: failed (unreadable)", out)   # reported, not raised


class TestBridgeWindowsSpawn(unittest.TestCase):
    def test_cmd_shims_run_through_cmd_exe(self):
        src = (Path(bob_dsh.REPO) / "scripts" / "dsh_bridge" / "index.js").read_text(encoding="utf-8")
        self.assertIn(r"\.(cmd|bat)$", src)
        self.assertIn('"/d", "/s", "/c"', src)
        self.assertIn("windowsVerbatimArguments", src)
        self.assertIn('child.stdin.on("error"', src)
