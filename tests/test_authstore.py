"""O8 — auth store: DB-backed tokens with salted-hash storage (plaintext never persisted), hot
revocation, scopes, per-owner rate, and persistence across reopen. Pure sqlite + stdlib → runs under
bare python3 (no venv deps). A fixed salt is injected so hashes are deterministic in-test."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401 — puts scripts/ on sys.path
from bob_authstore import AuthStore

SALT = "test-salt-fixed"


class TestAuthStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-auth-"))
        self.db = self.dir / "sessions.db"
        self.store = AuthStore(self.db, salt=SALT)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_issue_returns_plaintext_and_resolves(self):
        tok = self.store.issue("alice", ["file_*"], rate_per_min=60)
        self.assertTrue(tok.startswith("bob_"))
        rec = self.store.lookup(tok)
        self.assertEqual(rec["owner"], "alice")
        self.assertEqual(rec["scopes"], ["file_*"])
        self.assertEqual(rec["rate_per_min"], 60)

    def test_plaintext_never_stored(self):
        tok = self.store.issue("alice")
        # inspect the raw table — the plaintext must not appear anywhere; only a sha256 hash.
        raw = sqlite3.connect(str(self.db))
        rows = raw.execute("SELECT token_hash, owner FROM auth_tokens").fetchall()
        raw.close()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0][0], tok)
        self.assertEqual(len(rows[0][0]), 64)          # sha256 hexdigest
        self.assertNotIn(tok, rows[0][0])

    def test_unknown_token_is_none(self):
        self.assertIsNone(self.store.lookup("bob_nope"))

    def test_hot_revoke(self):
        tok = self.store.issue("alice")
        self.assertIsNotNone(self.store.lookup(tok))
        self.assertTrue(self.store.revoke(tok))
        self.assertIsNone(self.store.lookup(tok))       # revoked -> None on the very next lookup
        self.assertFalse(self.store.revoke(tok))        # already revoked -> no-op

    def test_revoke_prefix(self):
        tok = self.store.issue("alice")
        prefix = self.store.list()[0]["hash_prefix"]
        self.assertEqual(self.store.revoke_prefix(prefix), 1)
        self.assertIsNone(self.store.lookup(tok))
        self.assertEqual(self.store.revoke_prefix("deadbeef"), 0)

    def test_list_omits_plaintext(self):
        tok = self.store.issue("bob", ["web_fetch"], 10)
        rows = self.store.list()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["owner"], "bob")
        self.assertEqual(r["scopes"], ["web_fetch"])
        self.assertEqual(r["rate_per_min"], 10)
        self.assertFalse(r["revoked"])
        self.assertNotIn("token", r)
        self.assertNotIn(tok, str(r))

    def test_salt_is_deterministic(self):
        tok = self.store.issue("alice")
        # a second store over the same DB + same salt resolves the same token (stable hashing).
        other = AuthStore(self.db, salt=SALT)
        try:
            self.assertEqual(other.lookup(tok)["owner"], "alice")
        finally:
            other.close()

    def test_wrong_salt_does_not_resolve(self):
        tok = self.store.issue("alice")
        other = AuthStore(self.db, salt="different-salt")
        try:
            self.assertIsNone(other.lookup(tok))
        finally:
            other.close()

    def test_persistence_across_reopen(self):
        tok = self.store.issue("carol", ["mcp:*"], 5)
        self.store.close()
        reopened = AuthStore(self.db, salt=SALT)
        try:
            rec = reopened.lookup(tok)
            self.assertEqual(rec["owner"], "carol")
            self.assertEqual(rec["scopes"], ["mcp:*"])
        finally:
            reopened.close()

    def test_generated_salt_persists_without_secret(self):
        # no injected salt + no osenv secret -> a per-install salt is generated and persisted, so a
        # reopened store still resolves the token (the salt lives in auth_meta, not a tracked file).
        db2 = self.dir / "gen.db"
        s1 = AuthStore(db2)
        tok = s1.issue("dave")
        s1.close()
        s2 = AuthStore(db2)
        try:
            self.assertEqual(s2.lookup(tok)["owner"], "dave")
        finally:
            s2.close()


if __name__ == "__main__":
    unittest.main()
