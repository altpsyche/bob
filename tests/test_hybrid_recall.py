"""Hybrid recall (dense + BM25/FTS5 + Reciprocal Rank Fusion). `retrieval='hybrid'` fuses the
dense cosine ranking with a SQLite FTS5 BM25 ranking so a lexically-exact hit that dense ranks poorly
still surfaces; the recency/type/usage/salience terms apply on top of the fused set. `retrieval='dense'`
(default) matches the dense-only path — it doesn't even create the FTS index — and also backstops
hybrid when the SQLite build lacks FTS5."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — sys.path
import bob_memory

# Query embeds to this; row embeddings are stored verbatim below.
_QVEC = [1.0, 0.0, 0.0]


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class _HybridBase(unittest.TestCase):
    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = lambda text: list(_QVEC)
        self.dir = Path(tempfile.mkdtemp(prefix="bob-hybrid-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _insert(self, content, vec):
        db = bob_memory.get_db(self.db)
        db["memories"].insert({
            "content": content, "content_hash": bob_memory._content_hash(content),
            "embedding": json.dumps(vec), "type": "fact", "subject": "user",
            "owner_id": "local", "salience": 1.0,
        })
        db.conn.commit()

    def _seed(self):
        # A is the lexical target (matches the query words) but is ORTHOGONAL to the query vector, so a
        # dense-only scan ranks it last. B/C/D are near the query vector but share no query words.
        self._insert("kubernetes deployment yaml manifest", [0.0, 1.0, 0.0])
        self._insert("grocery milk eggs bread", [0.95, 0.05, 0.0])
        self._insert("weekend hiking trip plans", [0.90, 0.10, 0.0])
        self._insert("favorite science fiction movies", [0.85, 0.15, 0.0])


class TestHybridBeatsDense(_HybridBase):
    def test_hybrid_surfaces_lexical_match_dense_ranks_last(self):
        self._seed()
        q = "kubernetes deployment yaml"
        dense = bob_memory.recall(q, self.db, k=1, threshold=0.0, retrieval="dense")
        hybrid = bob_memory.recall(q, self.db, k=1, threshold=0.0, retrieval="hybrid")
        self.assertNotIn("kubernetes", dense[0]["content"])   # dense top is a high-cosine distractor
        self.assertIn("kubernetes", hybrid[0]["content"])      # hybrid pulls the exact lexical match up

    def test_dense_ranks_the_lexical_match_last(self):
        self._seed()
        dense = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0,
                                  retrieval="dense")
        self.assertIn("kubernetes", dense[-1]["content"])      # confirms dense really missed it


class TestDeterminismAndDefault(_HybridBase):
    def test_rrf_order_deterministic(self):
        self._seed()
        a = bob_memory.recall("kubernetes deployment", self.db, k=4, threshold=0.0, retrieval="hybrid")
        b = bob_memory.recall("kubernetes deployment", self.db, k=4, threshold=0.0, retrieval="hybrid")
        self.assertEqual([x["id"] for x in a], [x["id"] for x in b])

    def test_dense_default_does_not_create_fts_index(self):
        self._seed()
        bob_memory.recall("kubernetes", self.db, retrieval="dense", threshold=0.0)
        db = bob_memory.get_db(self.db)
        row = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone()
        self.assertIsNone(row, "dense mode must not materialize the FTS index (byte-unchanged DB)")


class TestFallbacks(_HybridBase):
    def test_fts_absent_falls_back_to_dense(self):
        self._seed()
        orig = bob_memory._ensure_fts
        bob_memory._ensure_fts = lambda db: False       # simulate a SQLite build without FTS5
        try:
            hits = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0,
                                     retrieval="hybrid")
        finally:
            bob_memory._ensure_fts = orig
        self.assertTrue(hits)                            # no crash; dense-over-candidates
        self.assertNotIn("kubernetes", hits[0]["content"])  # behaves like dense without the lexical half

    def test_empty_query_returns_empty(self):
        self._seed()
        self.assertEqual(bob_memory.recall("   ", self.db, retrieval="hybrid"), [])


class TestFtsPlumbing(_HybridBase):
    def test_match_query_sanitizes(self):
        self.assertIsNone(bob_memory._fts_match_query("...!?"))
        self.assertEqual(bob_memory._fts_match_query("API key!"), '"api" OR "key"')

    def test_trigger_indexes_new_rows(self):
        self._seed()
        # First hybrid recall builds + backfills the FTS index and installs the sync triggers.
        bob_memory.recall("kubernetes", self.db, retrieval="hybrid", threshold=0.0)
        # A row inserted AFTER the index exists must be indexed by the AFTER INSERT trigger.
        self._insert("elasticsearch cluster shard tuning", [0.0, 0.0, 1.0])
        db = bob_memory.get_db(self.db)
        from datetime import datetime, timezone
        ids = bob_memory._bm25_ranked_ids(db, "elasticsearch", "local", None,
                                          datetime.now(timezone.utc).isoformat(), None, 25)
        found = {r[0] for r in db.execute(
            "SELECT id FROM memories WHERE content LIKE 'elasticsearch%'").fetchall()}
        self.assertTrue(found & set(ids), "AFTER INSERT trigger did not index the new row")


if __name__ == "__main__":
    unittest.main()
