"""Cross-encoder rerank of the fused candidate set. `rerank=True` adds a second-stage rescoring pass
over the top-N hybrid candidates: the reranker score, min-max-normalized to 0-1, replaces the semantic
term before the recency/type/salience blend. Off reproduces hybrid exactly; a missing reranker
loud-fails back to the fused order. The reranker call (`_rerank_scores`) is monkeypatched here so the
suite stays hermetic (no live model), mirroring the `embed` fake in test_hybrid_recall."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — sys.path
import bob_memory

_QVEC = [1.0, 0.0, 0.0]


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class _RerankBase(unittest.TestCase):
    def setUp(self):
        self._orig_embed = bob_memory.embed
        self._orig_rerank = bob_memory._rerank_scores
        bob_memory.embed = lambda text: list(_QVEC)
        bob_memory._warned.clear()
        self.dir = Path(tempfile.mkdtemp(prefix="bob-rerank-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig_embed
        bob_memory._rerank_scores = self._orig_rerank
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
        # Same fixture shape as the hybrid suite: A is the lexical target but orthogonal to the query
        # vector (dense ranks it last); B/C/D are near the query vector but lexically unrelated.
        self._insert("kubernetes deployment yaml manifest", [0.0, 1.0, 0.0])
        self._insert("grocery milk eggs bread", [0.95, 0.05, 0.0])
        self._insert("weekend hiking trip plans", [0.90, 0.10, 0.0])
        self._insert("favorite science fiction movies", [0.85, 0.15, 0.0])

    def _fake_reranker(self, score_by_keyword):
        """Install a reranker that scores each doc by the first matching keyword, else 0."""
        def _scores(query, docs, base_url=None):
            out = []
            for d in docs:
                s = 0.0
                for kw, val in score_by_keyword.items():
                    if kw in d:
                        s = val
                        break
                out.append(s)
            return out
        bob_memory._rerank_scores = _scores


class TestRerankReorders(_RerankBase):
    def test_rerank_promotes_semantic_winner(self):
        self._seed()
        # The reranker judges the kubernetes doc most relevant; it must surface to rank 1 even though
        # dense ranks it last and hybrid alone does not necessarily put it first.
        self._fake_reranker({"kubernetes": 1.0})
        hits = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0, rerank=True)
        self.assertIn("kubernetes", hits[0]["content"])

    def test_rerank_normalizes_unbounded_logits(self):
        self._seed()
        # bge-style unbounded logits (incl. negatives) must min-max normalize and still order correctly.
        self._fake_reranker({"kubernetes": 8.4, "grocery": -3.0, "hiking": -5.0, "science": 1.2})
        hits = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0, rerank=True)
        self.assertIn("kubernetes", hits[0]["content"])
        self.assertTrue(hits, "normalized scores must still clear the threshold")

    def test_rerank_applies_on_dense_fallback_branch(self):
        self._seed()
        # A query with no lexical hits leaves bm25 empty -> the dense-fallback branch. Rerank must still
        # run there. The reranker prefers the science doc; it should lead.
        self._fake_reranker({"science": 1.0})
        hits = bob_memory.recall("zzzzz nonexistent tokens", self.db, k=4, threshold=0.0, rerank=True)
        self.assertIn("science", hits[0]["content"])


class TestRerankOffAndFallback(_RerankBase):
    def test_off_reproduces_hybrid(self):
        self._seed()
        # Rerank off must be identical to plain hybrid recall (byte-for-byte ordering).
        def _boom(query, docs, base_url=None):
            raise AssertionError("reranker must not be called when rerank is off")
        bob_memory._rerank_scores = _boom
        hybrid = bob_memory.recall("kubernetes deployment", self.db, k=4, threshold=0.0, retrieval="hybrid")
        off = bob_memory.recall("kubernetes deployment", self.db, k=4, threshold=0.0,
                                retrieval="hybrid", rerank=False)
        self.assertEqual([x["id"] for x in hybrid], [x["id"] for x in off])

    def test_missing_reranker_falls_back_to_hybrid(self):
        self._seed()
        baseline = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0,
                                     retrieval="hybrid")
        def _unreachable(query, docs, base_url=None):
            raise RuntimeError("no reranker model at /rerank")
        bob_memory._rerank_scores = _unreachable
        hits = bob_memory.recall("kubernetes deployment yaml", self.db, k=4, threshold=0.0, rerank=True)
        self.assertEqual([x["id"] for x in hits], [x["id"] for x in baseline])
        self.assertTrue(any("rerank unavailable" in w for w in bob_memory._warned),
                        "a missing reranker must log a one-time fallback warning")


if __name__ == "__main__":
    unittest.main()
