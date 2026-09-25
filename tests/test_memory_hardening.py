"""bob_memory correctness under real-world conditions: clear really clears, the recall threshold gates on
relevance (not on salience/recency), forgotten rows stay forgotten, inputs are fitted to the embed /
rerank / chat roles' limits, vectors are stamped on every write path, a profile with no embedder degrades
to keyword memory, and the DB is safe to share between processes. Hermetic: embed, rerank and the HTTP
helper are faked; no live model or network."""
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import _common  # noqa: F401 (sys.path + hermetic profile)
import bob_memory
import bob_repomap

_DEPS = unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                            f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")


def _topic_embed(text: str):
    """Topic vectors: rows about the same topic are near-identical, different topics are orthogonal."""
    t = text.lower()
    if "coffee" in t or "espresso" in t:
        return [1.0, 0.0, 0.0]
    if "rust" in t or "cargo" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig_embed = bob_memory.embed
        self._orig_rerank = bob_memory._rerank_scores
        bob_memory.embed = _topic_embed
        bob_memory._warned.clear()
        self.dir = Path(tempfile.mkdtemp(prefix="bob-memh-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig_embed
        bob_memory._rerank_scores = self._orig_rerank
        shutil.rmtree(self.dir, ignore_errors=True)


@_DEPS
class TestClear(_Base):
    def test_clear_commits_and_wipes_every_tier(self):
        bob_memory.store("User drinks espresso", self.db)
        bob_memory.block_set("task", "ship it", self.db)
        bob_memory.transcript_append("r1", "user", "talk about espresso", self.db)
        bob_memory.recall("espresso", self.db, retrieval="hybrid", threshold=0.0)   # builds memories_fts
        bob_memory.transcript_search("espresso", self.db)                         # builds transcript_fts
        bob_memory.cmd_clear(True, self.db)
        conn = sqlite3.connect(str(self.db), timeout=0)   # a second connection sees the committed wipe
        try:
            for table in ("memories", "core_blocks", "transcript"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'espresso'").fetchone()[0], 0)
            conn.execute("INSERT INTO core_blocks (name, content) VALUES ('x', 'y')")   # not locked
            conn.commit()
        finally:
            conn.close()


@_DEPS
class TestRecallGate(_Base):
    def _seed(self):
        bob_memory.store("User drinks espresso every morning", self.db, mem_type="preference")
        bob_memory.store("User writes Rust with cargo", self.db, mem_type="profile")

    def test_unrelated_query_returns_nothing(self):
        self._seed()
        # Orthogonal to both rows: salience + recency + type alone used to clear 0.35.
        self.assertEqual(bob_memory.recall("zzz totally unrelated", self.db, threshold=0.35), [])

    def test_related_query_still_blends(self):
        self._seed()
        hits = bob_memory.recall("coffee", self.db, threshold=0.35)
        self.assertEqual(len(hits), 1)
        self.assertIn("espresso", hits[0]["content"])
        self.assertGreater(hits[0]["score"], 1.0)   # survivors still get the non-semantic terms

    def test_forget_by_query_needs_a_real_match(self):
        self._seed()
        self.assertEqual(bob_memory.forget_by_query("zzz totally unrelated", self.db), [])
        self.assertEqual(len(bob_memory.list_memories(self.db)), 2)

    def test_forget_candidates_do_not_bump_use_count(self):
        self._seed()
        bob_memory.forget_candidates("coffee", self.db)
        db = bob_memory.get_db(self.db)
        self.assertEqual(db.execute("SELECT SUM(use_count) FROM memories").fetchone()[0], 0)

    def test_rerank_normalization_cannot_promote_irrelevant_docs(self):
        self._seed()
        bob_memory._rerank_scores = lambda q, docs, base_url=None: [0.01 for _ in docs]
        self.assertEqual(bob_memory.recall("zzz unrelated", self.db, threshold=0.35, rerank=True), [])

    def test_rerank_raw_score_is_the_gate(self):
        self._seed()
        bob_memory._rerank_scores = lambda q, docs, base_url=None: [0.9 if "Rust" in d else 0.02 for d in docs]
        hits = bob_memory.recall("which language", self.db, threshold=0.35, rerank=True)
        self.assertEqual([h["content"] for h in hits], ["User writes Rust with cargo"])

    def test_hybrid_weak_keyword_overlap_is_gated(self):
        self._seed()
        # Shares only "every" with a row: a BM25 hit, but not evidence of relevance.
        self.assertEqual(bob_memory.recall("every tuesday zzz", self.db, threshold=0.35,
                                           retrieval="hybrid"), [])
        # A query that covers the row's content words passes on lexical evidence alone.
        bob_memory.embed = lambda text: [0.0, 0.0, 1.0]
        hits = bob_memory.recall("espresso morning", self.db, threshold=0.35, retrieval="hybrid")
        self.assertTrue(hits and "espresso" in hits[0]["content"])

    def test_cli_forget_query_confirms_unless_yes(self):
        self._seed()
        with mock.patch("builtins.input", return_value="n"), mock.patch("builtins.print"):
            bob_memory.cmd_forget(None, "coffee", self.db, "local")
        self.assertEqual(len(bob_memory.list_memories(self.db)), 2)
        with mock.patch("builtins.print"):
            bob_memory.cmd_forget(None, "coffee", self.db, "local", yes=True)
        self.assertEqual(len(bob_memory.list_memories(self.db)), 1)


@_DEPS
class TestForgottenRowsStayForgotten(_Base):
    def test_profile_block_skips_forgotten(self):
        mid, _ = bob_memory.store("User's name is Siva", self.db, mem_type="profile")
        bob_memory.forget(mid, self.db)
        self.assertIsNone(bob_memory.profile_block("local", self.db))

    def test_forgotten_fact_can_be_stored_again(self):
        mid, _ = bob_memory.store("User drinks espresso", self.db)
        bob_memory.forget(mid, self.db)
        new_id, is_new = bob_memory.store("User drinks espresso", self.db)      # exact dedup
        self.assertTrue(is_new)
        self.assertNotEqual(new_id, mid)
        bob_memory.forget(new_id, self.db)
        _nid, is_new = bob_memory.store("User loves coffee", self.db)            # near dedup
        self.assertTrue(is_new)


@_DEPS
class TestInputLimits(_Base):
    def setUp(self):
        super().setUp()
        bob_memory.embed = self._orig_embed
        bob_memory._rerank_scores = self._orig_rerank
        self.sent = []

    def _limits(self, table):
        return mock.patch.object(bob_memory, "_role_limits", side_effect=lambda role: table.get(role, (None, None)))

    def test_embed_truncates_to_the_role_context(self):
        def fake_post(path, payload, timeout=None, base_url=None):
            self.sent.append(payload)
            return {"data": [{"embedding": [1.0]}]}
        with self._limits({"embed": (2048, 128)}), mock.patch.object(bob_memory, "_post_json", fake_post):
            bob_memory.embed("x" * 50000)
        text = self.sent[0]["input"][0]
        self.assertLess(len(text), 2048 * 3)
        self.assertTrue(text.startswith("x") and text.endswith("x"))

    def test_embed_retries_smaller_when_still_too_large(self):
        def fake_post(path, payload, timeout=None, base_url=None):
            self.sent.append(len(payload["input"][0]))
            if len(self.sent) == 1:
                raise bob_memory.MemoryHTTPError("u", 500, "input (2100 tokens) is too large to process")
            return {"data": [{"embedding": [1.0]}]}
        with self._limits({"embed": (2048, 128)}), mock.patch.object(bob_memory, "_post_json", fake_post):
            bob_memory.embed("y" * 50000)
        self.assertEqual(len(self.sent), 2)
        self.assertLess(self.sent[1], self.sent[0])

    def test_embed_uses_the_configured_model_name(self):
        def fake_post(path, payload, timeout=None, base_url=None):
            self.sent.append(payload["model"])
            return {"data": [{"embedding": [1.0]}]}
        roles = {"roles": {"my-embed": {"gguf": "e.gguf", "ctx": 2048}}, "defaults": {}}
        with mock.patch.object(bob_memory, "_app_config", return_value={"memory": {"embedModel": "my-embed"}}), \
                mock.patch.object(bob_memory, "_registry", return_value=roles), \
                mock.patch.object(bob_memory, "_post_json", fake_post):
            bob_memory.embed("hello")
        self.assertEqual(self.sent, ["my-embed"])

    def test_rerank_fits_query_and_docs_to_the_ubatch(self):
        def fake_post(path, payload, timeout=None, base_url=None):
            self.sent.append(payload)
            return {"results": [{"index": i, "relevance_score": 0.5} for i in range(len(payload["documents"]))]}
        with self._limits({"rerank": (2048, 2048)}), mock.patch.object(bob_memory, "_post_json", fake_post):
            bob_memory._rerank_scores("q" * 9000, ["d" * 20000, "short"])
        p = self.sent[0]
        budget_chars = (2048 - bob_memory._RERANK_TEMPLATE_TOKENS) * bob_memory._CHARS_PER_TOKEN
        self.assertLessEqual(len(p["query"]) + len(p["documents"][0]), budget_chars + 10)
        self.assertEqual(p["documents"][1], "short")

    def test_rerank_refuses_a_ubatch_too_small_to_rank(self):
        with self._limits({"rerank": (2048, 128)}):
            with self.assertRaises(RuntimeError) as cm:
                bob_memory._rerank_scores("q", ["d"])
        self.assertIn("-ub", str(cm.exception))

    def test_each_distinct_rerank_failure_is_warned(self):
        bob_memory.embed = _topic_embed
        bob_memory.store("User drinks espresso", self.db)
        reasons = iter(["HTTP 500: input too large", "connection refused"])

        def failing(q, docs, base_url=None):
            raise RuntimeError(next(reasons))
        bob_memory._rerank_scores = failing
        with mock.patch("sys.stderr"):
            bob_memory.recall("coffee", self.db, rerank=True, threshold=0.0)
            bob_memory.recall("coffee", self.db, rerank=True, threshold=0.0)
        msgs = [m for m in bob_memory._warned if "rerank unavailable" in m]
        self.assertEqual(len(msgs), 2)


@_DEPS
class TestHttpHelper(unittest.TestCase):
    def test_non_2xx_and_transport_errors_raise_runtimeerror(self):
        import requests
        resp = mock.Mock(status_code=500, text="boom")
        with mock.patch.object(bob_memory, "_litellm", return_value=("http://x/v1", {})):
            with mock.patch("requests.post", return_value=resp):
                with self.assertRaises(bob_memory.MemoryHTTPError) as cm:
                    bob_memory._post_json("embeddings", {})
                self.assertEqual(cm.exception.status, 500)
            with mock.patch("requests.post", side_effect=requests.ConnectionError("down")):
                with self.assertRaises(RuntimeError):
                    bob_memory._post_json("embeddings", {})


@_DEPS
class TestSummarize(unittest.TestCase):
    def setUp(self):
        self.sent = []

    def _post(self, content="summary", finish="stop"):
        def fake_post(path, payload, timeout=None, base_url=None):
            self.sent.append(payload)
            return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}
        return mock.patch.object(bob_memory, "_post_json", fake_post)

    def test_local_role_gets_thinking_off_and_fitted_input(self):
        turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "w" * 400}
                 for i in range(200)]
        with self._post(), mock.patch.object(bob_memory, "_role_limits", return_value=(4096, 512)):
            out = bob_memory.summarize_turns(turns, model="chat", max_tokens=512)
        self.assertEqual(out, "summary")
        p = self.sent[0]
        self.assertEqual(p["chat_template_kwargs"], {"enable_thinking": False})
        body = p["messages"][1]["content"]
        self.assertLess(len(body) / bob_memory._CHARS_PER_TOKEN, 4096 - 512)
        self.assertIn("turn 199", body)            # the newest turns are the ones kept
        self.assertNotIn('"turn 0 ', body)

    def test_cloud_peer_gets_no_template_kwargs(self):
        with self._post(), mock.patch.object(bob_memory, "_role_limits", return_value=(None, None)):
            bob_memory.summarize_turns([{"role": "user", "content": "hi"}], model="deepseek-pro")
        self.assertNotIn("chat_template_kwargs", self.sent[0])

    def test_none_content_and_length_cutoff_are_handled(self):
        with self._post(content=None, finish="length"), mock.patch("sys.stderr"):
            self.assertEqual(bob_memory.summarize_turns([{"role": "user", "content": "hi"}]), "")
        self.assertTrue(any("finish_reason=length" in m for m in bob_memory._warned))


@_DEPS
class TestStampingAndProfiles(_Base):
    def test_edit_stamps_the_embed_model(self):
        mid, _ = bob_memory.store("User drinks espresso", self.db)
        new_id = bob_memory.edit(mid, "User drinks tea", self.db)
        db = bob_memory.get_db(self.db)
        stamp = db.execute("SELECT embed_model FROM memories WHERE id=?", [new_id]).fetchone()[0]
        self.assertEqual(stamp, bob_memory.current_embed_model())
        self.assertIsNotNone(stamp)

    def test_transcript_rows_are_stamped_and_old_tables_migrate(self):
        conn = sqlite3.connect(str(self.db))
        conn.execute("CREATE TABLE transcript (id INTEGER PRIMARY KEY, run_id TEXT, owner_id TEXT NOT NULL "
                     "DEFAULT 'local', scope TEXT, seq INTEGER, role TEXT NOT NULL, content TEXT NOT NULL, "
                     "tool_name TEXT, created_at TEXT, embedding TEXT)")
        conn.commit()
        conn.close()
        bob_memory.transcript_append("r", "user", "hello there", self.db, session_id="s1")
        db = bob_memory.get_db(self.db)
        model, sid = db.execute("SELECT embed_model, session_id FROM transcript").fetchone()
        self.assertEqual(model, bob_memory.current_embed_model())
        self.assertEqual(sid, "s1")

    def test_embed_stamp_follows_a_profile_switch(self):
        state = {"key": 1, "gguf": "a.gguf"}
        with mock.patch.object(bob_memory, "_registry_key", side_effect=lambda: state["key"]), \
                mock.patch("bob_models.load_models_config", return_value={"defaults": {}}), \
                mock.patch("bob_models.profile_roles",
                           side_effect=lambda config=None: {"embed": {"gguf": state["gguf"]}}):
            bob_memory._REGISTRY.clear()
            self.assertEqual(bob_memory.current_embed_model(), "a.gguf")
            state.update(key=2, gguf="b.gguf")
            self.assertEqual(bob_memory.current_embed_model(), "b.gguf")
        bob_memory._REGISTRY.clear()

    def test_no_embed_role_degrades_to_keyword_memory(self):
        bob_memory.embed = self._orig_embed          # the real embed: must not reach HTTP
        cpu = {"roles": {"chat": {"gguf": "c.gguf", "ctx": 4096}}, "defaults": {}}
        with mock.patch.object(bob_memory, "_registry", return_value=cpu), \
                mock.patch.object(bob_memory, "_post_json", side_effect=AssertionError("no HTTP")):
            self.assertFalse(bob_memory.semantic_available())
            self.assertIsNone(bob_memory.current_embed_model())
            mid, is_new = bob_memory.store("User drinks espresso every morning", self.db)
            self.assertTrue(is_new)
            hits = bob_memory.recall("espresso morning", self.db)
            self.assertEqual([h["id"] for h in hits], [mid])
            self.assertEqual(bob_memory.stale_vector_count(self.db), 0)
            with self.assertRaises(RuntimeError):
                bob_memory.forget_by_query("espresso", self.db)
            with mock.patch.object(bob_memory, "summarize_turns",
                                   return_value="preference: User likes tea | NEW | 5"):
                res = bob_memory.consolidate_session(
                    [{"role": "user", "content": "I like tea"}, {"role": "assistant", "content": "ok"}],
                    self.db)
        self.assertEqual(res["facts"], 1)

    def test_consolidation_keeps_facts_when_embed_is_down(self):
        def down(_):
            raise RuntimeError("embed server down")
        bob_memory.embed = down
        orig = bob_memory.summarize_turns
        bob_memory.summarize_turns = lambda *a, **k: ("preference: User likes tea | NEW | 5\n"
                                                      "project: User builds Bob | NEW | 7")
        try:
            with mock.patch("sys.stderr"):
                res = bob_memory.consolidate_session(
                    [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}], self.db)
        finally:
            bob_memory.summarize_turns = orig
        self.assertEqual(res["facts"], 2)

    def test_reembed_fills_missing_vectors(self):
        def down(_):
            raise RuntimeError("embed server down")
        bob_memory.embed = down
        bob_memory.store("User drinks espresso", self.db, embed_optional=True)
        bob_memory.embed = _topic_embed
        self.assertEqual(bob_memory.rebuild_vectors(self.db), 1)
        self.assertTrue(bob_memory.recall("coffee", self.db))


@_DEPS
class TestConcurrency(_Base):
    def test_wal_and_busy_timeout(self):
        db = bob_memory.get_db(self.db)
        self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(db.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_no_connection_is_left_holding_a_write(self):
        mid, _ = bob_memory.store("User drinks espresso", self.db)
        bob_memory.forget(mid, self.db)
        bob_memory.set_pinned(mid, self.db, True)
        conn = sqlite3.connect(str(self.db), timeout=0)
        try:
            conn.execute("BEGIN IMMEDIATE")   # would raise "database is locked" if a write were open
            conn.rollback()
        finally:
            conn.close()

    def test_concurrent_transcript_appends_get_unique_seqs(self):
        bob_memory.embed = lambda text: [1.0, 0.0, 0.0]
        errors = []

        def worker(n):
            try:
                for i in range(10):
                    bob_memory.transcript_append("run", "user", f"turn {n}-{i}", self.db)
            except Exception as e:   # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        db = bob_memory.get_db(self.db)
        seqs = [r[0] for r in db.execute("SELECT seq FROM transcript WHERE run_id='run'").fetchall()]
        self.assertEqual(sorted(seqs), list(range(40)))

    def test_concurrent_first_opens_of_a_new_db(self):
        """Opening a brand-new DB from several threads at once: the WAL switch ignores busy_timeout, so it
        is read first and retried instead of failing one opener with "database is locked"."""
        errors, barrier = [], threading.Barrier(6)

        def opener():
            try:
                barrier.wait()
                bob_memory.get_db(self.db).conn.close()
            except Exception as e:   # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=opener) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(bob_memory.get_db(self.db).execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_wal_switch_retries_a_locked_db(self):
        calls = []

        class _Db:
            def execute(self, sql):
                calls.append(sql)
                if len(calls) <= 2:
                    raise sqlite3.OperationalError("database is locked")
                return mock.Mock(fetchone=lambda: ["delete"])

        bob_memory._ensure_wal(_Db())
        self.assertEqual(calls[-1], "PRAGMA journal_mode = WAL")
        with self.assertRaises(sqlite3.OperationalError):
            bob_memory._ensure_wal(mock.Mock(execute=mock.Mock(side_effect=sqlite3.OperationalError("no such x"))))

    def test_fts_setup_is_idempotent_across_connections(self):
        bob_memory.store("User drinks espresso", self.db)
        a, b = bob_memory.get_db(self.db), bob_memory.get_db(self.db)
        self.assertTrue(bob_memory._ensure_fts(a))
        self.assertTrue(bob_memory._ensure_fts(b))
        n = a.execute("SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'espresso'").fetchone()[0]
        self.assertEqual(n, 1)   # backfilled once, not twice


@_DEPS
class TestFtsUpdateTrigger(_Base):
    def test_trigger_only_fires_on_content_and_old_dbs_upgrade(self):
        bob_memory.store("User drinks espresso", self.db)
        db = bob_memory.get_db(self.db)
        bob_memory._ensure_fts(db)
        # Simulate a DB built with the old every-update trigger.
        db.execute("DROP TRIGGER memories_fts_au")
        db.execute("CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN "
                   "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); "
                   "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END")
        db.conn.commit()
        bob_memory.recall("coffee", self.db, retrieval="hybrid", threshold=0.0)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='memories_fts_au'").fetchone()[0]
        self.assertIn("UPDATE OF content", sql)
        db.execute("UPDATE memories SET content='User drinks matcha'")
        db.conn.commit()
        self.assertEqual(db.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH 'matcha'").fetchone()[0], 1)


@_DEPS
class TestTranscriptRetention(_Base):
    def test_prune_by_rows_and_age(self):
        for i in range(10):
            bob_memory.transcript_append("r", "user", f"turn {i}", self.db)
        db = bob_memory.get_db(self.db)
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        db.execute("UPDATE transcript SET created_at=? WHERE seq=0", [old])
        db.conn.commit()
        self.assertEqual(bob_memory.prune_transcript(self.db, max_rows=0, max_days=90), 1)
        self.assertEqual(bob_memory.prune_transcript(self.db, max_rows=5, max_days=0), 4)
        seqs = [r[0] for r in db.execute("SELECT seq FROM transcript ORDER BY seq").fetchall()]
        self.assertEqual(seqs, [5, 6, 7, 8, 9])

    def test_forget_session_covers_the_transcript(self):
        bob_memory.transcript_append("r", "user", "secret plan", self.db, session_id="s1")
        bob_memory.transcript_append("r", "user", "other chat", self.db, session_id="s2")
        bob_memory.store("User drinks espresso", self.db, source_session="s1")
        with mock.patch("builtins.print"):
            bob_memory.cmd_forget(None, None, self.db, "local", session="s1")
        self.assertEqual([h["content"] for h in bob_memory.transcript_search("secret plan other chat", self.db)],
                         ["other chat"])
        self.assertEqual(bob_memory.list_memories(self.db), [])


@_DEPS
class TestCodeIndex(_Base):
    def setUp(self):
        super().setUp()
        self.embedded = []

        def rec(text):
            self.embedded.append(text)
            return [1.0, 0.0, float(len(self.embedded))]
        bob_memory.embed = rec
        self.repo_dir = self.dir / "repo"
        self.repo_dir.mkdir()
        (self.repo_dir / "a.py").write_text("def load(my path):\n    return my path\n", encoding="utf-8")
        self.code_db = self.dir / "code.db"

    def _repo(self):
        return bob_repomap.RepoMap([self.repo_dir], extractor=bob_repomap.extract_tags_regex)

    def test_code_is_stored_verbatim_with_its_context(self):
        bob_repomap.index_semantic(self._repo(), db_path=self.code_db)
        db = bob_memory.get_db(self.code_db)
        content, ctx = db.execute("SELECT content, embed_context FROM memories").fetchone()
        self.assertIn("my path", content)
        self.assertNotIn("the user's", content)
        self.assertTrue(ctx.startswith("File a.py"))

    def test_reembed_reproduces_the_contextual_input(self):
        bob_repomap.index_semantic(self._repo(), db_path=self.code_db)
        first = self.embedded[-1]
        db = bob_memory.get_db(self.code_db)
        db.execute("UPDATE memories SET embed_model='old.gguf'")
        db.conn.commit()
        self.assertEqual(bob_memory.rebuild_vectors(self.code_db), 1)
        self.assertEqual(self.embedded[-1], first)

    def test_rebuild_semantic_reembeds_every_chunk(self):
        repo = self._repo()
        bob_repomap.index_semantic(repo, db_path=self.code_db)
        n_before = len(self.embedded)
        bob_repomap.index_semantic(repo, db_path=self.code_db)   # exact dedup: no new embeds
        self.assertEqual(len(self.embedded), n_before)
        self.assertEqual(bob_repomap.rebuild_semantic(repo, db_path=self.code_db), 1)
        self.assertEqual(len(self.embedded), n_before + 1)
        db = bob_memory.get_db(self.code_db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 1)


class TestRecallToolDefault(unittest.TestCase):
    def test_default_k_is_recallK(self):
        import memory as memory_tool
        import bob_core
        seen = {}

        def fake_recall(query, k=5, config=None, owner=None, scope=None):
            seen["k"] = k
            return "(no results)"
        memory_tool.configure({"memory": {"enabled": True, "recallK": 9}})
        with mock.patch.object(bob_core, "memory_recall", fake_recall):
            memory_tool._memory_recall("anything")
        self.assertEqual(seen["k"], 9)


if __name__ == "__main__":
    unittest.main()
