"""SessionStore under concurrent threadpool access: 0 errors, no lost turns.

Hammers create/append/read from N threads. The `len(history) == N*PER*2` assertion is the
direct lost-update detector — it fails against an unlocked store (read-modify-write outside the
lock) and passes once append_turn is a single BEGIN IMMEDIATE transaction.
"""
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import _common  # noqa: F401  (puts scripts/ on sys.path)
from bob_session import SessionStore


class TestSessionConcurrency(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-sess-conc-"))
        self.store = SessionStore(self.dir / "c.db")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_concurrent_appends_no_lost_turns(self):
        s = self.store.create()
        N, PER = 8, 25
        errors = []

        def worker(i):
            for j in range(PER):
                try:
                    self.store.append_turn(s["id"], f"u{i}-{j}", f"a{i}-{j}", tokens_used=1)
                except Exception as e:  # noqa: BLE001 — collect, assert none
                    errors.append(repr(e))

        with ThreadPoolExecutor(max_workers=N) as ex:
            list(ex.map(worker, range(N)))

        self.assertEqual(errors, [], f"append errors: {errors[:3]}")
        got = self.store.get(s["id"])
        self.assertEqual(len(got["history"]), N * PER * 2, "lost turns under concurrency")
        self.assertEqual(got["tokens_spent"], N * PER, "lost token tally under concurrency")

    def test_concurrent_create_and_read(self):
        N = 40
        errors = []
        ids = []

        def worker(i):
            try:
                sid = self.store.create(token_budget=i)["id"]
                ids.append(sid)
                assert self.store.get(sid) is not None
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(worker, range(N)))

        self.assertEqual(errors, [], f"create/read errors: {errors[:3]}")
        self.assertEqual(len(set(ids)), N, "duplicate or missing session ids")
        self.assertEqual(len(self.store.list_ids()), N)

    def test_mixed_create_append_read(self):
        errors = []

        def worker(i):
            try:
                sid = self.store.create()["id"]
                for j in range(5):
                    self.store.append_turn(sid, f"u{j}", f"a{j}", tokens_used=1)
                got = self.store.get(sid)
                assert len(got["history"]) == 10
                assert got["tokens_spent"] == 5
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        with ThreadPoolExecutor(max_workers=12) as ex:
            list(ex.map(worker, range(24)))

        self.assertEqual(errors, [], f"mixed errors: {errors[:3]}")

    def test_close_shuts_worker_thread_connections(self):
        # N-review H1: a connection opened on a worker thread must actually be closed by close()
        # (needs check_same_thread=False). Otherwise it leaks and holds the DB/-wal file open.
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(lambda: self.store.create()).result()  # opens a conn on the worker thread
        conns = list(self.store._all_conns)
        self.assertTrue(conns)
        self.store.close()
        for c in conns:
            try:
                c.execute("SELECT 1")
                self.fail("connection still open after close()")
            except sqlite3.ProgrammingError as e:
                self.assertIn("closed", str(e).lower())  # closed — not a cross-thread error


if __name__ == "__main__":
    unittest.main()
