"""Mid-run steering: a CancelToken carries a steer inbox so a driver can inject a user message into a
running loop without cancelling it. The inbox is thread-safe, not inherited by child (sub-agent) tokens,
and the loop drains it at the step boundary (never mid-tool-batch)."""
import unittest
from types import SimpleNamespace

import _common  # noqa: F401
import bob_core
import bob_loop
from bob_loop import CancelToken


class TestInbox(unittest.TestCase):
    def test_steer_and_drain(self):
        tok = CancelToken()
        tok.steer("do X")
        tok.steer("then Y")
        self.assertEqual(tok.drain_steer(), ["do X", "then Y"])
        self.assertEqual(tok.drain_steer(), [])          # drained -> empty

    def test_blank_steer_ignored(self):
        tok = CancelToken()
        tok.steer("   ")
        tok.steer("")
        self.assertEqual(tok.drain_steer(), [])

    def test_child_does_not_inherit_inbox(self):
        parent = CancelToken()
        parent.steer("root only")
        child = parent.child()
        self.assertEqual(child.drain_steer(), [])         # steering targets the root, not sub-runs
        self.assertEqual(parent.drain_steer(), ["root only"])

    def test_cancel_still_works_alongside_steer(self):
        tok = CancelToken()
        tok.steer("keep going")
        tok.cancel()
        self.assertTrue(tok.cancelled())                  # steering and cancel are independent


class TestLoopDrains(unittest.TestCase):
    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client

    def _cfg(self):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], agency="silent", maxSteps=6)
        return cfg

    def _recording_client(self, calls, turns):
        state = {"i": 0}

        class _C:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)

            def create(self, model, messages, tools, stream, timeout):
                calls.append([m.get("content", "") for m in messages])
                i = state["i"]
                state["i"] += 1
                return _common._FakeStream([_common._content_chunk(turns[min(i, len(turns) - 1)])])

        return _C()

    def test_queued_steer_reaches_next_step(self):
        calls = []
        bob_core.get_llm_client = lambda config=None: self._recording_client(calls, ["the answer"])
        tok = CancelToken()
        tok.steer("please also handle errors")            # queued before the run starts
        events = list(bob_loop.run_agent_events("go", self._cfg(), agency="silent",
                                                registry=_common.FakeRegistry(), cancel=tok))
        # a steer event was emitted, and the injected text is visible on the first LLM turn's messages
        self.assertTrue(any(e["type"] == "steer" for e in events))
        self.assertTrue(any("please also handle errors" in c for c in calls[0]))


if __name__ == "__main__":
    unittest.main()
