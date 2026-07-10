"""Durable run state: the checkpoint store round-trips a run's loop state (messages with tool-result
turns embedded, step index, identity, todos), owner scoping is enforced, status transitions persist, and
the run-state table coexists with the file-snapshot table in one DB without interfering."""
import shutil
import tempfile
import unittest
from pathlib import Path

import _common  # noqa: F401
import bob_checkpoint
import bob_core
import bob_loop


def _hermes_turns():
    """A minimal message list shaped like the loop's hermes-mode transcript: system, user goal,
    assistant tool call, and the tool-result user turn."""
    return [
        {"role": "system", "content": "you are bob"},
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": "<tool_call>{\"name\": \"file_read\"}</tool_call>"},
        {"role": "user", "content": "<tool_response>{\"name\": \"file_read\", \"content\": \"ok\"}</tool_response>"},
    ]


def _openai_turns():
    """A message list shaped like the loop's openai-mode transcript, including a tool role turn."""
    return [
        {"role": "system", "content": "you are bob"},
        {"role": "user", "content": "read a.py"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "file_read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]


class TestRunStateStore(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-runs-"))
        self.store = bob_checkpoint.CheckpointStore(
            db_path=self.dir / "cp.db", shadow_dir=self.dir / "blobs")

    def tearDown(self):
        self.store.close() if hasattr(self.store, "close") else None
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_run_state_round_trips_messages_and_step(self):
        for label, msgs in (("hermes", _hermes_turns()), ("openai", _openai_turns())):
            with self.subTest(mode=label):
                rid = f"run-{label}"
                self.store.save_run(rid, "alice", "running", "read a.py", msgs, step=3,
                                    exit_requested=False, scope="proj", agent_depth=0,
                                    todos=[{"text": "read", "done": True}], metrics={"tools_run": 1})
                got = self.store.load_run(rid, "alice")
                self.assertIsNotNone(got)
                self.assertEqual(got["messages"], msgs)
                self.assertEqual(got["step"], 3)
                self.assertEqual(got["goal"], "read a.py")
                self.assertEqual(got["scope"], "proj")
                self.assertEqual(got["agent_depth"], 0)
                self.assertEqual(got["todos"], [{"text": "read", "done": True}])
                self.assertEqual(got["metrics"], {"tools_run": 1})
                self.assertFalse(got["exit_requested"])

    def test_run_state_owner_isolated(self):
        self.store.save_run("r1", "alice", "running", "g", _hermes_turns(), step=1)
        self.assertIsNone(self.store.load_run("r1", "bob"))
        self.assertIsNotNone(self.store.load_run("r1", "alice"))
        self.assertEqual([r["run_id"] for r in self.store.list_runs("bob")], [])
        self.assertEqual([r["run_id"] for r in self.store.list_runs("alice")], ["r1"])

    def test_status_transitions_persist(self):
        self.store.save_run("r1", "alice", "running", "g", _hermes_turns(), step=1)
        self.store.set_status("r1", "alice", "done", result="final answer")
        got = self.store.load_run("r1", "alice")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["result"], "final answer")

    def test_save_run_upsert_preserves_created_at(self):
        self.store.save_run("r1", "alice", "running", "g", _hermes_turns(), step=1)
        created = self.store.load_run("r1", "alice")["created_at"]
        self.store.save_run("r1", "alice", "running", "g", _hermes_turns(), step=2)
        again = self.store.load_run("r1", "alice")
        self.assertEqual(again["created_at"], created)
        self.assertEqual(again["step"], 2)

    def test_snapshot_and_run_tables_coexist_in_one_db(self):
        work = self.dir / "work"
        work.mkdir()
        p = work / "a.py"
        p.write_text("x\n", encoding="utf-8")
        self.store.snapshot("r1", 0, "alice", [p], prefer_git=False)
        self.store.save_run("r1", "alice", "running", "g", _hermes_turns(), step=1)
        # both concerns readable, keyed differently, no interference
        self.assertTrue(self.store.has("r1", 0))
        self.assertEqual(self.store.steps("r1", "alice"), [0])
        self.assertEqual(self.store.load_run("r1", "alice")["step"], 1)


class TestCheckpointOnStep(unittest.TestCase):
    """The loop persists run state each step when agent.checkpoint is on, records a terminal status, and
    writes nothing when the gate is off."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-ckpt-write-"))
        self.db = self.dir / "cp.db"
        self.cfg = _common.fake_config()
        self.cfg["agent"]["checkpoint"] = True
        self.cfg["agent"]["checkpointDbPath"] = str(self.db)
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        shutil.rmtree(self.dir, ignore_errors=True)

    def _run(self, turns, results=None, run_id="r1", owner="alice"):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _common.FakeRegistry(results or {})
        events = list(bob_loop.run_agent_events(
            "go", self.cfg, agency="silent", registry=reg, run_id=run_id, owner=owner))
        return events, reg

    def _store(self):
        return bob_checkpoint.CheckpointStore(db_path=self.db, shadow_dir=self.dir / "blobs")

    def test_run_persists_growing_step_index(self):
        turns = [
            '<tool_call>{"name": "echo", "arguments": {"x": "a"}}</tool_call>',
            '<tool_call>{"name": "echo", "arguments": {"x": "b"}}</tool_call>',
            "done",
        ]
        self._run(turns, {"echo": "ok"})
        got = self._store().load_run("r1", "alice")
        self.assertIsNotNone(got)
        self.assertEqual(got["step"], 2)   # two completed tool steps -> next step is 2

    def test_completed_run_marked_done(self):
        turns = ['<tool_call>{"name": "echo", "arguments": {"x": "a"}}</tool_call>', "final answer"]
        self._run(turns, {"echo": "ok"})
        got = self._store().load_run("r1", "alice")
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["result"], "final answer")

    def test_failed_run_marked_failed(self):
        self._run([""], {})   # empty completion trips the empty-response guard -> error path
        got = self._store().load_run("r1", "alice")
        self.assertEqual(got["status"], "failed")
        self.assertIn("empty response", (got["result"] or "").lower())

    def test_checkpoint_off_writes_nothing(self):
        self.cfg["agent"]["checkpoint"] = False
        self._run(['<tool_call>{"name": "echo", "arguments": {"x": "a"}}</tool_call>', "done"], {"echo": "ok"})
        self.assertEqual(self._store().list_runs("alice"), [])


class TestResume(unittest.TestCase):
    """Resuming a checkpointed run continues from the next step, does not re-run tools whose results are
    already recorded, restores identity/todos, refuses unknown/wrong-owner runs, and refuses a run whose
    lease is still held by another process."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="bob-resume-"))
        self.db = self.dir / "cp.db"
        self.cfg = _common.fake_config()
        self.cfg["agent"]["checkpoint"] = True
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

    def _seed_interrupted_run(self, run_id="r1", owner="alice"):
        """Persist a run as if it had completed step 0 (a tool ran, its result recorded) and was then
        killed before step 1 -- exactly the state resume must continue from."""
        messages = [
            {"role": "system", "content": "you are bob"},
            {"role": "user", "content": "do the task"},
            {"role": "assistant", "content": "<tool_call>{\"name\": \"already_ran\"}</tool_call>"},
            {"role": "user", "content": "<tool_response>{\"name\": \"already_ran\", \"content\": \"done\"}</tool_response>"},
        ]
        self._store().save_run(run_id, owner, "running", "do the task", messages, step=1,
                               scope="proj", agent_depth=0, todos=[{"text": "finish", "done": False}])

    def test_resume_reenters_at_next_step_and_finishes(self):
        self._seed_interrupted_run()
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["all finished"])
        reg = _common.FakeRegistry({})
        events = list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="alice", resume="r1"))
        final = [e for e in events if e["type"] == "final"][-1]
        self.assertEqual(final["result"], "all finished")
        self.assertEqual(self._store().load_run("r1", "alice")["status"], "done")

    def test_resume_does_not_reexecute_completed_tool(self):
        self._seed_interrupted_run()
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["done now"])
        reg = _common.FakeRegistry({})
        list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="alice", resume="r1"))
        # the tool whose result is already in the persisted transcript must never be dispatched again
        self.assertNotIn("already_ran", reg.dispatched)

    def test_resume_restores_owner_scope_depth_and_todos(self):
        self._seed_interrupted_run()
        captured = {}

        class _Reg(_common.FakeRegistry):
            def dispatch_call(self, name, arguments_json, context=None):
                captured["owner"] = context.owner
                captured["scope"] = context.scope
                captured["agent_depth"] = context.agent_depth
                captured["todos"] = list(context.todos)
                return super().dispatch_call(name, arguments_json, context)

        turns = ['<tool_call>{"name": "probe", "arguments": {}}</tool_call>', "ok"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _Reg({"probe": "x"})
        list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="alice", resume="r1"))
        self.assertEqual(captured["owner"], "alice")
        self.assertEqual(captured["scope"], "proj")
        self.assertEqual(captured["agent_depth"], 0)
        self.assertEqual(captured["todos"], [{"text": "finish", "done": False}])

    def test_resume_unknown_or_wrong_owner_errors(self):
        self._seed_interrupted_run(owner="alice")
        reg = _common.FakeRegistry({})
        # unknown id
        ev = list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="alice", resume="nope"))
        self.assertEqual(ev[-1]["type"], "error")
        # wrong owner
        ev = list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="mallory", resume="r1"))
        self.assertEqual(ev[-1]["type"], "error")

    def test_concurrent_resume_of_live_run_refused(self):
        self._seed_interrupted_run()
        # simulate another process holding the lease
        held = self._store().acquire_lease("r1", "alice", holder="other-proc", ttl_seconds=3600)
        self.assertTrue(held)
        reg = _common.FakeRegistry({})
        ev = list(bob_loop.run_agent_events(
            "", self.cfg, agency="silent", registry=reg, owner="alice", resume="r1"))
        self.assertEqual(ev[-1]["type"], "error")
        self.assertIn("already active", ev[-1]["message"])


if __name__ == "__main__":
    unittest.main()
