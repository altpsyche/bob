"""Planning + reflection + self-repair, three config-gated loop phases (all default off →
identical to the plain loop). Plan: a bounded ponder turn injects a step list before the loop. Verify: a
critic turn before the final answer; on "not DONE" it injects the critique and continues. Self-repair:
a failed tool call is retried once (catches a flaky tool)."""
import unittest
from types import SimpleNamespace

import _common
import bob_core
import bob_loop


def _cfg(**agent_over):
    cfg = _common.fake_config()
    cfg["agent"] = dict(cfg["agent"], agency="silent", **agent_over)
    return cfg


def _recording_client(calls, turns):
    """Records the per-call message-content list; streams turns[i] as the assistant reply."""
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


class _FlakyRegistry(_common.FakeRegistry):
    """A tool that errors on its first dispatch and succeeds on the second (transient/flaky)."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def dispatch_call(self, name, arguments_json, context=None):
        self.dispatched.append(name)
        self.calls += 1
        return "Tool error (flaky): transient blip" if self.calls == 1 else "recovered-output"


class _LoopBase(unittest.TestCase):
    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _final(self, events):
        return [e for e in events if e["type"] == "final"][-1]["result"]


class TestDefaultOff(_LoopBase):
    def test_all_phases_off_single_call(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls, ["hi there"])
        events = list(bob_loop.run_agent_events("go", _cfg(), agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(self._final(events), "hi there")
        self.assertEqual(len(calls), 1)   # no extra plan/verify turns


class TestPlanPhase(_LoopBase):
    def test_plan_turn_injects_step_list(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: _recording_client(calls, ["1. do X\n2. do Y", "final"])
        events = list(bob_loop.run_agent_events("solve it", _cfg(plan=True), agency="silent",
                                                registry=_common.FakeRegistry()))
        # First create() was the plan turn (system=_PLAN_SYSTEM); the answer turn sees the injected plan.
        self.assertEqual(self._final(events), "final")
        self.assertTrue(any(bob_loop._PLAN_SYSTEM in c for c in calls[0]))
        self.assertTrue(any("Plan for this task:" in c and "do X" in c for c in calls[1]),
                        "the injected plan must be visible on the answering turn")


class TestVerifyPhase(_LoopBase):
    def test_verify_not_done_continues_then_returns_corrected(self):
        # step1 answer -> critic (not DONE) -> injected -> step2 corrected -> critic skipped (once).
        turns = ["wrong answer", "Missing: the total is absent", "corrected answer"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        events = list(bob_loop.run_agent_events("q", _cfg(verify=True), agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(self._final(events), "corrected answer")

    def test_verify_done_accepts_immediately(self):
        turns = ["the answer", "DONE"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        events = list(bob_loop.run_agent_events("q", _cfg(verify=True), agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(self._final(events), "the answer")

    def test_verify_runs_at_most_once(self):
        # Even if the critic keeps complaining, verify fires once — the 2nd answer is returned as-is.
        turns = ["a1", "still wrong", "a2", "still wrong again", "a3"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        events = list(bob_loop.run_agent_events("q", _cfg(verify=True, maxSteps=10), agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(self._final(events), "a2")   # a1 -> critique -> a2 (verify latched)


class TestSelfRepair(_LoopBase):
    def _run(self, self_repair):
        turns = ['<tool_call>{"name": "flaky", "arguments": {}}</tool_call>', "done"]
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(turns)
        reg = _FlakyRegistry()
        events = list(bob_loop.run_agent_events("go", _cfg(selfRepair=self_repair), agency="silent",
                                                registry=reg))
        tr = [e for e in events if e["type"] == "tool_result"][0]
        return reg, tr

    def test_retry_recovers_flaky_tool(self):
        reg, tr = self._run(self_repair=True)
        self.assertEqual(reg.calls, 2)                 # dispatched twice
        self.assertEqual(tr["result"], "recovered-output")

    def test_off_does_not_retry(self):
        reg, tr = self._run(self_repair=False)
        self.assertEqual(reg.calls, 1)                 # single dispatch, error surfaced
        self.assertIn("Tool error", tr["result"])


if __name__ == "__main__":
    unittest.main()
