"""Tool-result clearing (context editing). Once the transcript nears budget the loop replaces
OLD bulky tool-result messages with a compact stub (`[tool result rN cleared; N chars retained —
read_result('rN')]`) that stays re-fetchable via the model-callable `read_result` tool. The retention
store is the seam. Default off (`clearToolResults`) matches the prior path: no clearing,
and the read_result tool isn't even offered."""
import json
import unittest
from types import SimpleNamespace

import _common  # noqa: F401 — sys.path
import bob_core
import bob_loop
import read_result as read_result_tool
import tool_registry
from bob_loop import _clear_old_tool_results, _clear_hermes_responses
from tool_registry import ToolRegistry


def _retaining_registry(cap=50, store_max=40):
    reg = ToolRegistry()
    reg.max_result_chars = cap
    reg._result_store_max = store_max
    return reg


def _hermes_msg(name, result):
    """The exact shape run_agent_events builds for a hermes tool-result turn."""
    return {"role": "user",
            "content": f'<tool_response>{{"name": "{name}", "content": {json.dumps(result)}}}</tool_response>'}


class TestClearStub(unittest.TestCase):
    def test_stub_points_at_retained_handle_and_refetches(self):
        reg = _retaining_registry()
        big = "A" * 300
        retained = reg._truncate_and_retain(big)          # -> handle r1
        stub = reg.clear_stub(retained)
        self.assertIsNotNone(stub)
        self.assertIn("read_result('r1')", stub)
        self.assertIn("300 chars retained", stub)
        self.assertEqual(reg.read_result("r1"), big)       # full text still recoverable

    def test_no_handle_returns_none(self):
        reg = _retaining_registry()
        self.assertIsNone(reg.clear_stub("a small result with no retained handle"))

    def test_already_cleared_stub_is_idempotent(self):
        reg = _retaining_registry()
        retained = reg._truncate_and_retain("B" * 300)
        stub = reg.clear_stub(retained)
        self.assertIsNone(reg.clear_stub(stub))            # the stub itself has no 'retained as rN]'

    def test_evicted_handle_not_clearable(self):
        reg = _retaining_registry(store_max=1)
        r1 = reg._truncate_and_retain("C" * 300)           # handle r1
        reg._truncate_and_retain("D" * 300)                # r2 evicts r1
        self.assertIsNone(reg.clear_stub(r1))              # can't leave a dangling stub


class TestClearOpenAIMode(unittest.TestCase):
    def test_old_result_cleared_recent_protected(self):
        reg = _retaining_registry()
        old = {"role": "tool", "tool_call_id": "c1", "content": reg._truncate_and_retain("E" * 300)}
        recent = {"role": "tool", "tool_call_id": "c2", "content": reg._truncate_and_retain("F" * 300)}
        msgs = [{"role": "system", "content": "S"}, old, {"role": "assistant", "content": "ok"}, recent]
        out = _clear_old_tool_results(msgs, reg, keep_last=1, hermes=False)
        self.assertTrue(out[1]["content"].startswith("[tool result r1 cleared;"))  # old -> stub
        self.assertIs(out[3], msgs[3])                     # last message protected (unchanged object)
        self.assertEqual(reg.read_result("r1"), "E" * 300)  # still re-fetchable

    def test_small_result_left_untouched(self):
        reg = _retaining_registry()
        small = {"role": "tool", "tool_call_id": "c1", "content": "tiny"}
        msgs = [small, {"role": "assistant", "content": "x"}]
        out = _clear_old_tool_results(msgs, reg, keep_last=1, hermes=False)
        self.assertEqual(out[0]["content"], "tiny")


class TestClearHermesMode(unittest.TestCase):
    def test_inner_response_content_replaced_and_refetchable(self):
        reg = _retaining_registry()
        retained = reg._truncate_and_retain("G" * 300)      # r1
        old = _hermes_msg("bigtool", retained)
        msgs = [old, {"role": "assistant", "content": "next"}]
        out = _clear_old_tool_results(msgs, reg, keep_last=1, hermes=True)
        # Still a well-formed <tool_response> whose content is now the stub.
        inner = out[0]["content"].split("<tool_response>")[1].split("</tool_response>")[0]
        obj = json.loads(inner)
        self.assertEqual(obj["name"], "bigtool")
        self.assertIn("read_result('r1')", obj["content"])
        self.assertEqual(reg.read_result("r1"), "G" * 300)

    def test_non_json_segment_left_untouched(self):
        reg = _retaining_registry()
        msg = {"role": "user", "content": "<tool_response>not json</tool_response>"}
        new, changed = _clear_hermes_responses(msg["content"], reg)
        self.assertFalse(changed)
        self.assertEqual(new, msg["content"])

    def test_plain_user_message_ignored(self):
        reg = _retaining_registry()
        msgs = [{"role": "user", "content": "just a question"}, {"role": "assistant", "content": "a"}]
        out = _clear_old_tool_results(msgs, reg, keep_last=1, hermes=True)
        self.assertEqual(out[0]["content"], "just a question")


class TestReadResultTool(unittest.TestCase):
    def test_gated_on_clear_tool_results(self):
        self.assertFalse(read_result_tool.enabled({"agent": {"clearToolResults": False}}))
        self.assertTrue(read_result_tool.enabled({"agent": {"clearToolResults": True}}))
        self.assertFalse(read_result_tool.enabled({}))     # missing -> off

    def test_no_run_context_is_graceful(self):
        self.assertIn("unavailable", read_result_tool._read_result("r1"))

    def test_reads_through_run_context_registry(self):
        reg = _retaining_registry()
        reg._truncate_and_retain("H" * 300)                # r1
        tok = tool_registry._RUN_CONTEXT.set(SimpleNamespace(registry=reg))
        try:
            self.assertEqual(read_result_tool._read_result("r1"), "H" * 300)
            self.assertIn("Unknown result handle", read_result_tool._read_result("rZ"))
        finally:
            tool_registry._RUN_CONTEXT.reset(tok)


class TestRegistryBuildGating(unittest.TestCase):
    """Building the real registry: the read_result tool + the retention-store bump appear ONLY when
    clearToolResults is on, so the default toolset/behavior is unchanged."""

    def _cfg(self, on):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], clearToolResults=on, maxHistoryMsgs=40)
        return cfg

    def test_tool_and_store_bump_only_when_enabled(self):
        on = ToolRegistry.build(self._cfg(True), quiet=True)
        self.assertIn("read_result", on.dispatch)
        self.assertGreaterEqual(on._result_store_max, 40)

    def test_default_off_no_tool_no_bump(self):
        off = ToolRegistry.build(self._cfg(False), quiet=True)
        self.assertNotIn("read_result", off.dispatch)
        self.assertEqual(off._result_store_max, 8)          # default when clearing is off


class TestLoopDefaultOffUnaffected(unittest.TestCase):
    """The loop with clearToolResults=false must not touch messages (regression / byte-identity)."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def test_clearing_not_invoked_when_off(self):
        # Spy: if the clear pass ran it would call this; default-off must never call it.
        called = {"n": 0}
        orig = bob_loop._clear_old_tool_results
        bob_loop._clear_old_tool_results = lambda *a, **k: (called.__setitem__("n", called["n"] + 1) or a[0])
        try:
            bob_core.get_llm_client = lambda config=None: _common.scripted_client(["done"])
            list(bob_loop.run_agent_events("go", _common.fake_config(), agency="silent",
                                           registry=_common.FakeRegistry()))
        finally:
            bob_loop._clear_old_tool_results = orig
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()
