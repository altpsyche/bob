"""Long-horizon eval coverage: a run resumed from prior history reaches the same conclusion an
uninterrupted run would, and an over-budget run terminates with a final event instead of hanging or
re-running the tool it never reached. These pin the resumption-integrity and graceful-degradation
properties the durable-run work depends on. Hermetic: scripted client, no live model."""
import os
import sys
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_core
import bob_loop

_EVAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
if _EVAL not in sys.path:
    sys.path.insert(0, _EVAL)
import run_eval  # noqa: E402


class TestLongHorizonEval(unittest.TestCase):
    def setUp(self):
        self._orig = (bob_core.check_litellm, bob_core.get_llm_client)
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm, bob_core.get_llm_client = self._orig

    def test_resume_from_history_reaches_same_final(self):
        # The same goal answered cold vs with the first turn pre-baked into history reaches the same final.
        prior = [
            {"role": "user", "content": "Research the topic."},
            {"role": "assistant", "content": "Key facts: X and Y."},
        ]
        cfg = _common.fake_config()
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["Conclusion: Z."])
        cold = list(bob_loop.run_agent_events("State the conclusion.", cfg, agency="silent",
                                              registry=_common.FakeRegistry()))
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["Conclusion: Z."])
        warm = list(bob_loop.run_agent_events("State the conclusion.", cfg, agency="silent",
                                              registry=_common.FakeRegistry(), history=prior))
        cold_final = [e for e in cold if e["type"] == "final"][-1]["result"]
        warm_final = [e for e in warm if e["type"] == "final"][-1]["result"]
        self.assertEqual(cold_final, warm_final)

    def test_step_budget_stops_run_with_final(self):
        task = next(t for t in run_eval.EVAL_TASKS if t["name"] == "step_budget_exhaustion")
        earned, total, checks = run_eval.run_task(task)
        self.assertEqual(earned, total, dict(checks))

    def test_resume_integrity_fixture_scores_full(self):
        task = next(t for t in run_eval.EVAL_TASKS if t["name"] == "resume_integrity")
        earned, total, _ = run_eval.run_task(task)
        self.assertEqual(earned, total)

    def test_computer_use_gating_fixtures_pass(self):
        # off -> refused, ask -> approval event + not run when denied, approved -> runs.
        names = ["computer_use_denied_when_off", "computer_use_ask_enforced", "computer_use_approved_runs"]
        for name in names:
            task = next(t for t in run_eval.EVAL_TASKS if t["name"] == name)
            earned, total, checks = run_eval.run_task(task)
            self.assertEqual(earned, total, (name, dict(checks)))


if __name__ == "__main__":
    unittest.main()
