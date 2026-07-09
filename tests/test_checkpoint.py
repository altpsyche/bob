"""Checkpoint + rewind: the shadow store round-trips bytes and undoes created files, owner scoping is
enforced, (run_id, step) is idempotent, and the git backend snapshots via stash-create without touching
the working tree/index and restores on rewind."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401
import bob_checkpoint


class TestShadowStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-ckpt-"))
        self.store = bob_checkpoint.CheckpointStore(
            db_path=self.dir / "cp.db", shadow_dir=self.dir / "blobs")
        self.work = self.dir / "work"
        self.work.mkdir()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _w(self, name, text):
        p = self.work / name
        p.write_text(text, encoding="utf-8")
        return p

    def test_snapshot_then_restore_round_trips(self):
        p = self._w("a.py", "original\n")
        self.store.snapshot("run1", 0, "alice", [p], prefer_git=False)
        p.write_text("mutated\n", encoding="utf-8")
        n = self.store.restore("run1", 0, "alice")
        self.assertEqual(n, 1)
        self.assertEqual(p.read_text(), "original\n")

    def test_restore_removes_a_created_file(self):
        p = self.work / "new.py"                      # does not exist at snapshot time
        self.store.snapshot("run1", 1, "alice", [p], prefer_git=False)
        p.write_text("created by the step\n", encoding="utf-8")
        self.store.restore("run1", 1, "alice")
        self.assertFalse(p.exists())                   # rewind undoes the creation

    def test_owner_scoping(self):
        p = self._w("a.py", "orig\n")
        self.store.snapshot("run1", 0, "alice", [p], prefer_git=False)
        with self.assertRaises(KeyError):
            self.store.restore("run1", 0, "bob")       # different owner cannot see it

    def test_idempotent_per_run_step(self):
        p = self._w("a.py", "orig\n")
        self.assertTrue(self.store.snapshot("run1", 0, "alice", [p], prefer_git=False))
        p.write_text("changed after first snapshot\n", encoding="utf-8")
        # a second snapshot for the same (run_id, step) is a no-op -> the first snapshot is preserved
        self.assertFalse(self.store.snapshot("run1", 0, "alice", [p], prefer_git=False))
        p.write_text("mutated again\n", encoding="utf-8")
        self.store.restore("run1", 0, "alice")
        self.assertEqual(p.read_text(), "orig\n")

    def test_steps_lists_checkpoints(self):
        p = self._w("a.py", "x\n")
        self.store.snapshot("run1", 0, "alice", [p], prefer_git=False)
        self.store.snapshot("run1", 2, "alice", [p], prefer_git=False)
        self.assertEqual(self.store.steps("run1", "alice"), [0, 2])


@unittest.skipUnless(shutil.which("git"), "git not present")
class TestGitBackend(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-ckpt-git-"))
        self.repo = self.dir / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "t@t")
        self._git("config", "user.name", "t")
        (self.repo / "a.py").write_text("v1\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "init")
        self.store = bob_checkpoint.CheckpointStore(
            db_path=self.dir / "cp.db", shadow_dir=self.dir / "blobs")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args],
                              capture_output=True, text=True, check=True)

    def test_git_snapshot_and_restore(self):
        p = self.repo / "a.py"
        p.write_text("v2-uncommitted\n", encoding="utf-8")   # dirty working tree to capture
        wrote = self.store.snapshot("run1", 0, "alice", [p], prefer_git=True)
        self.assertTrue(wrote)
        cp = self.store.get("run1", 0, "alice")
        self.assertEqual(cp["kind"], "git")                  # used the git backend
        # working tree/index untouched by stash create:
        self.assertEqual(p.read_text(), "v2-uncommitted\n")
        # mutate further, then rewind restores the snapshot content
        p.write_text("v3-bad-edit\n", encoding="utf-8")
        self.store.restore("run1", 0, "alice")
        self.assertEqual(p.read_text(), "v2-uncommitted\n")


class TestLoopWiring(unittest.TestCase):
    """The loop snapshots a mutating step's affected files (via AFFECTS) before dispatch when
    agent.checkpointEdits is on; off is byte-identical (no snapshot)."""

    def setUp(self):
        import bob_core
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True
        self.bc = bob_core
        self.dir = Path(tempfile.mkdtemp(prefix="bob-ckpt-loop-"))
        self.target = self.dir / "t.py"
        self.target.write_text("before\n", encoding="utf-8")

    def tearDown(self):
        self.bc.check_litellm = self._orig_check
        self.bc.get_llm_client = self._orig_client
        shutil.rmtree(self.dir, ignore_errors=True)

    def _reg(self):
        reg = _common.FakeRegistry(mutating_tools={"edit_it"})
        reg.affects = {"edit_it": lambda args: [str(self.target)]}
        return reg

    def _cfg(self, **over):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], agency="silent", maxSteps=3,
                            checkpointDbPath=str(self.dir / "cp.db"), **over)
        return cfg

    def _run(self, cfg):
        import bob_core, bob_loop
        turns = ['<tool_call>{"name": "edit_it", "arguments": {}}</tool_call>', "done"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        list(bob_loop.run_agent_events("go", cfg, agency="silent", registry=self._reg(),
                                       run_id="run42", owner="alice"))

    def test_checkpoint_written_before_mutating_step(self):
        import bob_checkpoint
        self._run(self._cfg(checkpointEdits=True))
        store = bob_checkpoint.CheckpointStore(db_path=self.dir / "cp.db")
        self.assertIn(0, store.steps("run42", "alice"))     # step 0 snapshotted

    def test_off_writes_no_checkpoint(self):
        import bob_checkpoint
        self._run(self._cfg(checkpointEdits=False))
        # the db may not even exist; if it does, there are no rows for this run
        db = self.dir / "cp.db"
        if db.exists():
            store = bob_checkpoint.CheckpointStore(db_path=db)
            self.assertEqual(store.steps("run42", "alice"), [])


if __name__ == "__main__":
    unittest.main()
