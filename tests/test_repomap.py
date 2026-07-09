"""Repo map: regex symbol extraction, PageRank ranking (referenced above unreferenced), the mtime cache
(only changed files reparse), denied/secret exclusion, and token-bounded rendering."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_repomap


class TestExtractTags(unittest.TestCase):
    def test_python_defs_and_refs(self):
        src = "import os\n\ndef alpha():\n    return beta()\n\nclass Gamma:\n    pass\n"
        tags = bob_repomap.extract_tags_regex("a.py", src)
        names = {n for n, _ in tags["defs"]}
        self.assertEqual(names, {"alpha", "Gamma"})
        self.assertIn("beta", tags["refs"])          # a call is a reference
        self.assertNotIn("alpha", tags["refs"])       # own defs excluded from refs

    def test_js_defs(self):
        src = "export function foo() {}\nconst bar = 3\nclass Baz {}\n"
        names = {n for n, _ in bob_repomap.extract_tags_regex("a.js", src)["defs"]}
        self.assertEqual(names, {"foo", "bar", "Baz"})


class TestBackendSelection(unittest.TestCase):
    def test_default_extractor_is_regex_without_treesitter(self):
        # grep_ast is not a hard dependency; the selector must degrade to the regex extractor.
        if not bob_repomap.treesitter_available():
            self.assertIs(bob_repomap.default_extractor(), bob_repomap.extract_tags_regex)

    def test_hybrid_falls_back_on_extractor_error(self):
        orig_avail = bob_repomap.treesitter_available
        orig_ts = bob_repomap.extract_tags_treesitter
        try:
            bob_repomap.treesitter_available = lambda: True
            def boom(path, source):
                raise RuntimeError("grammar missing")
            bob_repomap.extract_tags_treesitter = boom
            ex = bob_repomap.default_extractor()
            tags = ex("a.py", "def alpha():\n    pass\n")   # must not raise; regex handles it
            self.assertEqual({n for n, _ in tags["defs"]}, {"alpha"})
        finally:
            bob_repomap.treesitter_available = orig_avail
            bob_repomap.extract_tags_treesitter = orig_ts


class TestRepoMap(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-repomap-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _w(self, name, text):
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_ranks_referenced_symbol_above_leaf(self):
        # core.py's `hub` is referenced by three callers; leaf.py's `lonely` by none.
        self._w("core.py", "def hub():\n    return 1\n")
        self._w("leaf.py", "def lonely():\n    return 2\n")
        for i in range(3):
            self._w(f"caller{i}.py", "from core import hub\n\ndef go():\n    return hub()\n")
        rm = bob_repomap.RepoMap([self.dir])
        ranked = [rm._rel(p) for p in rm.ranked_files()]
        self.assertLess(ranked.index("core.py"), ranked.index("leaf.py"))

    def test_definitions_and_references(self):
        self._w("core.py", "def hub():\n    return 1\n")
        self._w("caller.py", "from core import hub\n\ndef go():\n    return hub()\n")
        rm = bob_repomap.RepoMap([self.dir])
        defs = rm.definitions("hub")
        self.assertEqual(len(defs), 1)
        self.assertTrue(defs[0][0].endswith("core.py"))
        refs = rm.references("hub")
        self.assertTrue(any(r.endswith("caller.py") for r in refs))

    def test_cache_reparses_only_changed(self):
        p = self._w("a.py", "def one():\n    return 1\n")
        rm = bob_repomap.RepoMap([self.dir])
        calls = {"n": 0}
        base = bob_repomap.extract_tags_regex
        rm.extractor = lambda path, src: (calls.__setitem__("n", calls["n"] + 1) or base(path, src))
        rm.build()
        first = calls["n"]
        rm.build()                          # nothing changed -> no reparse
        self.assertEqual(calls["n"], first)
        p.write_text("def one():\n    return 2\n\ndef two():\n    return 3\n", encoding="utf-8")
        import os, time
        os.utime(p, (time.time() + 1, time.time() + 1))   # bump mtime deterministically
        rm.build()
        self.assertEqual(calls["n"], first + 1)            # exactly the one changed file reparsed

    def test_never_indexes_denied_or_secret(self):
        self._w("ok.py", "def ok():\n    pass\n")
        self._w("config.json", '{"litellmKey":"SECRET"}')
        self._w(".env", "TOKEN=abc")
        rm = bob_repomap.RepoMap([self.dir])
        indexed = {p.name for p in rm.build()}
        self.assertIn("ok.py", indexed)
        self.assertNotIn("config.json", indexed)
        self.assertNotIn(".env", indexed)

    def test_render_map_respects_token_budget(self):
        for i in range(30):
            self._w(f"m{i}.py", f"def sym{i}_a():\n    pass\n\ndef sym{i}_b():\n    pass\n")
        rm = bob_repomap.RepoMap([self.dir])
        small = rm.render_map(token_budget=30)
        big = rm.render_map(token_budget=2000)
        self.assertLessEqual(bob_repomap._est_tokens(small), 30)
        self.assertGreater(len(big), len(small))       # a bigger budget yields a bigger map

    def test_render_map_excludes_in_context_files(self):
        core = self._w("core.py", "def hub():\n    pass\n")
        self._w("caller.py", "from core import hub\n\ndef go():\n    return hub()\n")
        rm = bob_repomap.RepoMap([self.dir])
        out = rm.render_map(exclude=[core])
        self.assertNotIn("core.py:", out)


if __name__ == "__main__":
    unittest.main()
