"""M13 — bob_memory importable core (M14-blocker) with the embed server mocked."""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _common
import bob_memory


def _fake_embed(text: str):
    # Deterministic: identical text -> identical vector (so dedup fires); different text differs.
    return [float(len(text)), float(sum(ord(c) for c in text) % 97), 1.0]


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestMemoryCore(unittest.TestCase):
    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        # Unique DB per test — sqlite keeps the file open, so isolate rather than delete-between.
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_store_returns_id_and_is_new(self):
        mid, is_new = bob_memory.store("first fact", self.db)
        self.assertIsInstance(mid, int)
        self.assertTrue(is_new)

    def test_store_dedups_identical(self):
        mid, _ = bob_memory.store("same text", self.db)
        mid2, is_new = bob_memory.store("same text", self.db)
        self.assertFalse(is_new)
        self.assertEqual(mid, mid2)

    def test_recall_finds_match(self):
        bob_memory.store("the user likes powershell", self.db)
        hits = bob_memory.recall("the user likes powershell", self.db, k=3, threshold=0.3)
        self.assertTrue(hits)
        self.assertIn("powershell", hits[0]["content"])

    def test_recall_empty_query(self):
        self.assertEqual(bob_memory.recall("", self.db), [])

    def test_store_accepts_str_path(self):
        # bob_core._get_db_path passes a str; get_db must coerce (regression for the masked bug).
        mid, _ = bob_memory.store("str path fact", str(self.db))
        self.assertIsInstance(mid, int)

    def test_embed_failure_raises_runtimeerror(self):
        def boom(_):
            raise RuntimeError("embed down")
        bob_memory.embed = boom
        with self.assertRaises(RuntimeError):
            bob_memory.store("x", self.db)

    def test_litellm_does_not_cache_fallback_when_config_absent(self):
        # N-review H2: if config.json isn't readable on first call, use the fallback but DON'T
        # memoize it — a later call must pick up the real config instead of being poisoned.
        import bob_core
        bob_memory._LITELLM.clear()
        orig = bob_core.load_config
        try:
            def _missing():
                raise FileNotFoundError("no config yet")
            bob_core.load_config = _missing
            base1, _ = bob_memory._litellm()
            self.assertIn(":8081", base1)                    # central default fallback
            self.assertNotIn("v", bob_memory._LITELLM)       # NOT cached

            bob_core.load_config = lambda: {"litellmPort": 9099, "litellmKey": "sk-real"}
            base2, h2 = bob_memory._litellm()
            self.assertIn(":9099", base2)                    # re-read, not stuck on fallback
            self.assertEqual(h2["Authorization"], "Bearer sk-real")
        finally:
            bob_core.load_config = orig
            bob_memory._LITELLM.clear()


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestTypedWrite(unittest.TestCase):
    """MEM-1 — typed, owner-scoped write path: normalize, content_hash exact-dedup, near-dedup."""

    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem1-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _row(self, mid):
        db = bob_memory.get_db(self.db)
        return db.execute(
            "SELECT content, type, owner_id, scope, tags, salience, content_hash, subject "
            "FROM memories WHERE id=?", [mid]).fetchone()

    def test_content_normalized_on_write(self):
        mid, _ = bob_memory.store("I prefer dark mode", self.db)
        self.assertEqual(self._row(mid)[0], "User prefer dark mode")   # stored third-person

    def test_typed_fields_persisted(self):
        mid, _ = bob_memory.store("owns a repo", self.db, mem_type="project", owner="alice",
                                  scope="/repo", tags="x,y", salience=0.5)
        content, mtype, owner, scope, tags, salience, chash, subject = self._row(mid)
        self.assertEqual(mtype, "project")
        self.assertEqual(owner, "alice")
        self.assertEqual(scope, "/repo")
        self.assertEqual(tags, "x,y")
        self.assertEqual(salience, 0.5)
        self.assertEqual(subject, "user")
        self.assertIsNotNone(chash)

    def test_exact_dedup_short_circuits_before_embed(self):
        calls = {"n": 0}
        def counting(text):
            calls["n"] += 1
            return _fake_embed(text)
        bob_memory.embed = counting
        mid1, new1 = bob_memory.store("hello world", self.db)   # embeds once
        mid2, new2 = bob_memory.store("hello world", self.db)   # exact hash hit -> no embed
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertEqual(mid1, mid2)
        self.assertEqual(calls["n"], 1)                         # second store never embedded

    def test_exact_dedup_is_owner_scoped(self):
        _, a = bob_memory.store("shared note", self.db, owner="alice")
        _, b = bob_memory.store("shared note", self.db, owner="bob")
        self.assertTrue(a)
        self.assertTrue(b)                                      # same content, different owner -> both stored

    def test_near_dedup_respects_type_scope(self):
        # "ab" and "ba" embed identically under _fake_embed (same len + char-sum) but hash differently,
        # so this exercises the NEAR path, not the exact path.
        self.assertEqual(_fake_embed("ab"), _fake_embed("ba"))
        bob_memory.store("ab", self.db, mem_type="fact")
        _, same_type = bob_memory.store("ba", self.db, mem_type="fact")
        self.assertFalse(same_type)                             # near-dup within (owner, fact)
        _, other_type = bob_memory.store("ba", self.db, mem_type="preference")
        self.assertTrue(other_type)                            # different type -> not deduped

    def test_bob_core_threads_tags_type_owner_and_dedup(self):
        # The old bob_core.memory_store dropped `tags`; assert the full arg set now reaches store().
        import bob_core
        captured = {}
        def fake_store(content, db_path, **kw):
            captured.update(kw)
            return 7, True
        orig = bob_memory.store
        bob_memory.store = fake_store
        try:
            cfg = {"memory": {"dbPath": "data/bob.db", "dedupThreshold": 0.88},
                   "agent": {"defaultOwner": "zoe"}}
            out = bob_core.memory_store("a note", tags="t1,t2", mem_type="preference", config=cfg)
            self.assertIn("Stored", out)
            self.assertEqual(captured["tags"], "t1,t2")         # bug fixed — tags no longer dropped
            self.assertEqual(captured["mem_type"], "preference")
            self.assertEqual(captured["owner"], "zoe")
            self.assertEqual(captured["dedup_threshold"], 0.88)
        finally:
            bob_memory.store = orig


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestBlendedRead(unittest.TestCase):
    """MEM-2 — owner/scope prefilter + blended (semantic·recency·type·usage) ranking."""

    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem2-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _insert(self, content, emb_key=None, **cols):
        """Insert a row directly (bypassing store()'s dedup) so ranking inputs are controllable.
        emb_key lets several rows share one embedding (equal cosine) while differing in content."""
        db = bob_memory.get_db(self.db)
        row = {
            "content": content,
            "content_hash": bob_memory._content_hash(content),
            "embedding": json.dumps(_fake_embed(emb_key if emb_key is not None else content)),
            "type": "fact", "subject": "user", "owner_id": "local", "salience": 1.0,
        }
        row.update(cols)
        db["memories"].insert(row)
        db.conn.commit()

    def test_owner_isolation(self):
        self._insert("alice secret", owner_id="alice")
        self.assertEqual(bob_memory.recall("alice secret", self.db, owner="bob", threshold=0.0), [])
        hits = bob_memory.recall("alice secret", self.db, owner="alice", threshold=0.0)
        self.assertTrue(hits and hits[0]["content"] == "alice secret")

    def test_newer_outranks_older_at_equal_cosine(self):
        self._insert("older", emb_key="Q", created_at="2019-01-01T00:00:00+00:00")
        self._insert("newer", emb_key="Q", created_at=datetime.now(timezone.utc).isoformat())
        hits = bob_memory.recall("Q", self.db, k=2, threshold=0.0)
        self.assertEqual([h["content"] for h in hits], ["newer", "older"])

    def test_below_threshold_filtered(self):
        self._insert("something")
        # Max blended score is ~1.5; a threshold above it filters everything.
        self.assertEqual(bob_memory.recall("something", self.db, threshold=10.0), [])

    def test_type_weight_tie_break(self):
        now = datetime.now(timezone.utc).isoformat()
        self._insert("prof", emb_key="Q", type="profile", created_at=now)
        self._insert("epi", emb_key="Q", type="episodic", created_at=now)
        hits = bob_memory.recall("Q", self.db, k=2, threshold=0.0)
        self.assertEqual(hits[0]["content"], "prof")   # higher type weight wins at equal cosine/recency

    def test_expired_rows_excluded(self):
        self._insert("gone", expires_at="2019-01-01T00:00:00+00:00")
        self._insert("here", emb_key="gone")           # same embedding, not expired
        contents = [h["content"] for h in bob_memory.recall("gone", self.db, threshold=0.0)]
        self.assertIn("here", contents)
        self.assertNotIn("gone", contents)

    def test_superseded_rows_excluded(self):
        self._insert("old version", superseded_by=999)
        self._insert("new version", emb_key="old version")
        contents = [h["content"] for h in bob_memory.recall("old version", self.db, threshold=0.0)]
        self.assertNotIn("old version", contents)
        self.assertIn("new version", contents)

    def test_scope_filter_includes_global_and_matching_only(self):
        self._insert("global fact", scope=None)
        self._insert("repo A fact", emb_key="global fact", scope="/repoA")
        self._insert("repo B fact", emb_key="global fact", scope="/repoB")
        contents = [h["content"]
                    for h in bob_memory.recall("global fact", self.db, k=5, threshold=0.0, scope="/repoA")]
        self.assertIn("global fact", contents)         # NULL scope always in
        self.assertIn("repo A fact", contents)
        self.assertNotIn("repo B fact", contents)      # other project's rows excluded

    def test_bob_core_threads_owner_scope_and_weights(self):
        import bob_core
        captured = {}
        def fake_recall(query, db_path, **kw):
            captured.update(kw)
            return [{"id": 1, "content": "c", "score": 1.0}]
        orig = bob_memory.recall
        bob_memory.recall = fake_recall
        try:
            cfg = {"memory": {"dbPath": "data/bob.db", "recallThreshold": 0.4,
                              "typeWeights": {"fact": 0.7},
                              "ranking": {"wSemantic": 2.0, "halfLifeDays": {"fact": 10}}},
                   "agent": {"defaultOwner": "zoe"}}
            bob_core.memory_recall("q", config=cfg, scope="/r")
            self.assertEqual(captured["owner"], "zoe")
            self.assertEqual(captured["threshold"], 0.4)
            self.assertEqual(captured["scope"], "/r")
            self.assertEqual(captured["half_lives"], {"fact": 10})
        finally:
            bob_memory.recall = orig


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestProfileBlock(unittest.TestCase):
    """MEM-3 — profile_block selection/order/cap + bob_core gating and framing."""

    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem3-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _insert(self, content, **cols):
        db = bob_memory.get_db(self.db)
        row = {"content": content, "content_hash": bob_memory._content_hash(content),
               "embedding": "[0.0]", "type": "preference", "subject": "user",
               "owner_id": "local", "salience": 1.0, "pinned": 0}
        row.update(cols)
        db["memories"].insert(row)
        db.conn.commit()

    def test_selects_only_profile_and_preference_ordered(self):
        self._insert("User works on games", type="profile", salience=1.0)
        self._insert("User prefers dark mode", type="preference", salience=0.9)
        self._insert("User visited a repo", type="project")     # excluded
        self._insert("session recap", type="episodic")          # excluded
        self._insert("User pinned fact", type="preference", salience=0.1, pinned=1)
        body = bob_memory.profile_block("local", self.db, limit=5)
        self.assertIn("User works on games", body)
        self.assertIn("User prefers dark mode", body)
        self.assertNotIn("visited a repo", body)
        self.assertNotIn("session recap", body)
        self.assertTrue(body.startswith("- User pinned fact"))  # pinned sorts first

    def test_none_when_empty(self):
        self.assertIsNone(bob_memory.profile_block("local", self.db))

    def test_owner_scoped(self):
        self._insert("alice fact", owner_id="alice", type="profile")
        self.assertIsNone(bob_memory.profile_block("local", self.db))     # different owner
        self.assertIsNotNone(bob_memory.profile_block("alice", self.db))

    def test_respects_char_cap(self):
        for i in range(5):
            self._insert(f"User fact {i} " + "x" * 50, type="preference")
        body = bob_memory.profile_block("local", self.db, limit=5, max_chars=80)
        self.assertEqual(len(body.splitlines()), 1)              # only the first line fits under 80

    def test_bob_core_gating_and_frame(self):
        import bob_core
        orig = bob_memory.profile_block
        bob_memory.profile_block = lambda owner, db_path, max_chars=800: "- User works on games"
        try:
            on = {"memory": {"enabled": True, "injectProfileAtStart": True, "dbPath": "data/bob.db"},
                  "agent": {"defaultOwner": "local"}}
            out = bob_core.memory_profile_block(config=on)
            self.assertTrue(out.startswith(bob_core.MEMORY_CONTEXT_FRAME))
            self.assertIn("- User works on games", out)
            # gated off two ways
            self.assertIsNone(bob_core.memory_profile_block(
                config={"memory": {"enabled": True, "injectProfileAtStart": False}}))
            self.assertIsNone(bob_core.memory_profile_block(
                config={"memory": {"enabled": False, "injectProfileAtStart": True}}))
        finally:
            bob_memory.profile_block = orig


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestConsolidation(unittest.TestCase):
    """MEM-4 — in-process consolidation: typed fact extraction (LLM mocked), dedup, episodic recap."""

    def setUp(self):
        self._orig_embed = bob_memory.embed
        self._orig_sum = bob_memory.summarize_turns
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem4-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig_embed
        bob_memory.summarize_turns = self._orig_sum
        shutil.rmtree(self.dir, ignore_errors=True)

    _TURNS = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]

    def test_parse_typed_bullets(self):
        pairs = bob_memory._parse_typed_bullets(
            "- preference: User likes tea\nrandom line with no type\nproject: builds Bob\n")
        self.assertIn(("preference", "User likes tea"), pairs)
        self.assertIn(("project", "builds Bob"), pairs)
        self.assertIn(("fact", "random line with no type"), pairs)

    def test_extracts_typed_facts_and_episodic_recap(self):
        bob_memory.summarize_turns = lambda *a, **k: ("preference: User prefers dark mode\n"
                                                      "project: User is building Bob\n")
        result = bob_memory.consolidate_session(self._TURNS, self.db)
        self.assertEqual(result["facts"], 2)
        db = bob_memory.get_db(self.db)
        by_content = {r[0]: r[1] for r in db.execute("SELECT content, type FROM memories")}
        self.assertEqual(by_content.get("User prefers dark mode"), "preference")
        self.assertEqual(by_content.get("User is building Bob"), "project")
        types = [r[0] for r in db.execute("SELECT type FROM memories")]
        self.assertIn("episodic", types)                       # raw recap preserved

    def test_rerun_is_idempotent(self):
        bob_memory.summarize_turns = lambda *a, **k: "preference: User prefers dark mode\n"
        self.assertEqual(bob_memory.consolidate_session(self._TURNS, self.db)["facts"], 1)
        self.assertEqual(bob_memory.consolidate_session(self._TURNS, self.db)["facts"], 0)  # dedup

    def test_short_session_is_noop(self):
        r = bob_memory.consolidate_session([{"role": "user", "content": "hi"}], self.db)
        self.assertEqual(r["facts"], 0)
        self.assertIsNone(r["summary"])

    def test_empty_extraction_is_noop(self):
        bob_memory.summarize_turns = lambda *a, **k: ""
        r = bob_memory.consolidate_session(self._TURNS, self.db)
        self.assertEqual(r["facts"], 0)
        self.assertEqual(bob_memory.get_db(self.db).execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)

    def test_forwards_timeout_to_summarizer(self):
        captured = {}
        bob_memory.summarize_turns = (lambda turns, model="chat", system_prompt=None,
                                      max_tokens=256, timeout=60: captured.update(timeout=timeout) or "")
        bob_memory.consolidate_session(self._TURNS, self.db, timeout=17)
        self.assertEqual(captured["timeout"], 17)          # bounded exit stall reaches the LLM call


class TestOwnerThreading(unittest.TestCase):
    """MEM-6 — RunContext carries owner/agent_depth; the memory tools scope to the run's owner."""

    def test_run_context_owner_depth_scope(self):
        from bob_loop import RunContext
        ctx = RunContext(cancel=None, config={}, registry=None, run_id="x", approve=None,
                         owner="zoe", agent_depth=2, scope="/repo")
        self.assertEqual(ctx.owner, "zoe")
        self.assertEqual(ctx.agent_depth, 2)
        self.assertEqual(ctx.scope, "/repo")
        default = RunContext(cancel=None, config={}, registry=None, run_id="x", approve=None)
        self.assertEqual(default.owner, "local")           # root defaults
        self.assertEqual(default.agent_depth, 0)
        self.assertIsNone(default.scope)

    def test_memory_tools_scope_to_run_owner_and_project(self):
        import bob_core
        import tool_registry
        from bob_loop import RunContext
        import importlib
        memtool = importlib.import_module("memory")        # scripts/tools/memory.py
        captured = {}
        orig_store = bob_core.memory_store
        orig_recall = bob_core.memory_recall
        bob_core.memory_store = (lambda content, tags="", mem_type="fact", owner=None, scope=None,
                                 config=None: captured.update(store_owner=owner, store_type=mem_type,
                                                              store_scope=scope) or "ok")
        bob_core.memory_recall = (lambda query, k=5, config=None, owner=None, scope=None:
                                  captured.update(recall_owner=owner, recall_scope=scope) or "note")
        tok = tool_registry._RUN_CONTEXT.set(
            RunContext(cancel=None, config={}, registry=None, run_id="r", approve=None,
                       owner="alice", scope="/repoA"))
        try:
            memtool._memory_store("a note", type="preference")
            memtool._memory_recall("q")
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)
            bob_core.memory_store = orig_store
            bob_core.memory_recall = orig_recall
        self.assertEqual(captured["store_owner"], "alice")   # tool scoped store to the run's owner
        self.assertEqual(captured["store_type"], "preference")
        self.assertEqual(captured["store_scope"], "/repoA")  # and the run's project scope
        self.assertEqual(captured["recall_owner"], "alice")
        self.assertEqual(captured["recall_scope"], "/repoA")


class TestProjectScoping(unittest.TestCase):
    """MEM-7a — project_key detection + bob_core scoping gate (project-type only)."""

    def test_project_key_finds_git_root(self):
        import bob_core
        d = Path(tempfile.mkdtemp(prefix="bob-proj-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / ".git").mkdir()
        sub = d / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(bob_core.project_key(str(sub), {"memory": {"scopeByProject": True}}),
                         str(d.resolve()))

    def test_project_key_none_when_disabled(self):
        import bob_core
        self.assertIsNone(bob_core.project_key(".", {"memory": {"scopeByProject": False}}))

    def test_project_key_falls_back_to_dir_without_git(self):
        import bob_core
        d = Path(tempfile.mkdtemp(prefix="bob-proj2-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        self.assertEqual(bob_core.project_key(str(d), {"memory": {"scopeByProject": True}}),
                         str(d.resolve()))

    def test_memory_store_scopes_only_project_type(self):
        import bob_core
        import bob_memory
        captured = {}
        orig = bob_memory.store
        bob_memory.store = lambda content, db_path, **kw: captured.update(kw) or (1, True)
        try:
            cfg = {"memory": {"dbPath": "data/bob.db"}, "agent": {"defaultOwner": "local"}}
            bob_core.memory_store("x", mem_type="project", scope="/repoA", config=cfg)
            self.assertEqual(captured["scope"], "/repoA")     # project facts scoped
            bob_core.memory_store("y", mem_type="preference", scope="/repoA", config=cfg)
            self.assertIsNone(captured["scope"])              # identity/prefs stay global
        finally:
            bob_memory.store = orig

    def test_project_memory_block_reads_and_frames(self):
        import bob_core
        orig = bob_core._project_memory_files
        self.addCleanup(lambda: setattr(bob_core, "_project_memory_files", orig))
        d = Path(tempfile.mkdtemp(prefix="bob-bobmd-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "BOB.md").write_text("Use pnpm, not npm.", encoding="utf-8")
        bob_core._project_memory_files = lambda pd: [Path(pd) / "BOB.md"]
        out = bob_core.project_memory_block(str(d), {"memory": {"projectFiles": True}})
        self.assertTrue(out.startswith("Project instructions"))
        self.assertIn("Use pnpm, not npm.", out)

    def test_project_memory_block_gated_and_empty(self):
        import bob_core
        orig = bob_core._project_memory_files
        self.addCleanup(lambda: setattr(bob_core, "_project_memory_files", orig))
        bob_core._project_memory_files = lambda pd: [Path(pd) / "BOB.md"]
        d = Path(tempfile.mkdtemp(prefix="bob-bobmd2-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "BOB.md").write_text("x", encoding="utf-8")
        self.assertIsNone(bob_core.project_memory_block(str(d), {"memory": {"projectFiles": False}}))
        self.assertIsNone(bob_core.project_memory_block(None, {"memory": {"projectFiles": True}}))
        empty = Path(tempfile.mkdtemp(prefix="bob-bobmd3-"))
        self.addCleanup(lambda: shutil.rmtree(empty, ignore_errors=True))
        self.assertIsNone(bob_core.project_memory_block(str(empty), {"memory": {"projectFiles": True}}))

    def test_project_memory_block_caps_length(self):
        import bob_core
        orig = bob_core._project_memory_files
        self.addCleanup(lambda: setattr(bob_core, "_project_memory_files", orig))
        d = Path(tempfile.mkdtemp(prefix="bob-bobmd4-"))
        self.addCleanup(lambda: shutil.rmtree(d, ignore_errors=True))
        (d / "BOB.md").write_text("x" * 1000, encoding="utf-8")
        bob_core._project_memory_files = lambda pd: [Path(pd) / "BOB.md"]
        out = bob_core.project_memory_block(str(d), {"memory": {"projectFiles": True, "bobMdMaxTokens": 10}})
        prefix = "Project instructions (from BOB.md — follow these for this project):\n"
        self.assertLessEqual(len(out) - len(prefix), 40)     # body capped at bobMdMaxTokens*4


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestHygiene(unittest.TestCase):
    """MEM-5 — TTL prune, size cap, soft-delete (forget), soft-update (edit), list/export."""

    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-mem5-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    def _insert(self, content, **cols):
        db = bob_memory.get_db(self.db)
        row = {"content": content, "content_hash": bob_memory._content_hash(content),
               "embedding": "[0.0]", "type": "fact", "subject": "user", "owner_id": "local",
               "salience": 1.0, "pinned": 0}
        row.update(cols)
        db["memories"].insert(row)
        db.conn.commit()

    def _contents(self):
        return {r[0] for r in bob_memory.get_db(self.db).execute("SELECT content FROM memories")}

    def test_ttl_prune_keeps_pinned_and_identity(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        now = datetime.now(timezone.utc).isoformat()
        self._insert("old recap", type="episodic", created_at=old)
        self._insert("pinned recap", type="episodic", created_at=old, pinned=1)
        self._insert("old identity", type="profile", created_at=old)
        self._insert("fresh recap", type="episodic", created_at=now)
        r = bob_memory.prune(self.db, forget_after_days={"episodic": 180}, max_rows=10000)
        self.assertEqual(r["ttl_pruned"], 1)
        remaining = self._contents()
        self.assertNotIn("old recap", remaining)      # past TTL -> pruned
        self.assertIn("pinned recap", remaining)       # pinned exempt
        self.assertIn("old identity", remaining)        # profile exempt
        self.assertIn("fresh recap", remaining)         # within TTL

    def test_size_cap_drops_lowest_salience_keeps_pinned(self):
        self._insert("keep pinned", salience=0.1, pinned=1)
        for i, s in enumerate([0.2, 0.3, 0.4, 0.5, 0.6]):
            self._insert(f"row {i}", salience=s)
        r = bob_memory.prune(self.db, max_rows=3)           # 6 rows -> drop 3
        self.assertEqual(r["capped"], 3)
        remaining = self._contents()
        self.assertIn("keep pinned", remaining)             # pinned never dropped
        self.assertNotIn("row 0", remaining)                # lowest salience dropped first
        self.assertIn("row 4", remaining)                   # highest salience kept

    def test_forget_soft_deletes(self):
        mid, _ = bob_memory.store("the user likes tea", self.db)
        self.assertTrue(bob_memory.forget(mid, self.db))
        self.assertEqual(bob_memory.recall("the user likes tea", self.db, threshold=0.0), [])  # hidden
        self.assertIsNotNone(bob_memory.get_memory(mid, self.db))                               # still in DB

    def test_edit_supersedes_and_reembeds(self):
        mid, _ = bob_memory.store("the user likes tea", self.db)
        new_id = bob_memory.edit(mid, "the user likes coffee", self.db)
        self.assertIsNotNone(new_id)
        self.assertNotEqual(new_id, mid)
        self.assertEqual(bob_memory.get_memory(mid, self.db)["superseded_by"], new_id)
        hits = [h["content"] for h in bob_memory.recall("the user likes coffee", self.db, threshold=0.0)]
        self.assertIn("the user likes coffee", hits)
        self.assertNotIn("the user likes tea", hits)        # superseded -> filtered from recall

    def test_list_filters_by_type_and_export(self):
        bob_memory.store("a fact here", self.db, mem_type="fact")
        bob_memory.store("a pref here", self.db, mem_type="preference")
        self.assertEqual([r["content"] for r in bob_memory.list_memories(self.db, type_filter="fact")],
                         ["a fact here"])
        self.assertEqual(len(bob_memory.export_memories(self.db)), 2)

    def test_init_profile_writes_profile_rows_not_dead_table(self):
        # (The two facts can collapse under the length-dominated _fake_embed near-dedup — real BGE-M3
        # keeps them distinct, per the live smoke. Here we assert the REDIRECT, not both survive.)
        bob_memory.cmd_init_profile("Siva", "game dev and AI tooling", self.db)
        profiles = bob_memory.list_memories(self.db, type_filter="profile")
        self.assertTrue(profiles)                                        # wrote type=profile memory rows
        self.assertTrue(any("Siva" in r["content"] for r in profiles))
        db = bob_memory.get_db(self.db)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM profile").fetchone()[0], 0)  # dead table unused


class TestNormalize(unittest.TestCase):
    """§2.3 deterministic third-person normalization + content hash — pure, no deps."""

    def test_leading_pronoun_rules(self):
        n = bob_memory._normalize_third_person
        self.assertEqual(n("I prefer dark mode in all editors"),
                         "User prefer dark mode in all editors")
        self.assertEqual(n("My preferred shell is PowerShell 7"),
                         "User's preferred shell is PowerShell 7")
        self.assertEqual(n("I'm a game developer"), "User is a game developer")
        self.assertEqual(n("I've shipped it"), "User has shipped it")

    def test_standalone_my_midsentence(self):
        self.assertEqual(
            bob_memory._normalize_third_person("I use Claude Code as my primary coding assistant"),
            "User use Claude Code as the user's primary coding assistant",
        )

    def test_unmatched_passes_through(self):
        self.assertEqual(bob_memory._normalize_third_person("The sky is blue"), "The sky is blue")

    def test_content_hash_is_stable_and_distinct(self):
        self.assertEqual(bob_memory._content_hash("abc"), bob_memory._content_hash("abc"))
        self.assertNotEqual(bob_memory._content_hash("abc"), bob_memory._content_hash("abd"))


@unittest.skipUnless(bob_memory._DEPS_ERROR is None,
                     f"memory deps (sqlite-utils/requests) not installed: {bob_memory._DEPS_ERROR}")
class TestSchemaV2(unittest.TestCase):
    """MEM-0 — PRAGMA user_version migration in get_db + migrate --normalize."""

    def setUp(self):
        self._orig = bob_memory.embed
        bob_memory.embed = _fake_embed
        self.dir = Path(tempfile.mkdtemp(prefix="bob-memv2-"))
        self.db = self.dir / "m.db"

    def tearDown(self):
        bob_memory.embed = self._orig
        shutil.rmtree(self.dir, ignore_errors=True)

    _V2_COLS = ("content_hash", "type", "subject", "owner_id", "scope", "tags",
                "salience", "pinned", "superseded_by", "updated_at", "expires_at")

    def test_fresh_db_is_version_2_with_all_columns(self):
        db = bob_memory.get_db(self.db)
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        cols = {r[1] for r in db.execute("PRAGMA table_info(memories)").fetchall()}
        for c in self._V2_COLS:
            self.assertIn(c, cols)

    def _seed_v1(self):
        """Build a pre-MEM-0 (v1) DB: the old column set, user_version=0, the 4 legacy rows."""
        import sqlite3
        conn = sqlite3.connect(str(self.db))
        conn.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL, "
            "embedding TEXT NOT NULL, source TEXT DEFAULT 'user', created_at TEXT, "
            "last_used TEXT, use_count INTEGER DEFAULT 0)"
        )
        for content, source in [
            ("I prefer dark mode in all editors", "user"),
            ("I work on game dev and AI tooling", "user"),
            ("My preferred shell is PowerShell 7", "user"),
            ("recap of the last session", "session"),
        ]:
            conn.execute("INSERT INTO memories (content, embedding, source) VALUES (?,?,?)",
                         [content, "[0.0]", source])
        conn.commit()
        conn.close()

    def test_legacy_v1_migrates_and_backfills_type(self):
        self._seed_v1()
        db = bob_memory.get_db(self.db)                       # triggers migration
        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        type_by_content = {r[0]: r[1]
                           for r in db.execute("SELECT content, type FROM memories").fetchall()}
        self.assertEqual(type_by_content["I prefer dark mode in all editors"], "preference")  # source=user
        self.assertEqual(type_by_content["recap of the last session"], "episodic")            # source=session
        self.assertEqual({r[0] for r in db.execute("SELECT DISTINCT owner_id FROM memories")}, {"local"})
        self.assertEqual({r[0] for r in db.execute("SELECT DISTINCT subject FROM memories")}, {"user"})

    def test_migration_is_idempotent(self):
        self._seed_v1()
        bob_memory.get_db(self.db)
        db2 = bob_memory.get_db(self.db)                      # second open must not error or change data
        self.assertEqual(db2.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(db2.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 4)

    def test_migrate_normalize_rewrites_first_person_and_reembeds(self):
        db = bob_memory.get_db(self.db)
        db["memories"].insert({"content": "I prefer dark mode in all editors",
                               "embedding": "[0.0]", "source": "user"})
        db["memories"].insert({"content": "The sky is blue", "embedding": "[0.0]", "source": "user"})
        db.conn.commit()

        bob_memory.cmd_migrate(self.db, normalize=True)

        db2 = bob_memory.get_db(self.db)
        by_content = {r[0]: {"hash": r[1], "emb": r[2]}
                      for r in db2.execute("SELECT content, content_hash, embedding FROM memories")}
        # First-person row rewritten, hashed, re-embedded (embedding no longer the seeded "[0.0]").
        self.assertIn("User prefer dark mode in all editors", by_content)
        self.assertIsNotNone(by_content["User prefer dark mode in all editors"]["hash"])
        self.assertNotEqual(by_content["User prefer dark mode in all editors"]["emb"], "[0.0]")
        # Unmatched row left exactly as-is (no rewrite, no re-embed, no hash).
        self.assertIn("The sky is blue", by_content)
        self.assertIsNone(by_content["The sky is blue"]["hash"])
        self.assertEqual(by_content["The sky is blue"]["emb"], "[0.0]")


if __name__ == "__main__":
    unittest.main()
