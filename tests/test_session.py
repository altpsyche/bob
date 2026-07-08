"""SessionStore CRUD + budget."""
import os
import tempfile
import unittest
from pathlib import Path

import _common
from bob_session import SessionStore


class TestSessionStore(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.gettempdir()) / f"bob-sess-{os.getpid()}.db"
        self.store = SessionStore(self.db)

    def tearDown(self):
        self.store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.db) + suffix).unlink()
            except OSError:
                pass

    def test_create_and_get(self):
        s = self.store.create(token_budget=100)
        got = self.store.get(s["id"])
        self.assertEqual(got["id"], s["id"])
        self.assertEqual(got["history"], [])
        self.assertEqual(got["token_budget"], 100)

    def test_get_unknown_is_none(self):
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_name_defaults_none_and_sets_owner_scoped(self):
        s = self.store.create(owner_id="local")
        self.assertIsNone(self.store.get(s["id"])["name"])
        self.assertTrue(self.store.set_name_owned(s["id"], "local", "my feature work"))
        self.assertEqual(self.store.get(s["id"])["name"], "my feature work")
        self.assertFalse(self.store.set_name_owned(s["id"], "someone-else", "hijack"))
        self.assertEqual(self.store.get(s["id"])["name"], "my feature work")   # non-owner can't rename

    def test_rename_does_not_bump_recency(self):
        s = self.store.create(owner_id="local")
        before = self.store.get(s["id"])["updated_at"]
        self.store.set_name_owned(s["id"], "local", "renamed")
        self.assertEqual(self.store.get(s["id"])["updated_at"], before)   # rename isn't activity

    def test_delete_all_owned_is_scoped(self):
        self.store.append_turn(self.store.create(owner_id="alice")["id"], "x", "y")
        self.store.create(owner_id="alice")
        keep = self.store.create(owner_id="bob")
        self.assertEqual(self.store.delete_all_owned("alice"), 2)
        self.assertEqual(self.store.list_owned("alice"), [])
        self.assertEqual(self.store.list_owned("bob"), [keep["id"]])      # other owner untouched

    def test_append_turn_and_spend(self):
        s = self.store.create()
        self.store.append_turn(s["id"], "hello", "hi there", tokens_used=40)
        got = self.store.get(s["id"])
        self.assertEqual([m["role"] for m in got["history"]], ["user", "assistant"])
        self.assertEqual(got["tokens_spent"], 40)

    def test_budget_enforcement(self):
        s = self.store.create(token_budget=100)
        self.store.append_turn(s["id"], "a", "b", tokens_used=40)
        self.assertFalse(self.store.over_budget(s["id"]))
        self.store.append_turn(s["id"], "c", "d", tokens_used=70)
        self.assertTrue(self.store.over_budget(s["id"]))

    def test_zero_budget_never_over(self):
        s = self.store.create(token_budget=0)
        self.store.append_turn(s["id"], "a", "b", tokens_used=10_000)
        self.assertFalse(self.store.over_budget(s["id"]))

    def test_delete(self):
        s = self.store.create()
        self.assertTrue(self.store.delete(s["id"]))
        self.assertIsNone(self.store.get(s["id"]))

    # --- ownership ------------------------------------------------------
    def test_create_stamps_owner(self):
        s = self.store.create(owner_id="alice")
        self.assertEqual(self.store.get(s["id"])["owner_id"], "alice")

    def test_create_defaults_owner(self):
        store = SessionStore(self.dir_default() / "d.db", default_owner="local")
        try:
            s = store.create()
            self.assertEqual(store.get(s["id"])["owner_id"], "local")
        finally:
            store.close()

    def test_get_owned_wrong_owner_is_none(self):
        s = self.store.create(owner_id="alice")
        self.assertIsNone(self.store.get_owned(s["id"], "bob"))
        self.assertIsNotNone(self.store.get_owned(s["id"], "alice"))

    def test_delete_owned_scopes_to_owner(self):
        s = self.store.create(owner_id="alice")
        self.assertFalse(self.store.delete_owned(s["id"], "bob"))
        self.assertIsNotNone(self.store.get(s["id"]))
        self.assertTrue(self.store.delete_owned(s["id"], "alice"))

    def test_list_owned_isolates_owners(self):
        a1 = self.store.create(owner_id="alice")
        a2 = self.store.create(owner_id="alice")
        b1 = self.store.create(owner_id="bob")
        self.assertEqual(set(self.store.list_owned("alice")), {a1["id"], a2["id"]})
        self.assertEqual(self.store.list_owned("bob"), [b1["id"]])

    def test_list_owned_unknown_owner_is_empty(self):
        self.store.create(owner_id="alice")
        self.assertEqual(self.store.list_owned("nobody"), [])

    def test_list_owned_orders_newest_first(self):
        older = self.store.create(owner_id="alice")
        newer = self.store.create(owner_id="alice")
        # Re-stamp `older` (append_turn calls _now() strictly later) so it becomes the newest.
        self.store.append_turn(older["id"], "bump", None)
        self.assertEqual(self.store.list_owned("alice"), [older["id"], newer["id"]])

    def dir_default(self):
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="bob-sess-def-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d

    def test_migration_backfills_owner_from_old_schema(self):
        import sqlite3
        old = self.dir_default() / "old.db"
        # Build a legacy DB (no owner_id column), with one row carrying a legacy `client`.
        conn = sqlite3.connect(str(old))
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, history TEXT NOT NULL DEFAULT '[]', "
            "token_budget INTEGER NOT NULL DEFAULT 0, tokens_spent INTEGER NOT NULL DEFAULT 0, client TEXT)"
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, history, client) VALUES (?,?,?,?,?)",
            ["legacy1", "t", "t", "[]", "carol"],
        )
        conn.execute(
            "INSERT INTO sessions (id, created_at, updated_at, history, client) VALUES (?,?,?,?,?)",
            ["legacy2", "t", "t", "[]", None],
        )
        conn.commit()
        conn.close()

        store = SessionStore(old, default_owner="local")
        try:
            self.assertEqual(store.get("legacy1")["owner_id"], "carol")   # from client
            self.assertEqual(store.get("legacy2")["owner_id"], "local")   # backfilled default
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
