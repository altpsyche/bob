"""Conversation paging (R3): the full run transcript — incl. intermediate tool turns and stateless CLI
runs — is captured as it happens so a compacted-away turn stays searchable and can be paged back via the
conversation_search tool. Covers the transcript store, the loop capture seam, and the tool. Hermetic:
embed is faked; no live model / network."""
import shutil
import tempfile
import unittest
from types import SimpleNamespace

import _common  # noqa: F401 — sys.path
import bob_core
import bob_loop
import bob_memory


def _fake_embed(text: str):
    return [float(len(text)), float(sum(ord(c) for c in text) % 97), 1.0]


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestTranscriptStore(unittest.TestCase):
    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = tempfile.mkdtemp(prefix="bob-transcript-")
        self.db = f"{self.dir}/m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_append_and_search_round_trip(self):
        bob_memory.transcript_append("run1", "user", "decide the database engine", self.db)
        bob_memory.transcript_append("run1", "assistant", "we will use sqlite for local-first", self.db)
        bob_memory.transcript_append("run1", "tool", "grep found 3 call sites", self.db, tool_name="grep")
        hits = bob_memory.transcript_search("database engine", self.db, k=3)
        self.assertTrue(hits)
        self.assertIn("database engine", hits[0]["content"])
        self.assertEqual(hits[0]["role"], "user")

    def test_tool_turns_are_captured_with_name(self):
        bob_memory.transcript_append("run1", "tool", "elasticsearch shard count is 5", self.db,
                                     tool_name="shell")
        hits = bob_memory.transcript_search("elasticsearch shard", self.db, k=3)
        self.assertEqual(hits[0]["tool_name"], "shell")

    def test_lexical_floor_when_embed_down(self):
        # Embed server down at capture -> NULL vector; FTS keeps the turn searchable lexically.
        def _down(text):
            raise RuntimeError("embed server down")
        bob_memory.embed = _down
        rid = bob_memory.transcript_append("run1", "assistant", "kubernetes ingress annotations", self.db)
        self.assertGreater(rid, 0)                              # persisted despite no embedding
        hits = bob_memory.transcript_search("kubernetes ingress", self.db, k=3)  # search also embed-less
        self.assertTrue(hits)
        self.assertIn("kubernetes", hits[0]["content"])

    def test_owner_scoped_no_leak(self):
        bob_memory.transcript_append("r", "user", "alice secret plan", self.db, owner="alice")
        bob_memory.transcript_append("r", "user", "bob secret plan", self.db, owner="bob")
        hits = bob_memory.transcript_search("secret plan", self.db, owner="alice", k=5)
        self.assertTrue(hits)
        self.assertTrue(all("alice" in h["content"] for h in hits))

    def test_lazy_table_not_created_by_recall_only_db(self):
        bob_memory.get_db(self.db)
        db = bob_memory.get_db(self.db)
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcript'").fetchone()
        self.assertIsNone(row)


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestLoopCapture(unittest.TestCase):
    """The loop persists the transcript as it runs (incl. tool turns), gated by agent.conversationPaging."""
    def setUp(self):
        self._orig_embed = bob_memory.embed
        self._orig_client = bob_core.get_llm_client
        self._orig_check = bob_core.check_litellm
        bob_memory.embed = _fake_embed
        # run_agent_events preflights the LiteLLM proxy and returns early if it's unreachable; stub it
        # True so the loop actually runs offline (no live endpoint in CI), mirroring test_agent_loop.
        bob_core.check_litellm = lambda config=None: True
        self.dir = tempfile.mkdtemp(prefix="bob-loopcap-")
        self.db = f"{self.dir}/m.db"

    def tearDown(self):
        bob_memory.embed = self._orig_embed
        bob_core.get_llm_client = self._orig_client
        bob_core.check_litellm = self._orig_check
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cfg(self, paging):
        cfg = _common.fake_config()
        cfg["memory"] = {"enabled": True, "dbPath": self.db}
        cfg["agent"] = {**cfg.get("agent", {}), "conversationPaging": paging,
                        "toolFormat": "hermes", "maxSteps": 5}
        return cfg

    def _run(self, cfg):
        turns = ['<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>', "final answer text"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _common.FakeRegistry({"echo": "echoed-result-payload"})
        list(bob_loop.run_agent_events("what is the plan", cfg, agency="silent", registry=reg))

    def test_paging_on_persists_user_tool_and_final(self):
        self._run(self._cfg(paging=True))
        db = bob_memory.get_db(self.db)
        rows = db.execute("SELECT role, content, tool_name FROM transcript ORDER BY seq").fetchall()
        roles = [r[0] for r in rows]
        self.assertIn("user", roles)
        self.assertIn("tool", roles)
        self.assertTrue(any("echoed-result-payload" in (r[1] or "") for r in rows))   # tool output captured
        self.assertTrue(any("final answer text" in (r[1] or "") for r in rows))       # final answer captured
        self.assertTrue(any(r[2] == "echo" for r in rows))                            # tool name recorded

    def test_paging_off_persists_nothing(self):
        self._run(self._cfg(paging=False))
        db = bob_memory.get_db(self.db)
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcript'").fetchone()
        self.assertIsNone(row, "paging off must not even create the transcript table")

    def test_compacted_turn_is_still_retrievable(self):
        # An early turn dropped from context by compaction is still in the transcript store.
        self._run(self._cfg(paging=True))
        hits = bob_memory.transcript_search("plan", self.db, k=5)
        self.assertTrue(any("what is the plan" in h["content"] for h in hits))


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestConversationSearchTool(unittest.TestCase):
    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = tempfile.mkdtemp(prefix="bob-cs-tool-")
        self.db = f"{self.dir}/m.db"
        self.cfg = {"memory": {"enabled": True, "dbPath": self.db},
                    "agent": {"conversationPaging": True, "defaultOwner": "local"}}

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_wrapper_formats_hits_and_sentinel(self):
        self.assertEqual(bob_core.conversation_search("anything", config=self.cfg),
                         "(no matching earlier turns)")
        bob_memory.transcript_append("r", "tool", "the api key rotates monthly", self.db,
                                     owner="local", tool_name="shell")
        out = bob_core.conversation_search("api key", config=self.cfg)
        self.assertIn("api key", out)
        self.assertIn("[tool:shell]", out)

    def test_tool_gate_and_read_only(self):
        import importlib
        tool = importlib.import_module("conversation_search")   # scripts/tools/conversation_search.py
        self.assertTrue(tool.enabled(self.cfg))
        self.assertFalse(tool.enabled({"agent": {"conversationPaging": False},
                                       "memory": {"enabled": True}}))
        self.assertFalse(hasattr(tool, "MUTATING_TOOLS") and tool.MUTATING_TOOLS)  # read-only

    def test_paged_in_result_is_bounded_by_retention_seam(self):
        # A large paged-in result is a normal tool output -> truncated + retained (can't re-overflow).
        import importlib
        tr = importlib.import_module("tool_registry")
        reg = tr.ToolRegistry.__new__(tr.ToolRegistry)
        reg.max_result_chars = 100
        reg._result_store = {}
        reg._result_seq = 0
        reg._result_store_max = 8
        out = reg._truncate_and_retain("x" * 500)
        self.assertTrue(out.startswith("x" * 100))              # body capped at max_result_chars
        self.assertNotIn("x" * 101, out)                        # nothing beyond the cap kept inline
        self.assertIn("retained as r", out)                     # full text recoverable via read_result


if __name__ == "__main__":
    unittest.main()
