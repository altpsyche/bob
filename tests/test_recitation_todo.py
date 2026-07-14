"""Goal recitation + living TODO. The `todo` tool maintains a run-local task list (RunContext.
todos); the recitation hook re-emits the goal + open items at the CONTEXT TAIL of each request (never
stored in `messages`). Both gated: agent.todoTool / agent.recite default off → no change to the
request (no new tool, no tail block)."""
import unittest
from types import SimpleNamespace

import _common
import bob_core
import bob_loop
import todo as todo_tool
import tool_registry
from bob_loop import CancelToken, RunContext, _recitation_block
from tool_registry import ToolRegistry


def _cfg(**agent_over):
    cfg = _common.fake_config()
    cfg["agent"] = dict(cfg["agent"], agency="silent", **agent_over)
    return cfg


def _recording_client(calls, turns):
    state = {"i": 0}

    class _C:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)

        def create(self, model, messages, tools, stream, timeout, **kwargs):
            calls.append([m.get("content", "") for m in messages])
            i = state["i"]
            state["i"] += 1
            return _common._FakeStream([_common._content_chunk(turns[min(i, len(turns) - 1)])])

    return _C()


class TestRecitationBlock(unittest.TestCase):
    def test_goal_only_when_no_todos(self):
        out = _recitation_block("ship the feature", [])
        self.assertIn("Current goal: ship the feature", out)
        self.assertNotIn("Open TODO", out)

    def test_open_items_listed_done_hidden(self):
        todos = [{"task": "write code", "status": "done"},
                 {"task": "add tests", "status": "in_progress"},
                 {"task": "update docs", "status": "pending"}]
        out = _recitation_block("do it", todos)
        self.assertIn("add tests", out)
        self.assertIn("update docs", out)
        self.assertNotIn("write code", out)          # done items are not recited
        self.assertIn("[~] add tests", out)           # in_progress marker


class TestTodoTool(unittest.TestCase):
    def _ctx(self):
        return RunContext(cancel=CancelToken(), config=_cfg(todoTool=True), registry=None,
                          run_id="r", approve=None)

    def test_no_run_context_is_graceful(self):
        self.assertIn("unavailable", todo_tool._todo_write(["x"]))

    def test_write_then_update_mutates_run_local_state(self):
        ctx = self._ctx()
        tok = tool_registry._RUN_CONTEXT.set(ctx)
        try:
            todo_tool._todo_write(["design", {"task": "build", "status": "in_progress"}])
            self.assertEqual([t["task"] for t in ctx.todos], ["design", "build"])
            todo_tool._todo_update("design", "done")
            self.assertEqual(ctx.todos[0]["status"], "done")
            todo_tool._todo_update("ship it")          # new task -> appended, default done
            self.assertEqual(ctx.todos[-1], {"task": "ship it", "status": "done"})
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)

    def test_gating(self):
        self.assertFalse(todo_tool.enabled({"agent": {"todoTool": False}}))
        self.assertTrue(todo_tool.enabled({"agent": {"todoTool": True}}))

    def test_registry_present_only_when_enabled(self):
        on = ToolRegistry.build(_cfg(todoTool=True), quiet=True)
        off = ToolRegistry.build(_cfg(todoTool=False), quiet=True)
        self.assertIn("todo_write", on.dispatch)
        self.assertNotIn("todo_write", off.dispatch)


class TestRecitationInLoop(unittest.TestCase):
    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def test_recitation_appended_at_tail_when_on(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls, ["done"])
        list(bob_loop.run_agent_events("achieve the objective", _cfg(recite=True),
                                       agency="silent", registry=_common.FakeRegistry()))
        # Last message of the request is the recitation, carrying the goal.
        self.assertIn("Current goal: achieve the objective", calls[0][-1])

    def test_no_recitation_when_off(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls, ["done"])
        list(bob_loop.run_agent_events("achieve the objective", _cfg(),
                                       agency="silent", registry=_common.FakeRegistry()))
        self.assertFalse(any("Current goal:" in c for c in calls[0]))   # byte-identical: no tail block


if __name__ == "__main__":
    unittest.main()
