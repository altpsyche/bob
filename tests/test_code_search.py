"""code_search tool: gated on agent.repoMap, read-only, resolves defs/refs and a ranked map over the
allowedReadPaths roots, and never reaches outside them."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ + scripts/tools on sys.path
import code_search
from tool_registry import ToolRegistry


class TestGating(unittest.TestCase):
    def test_gated_off_by_default(self):
        off = ToolRegistry.build(_common.fake_config(), set())
        self.assertNotIn("code_search", off.dispatch)

    def test_loaded_and_read_only_when_on(self):
        on = ToolRegistry.build(
            _common.fake_config(agent={"toolFormat": "hermes", "maxSteps": 5, "maxToolResultTokens": 1000,
                                       "repoMap": True, "allowedReadPaths": ["."]}), set())
        self.assertIn("code_search", on.dispatch)
        self.assertNotIn("code_search", on.mutating_tools)   # navigation is read-only


class TestQueries(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-cs-"))
        (self.dir / "core.py").write_text("def hub():\n    return 1\n", encoding="utf-8")
        (self.dir / "caller.py").write_text(
            "from core import hub\n\ndef go():\n    return hub()\n", encoding="utf-8")
        code_search.configure({"agent": {"repoMap": True, "allowedReadPaths": [str(self.dir)],
                                         "repoMapTokens": 1024}})

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_def_resolves(self):
        out = code_search._code_search(action="def", symbol="hub")
        self.assertIn("core.py:1", out)

    def test_refs_lists_callers(self):
        out = code_search._code_search(action="refs", symbol="hub")
        self.assertIn("caller.py", out)

    def test_map_renders(self):
        out = code_search._code_search(action="map", query="hub")
        self.assertIn("core.py", out)

    def test_refuses_path_outside_roots(self):
        other = Path(tempfile.mkdtemp(prefix="bob-cs-other-"))
        try:
            (other / "secret_code.py").write_text("def leak():\n    pass\n", encoding="utf-8")
            # 'leak' is defined only outside the allowed root -> not found.
            out = code_search._code_search(action="def", symbol="leak")
            self.assertIn("No definition", out)
        finally:
            shutil.rmtree(other, ignore_errors=True)


class TestSemanticIndex(unittest.TestCase):
    """Mode B reuses bob_memory (contextual-chunk store + hybrid recall) in a separate code db under a
    synthetic owner, so code chunks never leak into the user's memory recall."""

    def setUp(self):
        import bob_memory
        self.bm = bob_memory
        self.dir = Path(tempfile.mkdtemp(prefix="bob-cs-sem-"))
        (self.dir / "core.py").write_text(
            "def compute_total(items):\n    return sum(items)\n", encoding="utf-8")
        self.code_db = self.dir / "code.db"
        self.user_db = self.dir / "bob.db"
        # Deterministic fake embedder: a tiny vector keyed by whether the text mentions 'total'.
        self._orig_embed = bob_memory.embed
        bob_memory.embed = lambda text: [1.0, 0.0] if "total" in text.lower() else [0.0, 1.0]

    def tearDown(self):
        self.bm.embed = self._orig_embed
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_index_stores_code_chunks_with_context_and_owner(self):
        import bob_repomap
        seen = {}
        orig_store = self.bm.store

        def spy_store(content, db_path, **kw):
            seen.setdefault("calls", []).append({"content": content, "db_path": str(db_path), **kw})
            return orig_store(content, db_path, **kw)

        self.bm.store = spy_store
        try:
            repo = bob_repomap.RepoMap([self.dir])
            n = bob_repomap.index_semantic(repo, db_path=self.code_db, scope="proj")
        finally:
            self.bm.store = orig_store
        self.assertGreaterEqual(n, 1)
        call = seen["calls"][0]
        self.assertEqual(call["owner"], bob_repomap.CODE_OWNER)
        self.assertEqual(call["mem_type"], bob_repomap.CODE_TYPE)
        self.assertEqual(call["scope"], "proj")
        self.assertTrue(call["context"].startswith("File core.py - symbol compute_total"))
        self.assertEqual(call["db_path"], str(self.code_db))

    def test_code_chunks_isolated_from_user_recall(self):
        import bob_repomap
        # Index code into code.db; store a user memory into bob.db.
        repo = bob_repomap.RepoMap([self.dir])
        bob_repomap.index_semantic(repo, db_path=self.code_db, scope="proj")
        self.bm.store("The total budget is fixed.", self.user_db, owner="local", mem_type="fact")
        # A user recall on bob.db must not surface code chunks (different db + owner).
        hits = self.bm.recall("compute total", self.user_db, owner="local")
        self.assertFalse(any("def compute_total" in h.get("content", "") for h in hits))


if __name__ == "__main__":
    unittest.main()
