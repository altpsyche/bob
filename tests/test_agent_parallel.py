"""O2 — parallel tool execution: wall-clock speedup for side-effect-free calls, stable result order,
mutating/deny/ask stay sequential, cancel abandons the batch, and cap==1 reproduces the sequential
loop. Fake client + FakeRegistry; no model."""
import time
import unittest

import _common
import bob_core
import bob_loop

# One step emitting three independent tool calls, then a final answer.
THREE_CALLS = ('<tool_call>{"name": "echo1", "arguments": {}}</tool_call>'
               '<tool_call>{"name": "echo2", "arguments": {}}</tool_call>'
               '<tool_call>{"name": "echo3", "arguments": {}}</tool_call>')
RESULTS = {"echo1": "r1", "echo2": "r2", "echo3": "r3"}


def _cfg(max_parallel):
    return _common.fake_config(agent={
        "toolFormat": "hermes", "maxSteps": 5, "maxContextTokens": 0,
        "maxToolResultTokens": 1000, "maxParallelTools": max_parallel})


class TestParallelUnits(unittest.TestCase):
    def test_cap(self):
        self.assertEqual(bob_loop._parallel_cap(1), 1)
        self.assertEqual(bob_loop._parallel_cap(0), 1)
        self.assertEqual(bob_loop._parallel_cap("x"), 1)
        cap = bob_loop._parallel_cap(100)
        self.assertGreaterEqual(cap, 1)
        self.assertLessEqual(cap, 100)

    def test_eligible_readonly_allow(self):
        reg = _common.FakeRegistry(RESULTS)
        ctx = bob_loop.RunContext(cancel=None, config={}, registry=reg, run_id="x", approve=None)
        self.assertTrue(bob_loop._parallel_eligible("echo1", reg, ctx, "silent"))

    def test_ineligible_when_mutating(self):
        reg = _common.FakeRegistry(RESULTS, mutating_tools={"echo2"})
        ctx = bob_loop.RunContext(cancel=None, config={}, registry=reg, run_id="x", approve=None)
        self.assertFalse(bob_loop._parallel_eligible("echo2", reg, ctx, "silent"))

    def test_ineligible_when_approval_required(self):
        reg = _common.FakeRegistry(RESULTS, approval_required_tools={"echo3"})
        ctx = bob_loop.RunContext(cancel=None, config={}, registry=reg, run_id="x", approve=None)
        self.assertFalse(bob_loop._parallel_eligible("echo3", reg, ctx, "silent"))

    def test_ineligible_when_policy_not_allow(self):
        from bob_permissions import PermissionPolicy
        reg = _common.FakeRegistry(RESULTS)
        policy = PermissionPolicy({"agent": {"permissions": {"tools": {"echo1": "ask"}}}})
        ctx = bob_loop.RunContext(cancel=None, config={}, registry=reg, run_id="x", approve=None,
                                  policy=policy)
        self.assertFalse(bob_loop._parallel_eligible("echo1", reg, ctx, "silent"))
        self.assertTrue(bob_loop._parallel_eligible("echo2", reg, ctx, "silent"))


class TestParallelExecution(unittest.TestCase):
    def setUp(self):
        self._check = bob_core.check_litellm
        self._client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([THREE_CALLS, "done."])

    def tearDown(self):
        bob_core.check_litellm = self._check
        bob_core.get_llm_client = self._client

    def _events(self, cfg, reg):
        return list(bob_loop.run_agent_events("go", cfg, agency="silent", registry=reg))

    def _tool_results(self, events):
        return [(e["name"], e["result"]) for e in events if e["type"] == "tool_result"]

    @unittest.skipUnless(bob_loop._parallel_cap(4) >= 2,
                         "host core count caps parallelism at 1 (cpu-2 floor) — wall-clock test skipped")
    def test_parallel_is_faster_than_sequential(self):
        reg = _common.FakeRegistry(RESULTS, delay=0.1)
        t0 = time.monotonic()
        events = self._events(_cfg(4), reg)
        elapsed = time.monotonic() - t0
        self.assertEqual(len(self._tool_results(events)), 3)
        self.assertLess(elapsed, 0.25, f"3x100ms should overlap to ~100ms, took {elapsed:.3f}s")

    def test_default_is_sequential(self):
        reg = _common.FakeRegistry(RESULTS, delay=0.1)
        t0 = time.monotonic()
        self._events(_cfg(1), reg)          # default cap == 1
        elapsed = time.monotonic() - t0
        self.assertGreater(elapsed, 0.25, f"cap==1 must serialize (~300ms), took {elapsed:.3f}s")

    def test_result_order_is_stable(self):
        # Even though they complete concurrently, results surface in ORIGINAL call order.
        reg = _common.FakeRegistry(RESULTS, delay=0.05)
        results = self._tool_results(self._events(_cfg(4), reg))
        self.assertEqual(results, [("echo1", "r1"), ("echo2", "r2"), ("echo3", "r3")])

    def test_parallel_and_sequential_agree(self):
        # cap==1 and cap==4 produce identical tool_result event sequences.
        seq = self._tool_results(self._events(_cfg(1), _common.FakeRegistry(RESULTS)))
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([THREE_CALLS, "done."])
        par = self._tool_results(self._events(_cfg(4), _common.FakeRegistry(RESULTS)))
        self.assertEqual(seq, par)

    def test_mutating_in_batch_still_ordered(self):
        # A mutating tool in a parallel batch runs sequentially but results stay in order.
        reg = _common.FakeRegistry(RESULTS, mutating_tools={"echo2"})
        results = self._tool_results(self._events(_cfg(4), reg))
        self.assertEqual(results, [("echo1", "r1"), ("echo2", "r2"), ("echo3", "r3")])

    def test_cancel_before_step_runs_nothing(self):
        reg = _common.FakeRegistry(RESULTS, delay=0.05)
        cancel = bob_loop.CancelToken()
        cancel.cancel()
        events = list(bob_loop.run_agent_events("go", _cfg(4), agency="silent",
                                                registry=reg, cancel=cancel))
        self.assertEqual(events[-1]["type"], "final")
        self.assertEqual(events[-1]["reason"], "cancelled")
        self.assertEqual(reg.dispatched, [])   # no tool ran


if __name__ == "__main__":
    unittest.main()
