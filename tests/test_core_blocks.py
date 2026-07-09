"""Core-memory block store — agent-editable, size-capped, owner/scope-scoped named strings kept out of
the decaying `memories` table. Covers the storage layer (block_get/block_set/block_list); the agent tool
and injection live in test_core_blocks_tool / test_context."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — sys.path
import bob_core
import bob_memory


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestCoreBlockStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-block-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_set_get_round_trip(self):
        self.assertIsNone(bob_memory.block_get("task", self.db))
        stored, trimmed = bob_memory.block_set("task", "ship the reranker", self.db)
        self.assertEqual(stored, "ship the reranker")
        self.assertFalse(trimmed)
        self.assertEqual(bob_memory.block_get("task", self.db), "ship the reranker")

    def test_upsert_replaces(self):
        bob_memory.block_set("task", "first", self.db)
        bob_memory.block_set("task", "second", self.db)
        self.assertEqual(bob_memory.block_get("task", self.db), "second")

    def test_cap_trims_oldest_keeps_newest(self):
        stored, trimmed = bob_memory.block_set("task", "0123456789", self.db, cap=4)
        self.assertTrue(trimmed)
        self.assertEqual(stored, "6789")                       # newest tail kept, within cap
        self.assertEqual(bob_memory.block_get("task", self.db), "6789")

    def test_owner_and_scope_isolation(self):
        bob_memory.block_set("task", "alice-global", self.db, owner="alice")
        bob_memory.block_set("task", "bob-global", self.db, owner="bob")
        bob_memory.block_set("task", "alice-projX", self.db, owner="alice", scope="projX")
        self.assertEqual(bob_memory.block_get("task", self.db, owner="alice"), "alice-global")
        self.assertEqual(bob_memory.block_get("task", self.db, owner="bob"), "bob-global")
        self.assertEqual(bob_memory.block_get("task", self.db, owner="alice", scope="projX"), "alice-projX")
        self.assertIsNone(bob_memory.block_get("task", self.db, owner="carol"))

    def test_list_is_name_ordered(self):
        bob_memory.block_set("user", "u", self.db)
        bob_memory.block_set("task", "t", self.db)
        self.assertEqual(list(bob_memory.block_list(self.db).keys()), ["task", "user"])  # deterministic

    def test_lazy_table_not_created_by_plain_recall(self):
        # A DB that only ever did memory work must not carry the core_blocks table (byte-unchanged).
        bob_memory.get_db(self.db)
        db = bob_memory.get_db(self.db)
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='core_blocks'").fetchone()
        self.assertIsNone(row)


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestCoreBlockEditAndInject(unittest.TestCase):
    """The bob_core edit + injection wrappers the memory_block tool routes through, and the always-on
    injection block."""
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-blockcore-"))
        # Absolute dbPath -> _get_db_path returns it as-is (REPO / abs == abs), keeping the test hermetic.
        self.cfg = {"memory": {"enabled": True, "dbPath": str(self.dir / "m.db"),
                               "coreBlocks": {"task": 40, "user": 40}},
                    "agent": {"defaultOwner": "local"}}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_append_then_injected(self):
        bob_core.memory_block_edit("append", "task", "wire the reranker", config=self.cfg)
        block = bob_core.core_blocks_block(config=self.cfg)
        self.assertIn("[task]", block)
        self.assertIn("wire the reranker", block)
        self.assertTrue(block.startswith(bob_core.CORE_BLOCKS_FRAME))

    def test_replace_overwrites(self):
        bob_core.memory_block_edit("append", "task", "old", config=self.cfg)
        bob_core.memory_block_edit("replace", "task", "new goal", config=self.cfg)
        self.assertEqual(bob_memory.block_get("task", self.cfg["memory"]["dbPath"]), "new goal")

    def test_unknown_block_rejected(self):
        out = bob_core.memory_block_edit("append", "nope", "x", config=self.cfg)
        self.assertIn("Unknown core-memory block", out)

    def test_cap_enforced_through_edit(self):
        out = bob_core.memory_block_edit("replace", "task", "y" * 100, config=self.cfg)
        self.assertIn("trimmed", out)
        self.assertEqual(len(bob_memory.block_get("task", self.cfg["memory"]["dbPath"])), 40)

    def test_injection_deterministic_and_sorted(self):
        bob_core.memory_block_edit("replace", "user", "likes dark mode", config=self.cfg)
        bob_core.memory_block_edit("replace", "task", "ship R", config=self.cfg)
        a = bob_core.core_blocks_block(config=self.cfg)
        b = bob_core.core_blocks_block(config=self.cfg)
        self.assertEqual(a, b)                                   # byte-identical across turns
        self.assertLess(a.index("[task]"), a.index("[user]"))    # name-sorted order

    def test_off_returns_none(self):
        off = {"memory": {"enabled": True, "dbPath": str(self.dir / "m.db"), "coreBlocks": {}}}
        self.assertIsNone(bob_core.core_blocks_block(config=off))
        disabled = {"memory": {"enabled": False, "coreBlocks": {"task": 40}}}
        self.assertIsNone(bob_core.core_blocks_block(config=disabled))

    def test_tool_gate_and_mutating(self):
        import importlib
        tool = importlib.import_module("memory_block")           # scripts/tools/memory_block.py
        self.assertTrue(tool.enabled(self.cfg))
        self.assertFalse(tool.enabled({"memory": {"enabled": True, "coreBlocks": {}}}))
        self.assertIn("memory_block", tool.MUTATING_TOOLS)


if __name__ == "__main__":
    unittest.main()
