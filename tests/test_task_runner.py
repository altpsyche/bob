"""Detached background tasks: start_detached truncates its log by default and appends when asked (so a
resumable task keeps one growing log across relaunches)."""
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import contextlib
import io

import _common  # noqa: F401
import bob_checkpoint
import bob_core
import bob_loop
import bob_task_runner
import osenv
from bob import cli


def _wait_for(path: Path, needle: str, timeout: float = 5.0) -> str:
    """Poll a detached child's log until it contains `needle` (the child flushes asynchronously)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
            if needle in text:
                return text
        time.sleep(0.02)
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


class TestStartDetachedLog(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-detach-"))
        self.log = self.dir / "task.log"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _emit(self, text, append):
        osenv.start_detached([sys.executable, "-c", f"print({text!r})"],
                             log_path=str(self.log), append=append)
        return _wait_for(self.log, text)

    def test_start_detached_truncates_by_default(self):
        self._emit("one", append=False)
        text = self._emit("two", append=False)
        self.assertIn("two", text)
        self.assertNotIn("one", text)

    def test_start_detached_appends_when_requested(self):
        self._emit("one", append=False)
        text = self._emit("two", append=True)
        self.assertIn("one", text)
        self.assertIn("two", text)


class TestTaskRunner(unittest.TestCase):
    """The detached worker drives the durable loop and lets it record the terminal status; a SIGTERM
    (delivered via its cancel token) stops the run and leaves it cancelled."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-taskrun-"))
        self.db = self.dir / "cp.db"
        self.cfg = _common.fake_config()
        self.cfg["agent"]["checkpointDbPath"] = str(self.db)
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        shutil.rmtree(self.dir, ignore_errors=True)

    def _store(self):
        return bob_checkpoint.CheckpointStore(db_path=self.db, shadow_dir=self.dir / "blobs")

    def test_task_runner_records_terminal_status(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["final answer"])
        rc = bob_task_runner.run_task(self.cfg, "t1", "alice", goal="do it")
        self.assertEqual(rc, 0)
        got = self._store().load_run("t1", "alice")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["result"], "final answer")

    def test_sigterm_marks_cancelled(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["never reached"])
        cancel = bob_loop.CancelToken()
        cancel.cancel()   # stands in for the SIGTERM the handler would deliver
        bob_task_runner.run_task(self.cfg, "t2", "alice", goal="do it", cancel=cancel)
        self.assertEqual(self._store().load_run("t2", "alice")["status"], "cancelled")


class TestTaskVerbs(unittest.TestCase):
    """The task start/status/logs CLI verbs create, list, and inspect detached tasks, owner-scoped,
    without launching a real worker (start_detached is stubbed)."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-taskverb-"))
        self.db = self.dir / "cp.db"
        self.cfg = {"agent": {"checkpointDbPath": str(self.db), "defaultOwner": "local"}}
        self._orig_load = bob_core.load_config
        self._orig_detach = osenv.start_detached
        bob_core.load_config = lambda: self.cfg
        self.launched = []
        osenv.start_detached = lambda argv, **kw: (self.launched.append((argv, kw)) or 4321)

    def tearDown(self):
        bob_core.load_config = self._orig_load
        osenv.start_detached = self._orig_detach
        shutil.rmtree(self.dir, ignore_errors=True)

    def _store(self):
        return bob_checkpoint.CheckpointStore(db_path=self.db, shadow_dir=self.dir / "blobs")

    def _capture(self, fn, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(*a)
        return rc, buf.getvalue()

    def test_task_start_inserts_running_row_and_prints_id(self):
        rc, out = self._capture(cli._handle_task_start, ["do", "the", "thing"])
        self.assertEqual(rc, 0)
        run_id = out.strip()
        self.assertTrue(run_id)
        self.assertEqual(len(self.launched), 1)   # one detached worker launched
        argv = self.launched[0][0]
        self.assertIn("--run-id", argv)
        self.assertIn(run_id, argv)
        self.assertTrue(any(str(a).endswith("bob_task_runner.py") for a in argv))
        row = self._store().load_run(run_id, "local")
        self.assertEqual(row["goal"], "do the thing")
        self.assertEqual(row["pid"], 4321)

    def test_task_status_lists_only_this_owner(self):
        st = self._store()
        st.save_run("mine", "local", "running", "my goal", [], step=1)
        st.save_run("theirs", "other", "running", "their goal", [], step=1)
        rc, out = self._capture(cli._handle_task_status, [])
        self.assertIn("mine", out)
        self.assertNotIn("theirs", out)

    def test_task_logs_tails_log_file(self):
        log = self.dir / "t.log"
        log.write_text("line one\nline two\nline three\n", encoding="utf-8")
        st = self._store()
        st.save_run("r1", "local", "running", "g", [], step=1)
        st.set_run_process("r1", "local", 4321, str(log))
        rc, out = self._capture(cli._handle_task_logs, ["r1"])
        self.assertIn("line three", out)

    def test_task_status_flags_dead_pid(self):
        st = self._store()
        st.save_run("r1", "local", "running", "g", [], step=1)
        st.set_run_process("r1", "local", 2147483000, "/nope.log")  # a pid that is not alive
        rc, out = self._capture(cli._handle_task_status, ["r1"])
        self.assertIn("interrupted", out)

    def test_cancel_kills_pid_and_marks_cancelled(self):
        st = self._store()
        st.save_run("r1", "local", "running", "g", [], step=1)
        st.set_run_process("r1", "local", 4321, "/x.log")
        killed = []
        orig_alive, orig_kill = osenv.pid_alive, osenv.stop_process_tree
        osenv.pid_alive = lambda pid: True
        osenv.stop_process_tree = lambda pid: killed.append(pid)
        try:
            rc, _ = self._capture(cli._handle_task_cancel, ["r1"])
        finally:
            osenv.pid_alive, osenv.stop_process_tree = orig_alive, orig_kill
        self.assertEqual(rc, 0)
        self.assertEqual(killed, [4321])
        self.assertEqual(self._store().load_run("r1", "local")["status"], "cancelled")

    def test_resume_relaunches_same_run_id(self):
        st = self._store()
        st.save_run("r1", "local", "running", "keep going", [], step=2)
        rc, _ = self._capture(cli._handle_task_resume, ["r1"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.launched), 1)
        argv = self.launched[0][0]
        self.assertIn("--resume", argv)
        self.assertIn("r1", argv)

    def test_cancel_wrong_owner_refused(self):
        self._store().save_run("r1", "other", "running", "g", [], step=1)
        rc, _ = self._capture(cli._handle_task_cancel, ["r1"])   # config owner is 'local'
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
