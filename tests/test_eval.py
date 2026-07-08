"""The agent-capability eval harness: fixtures score stably (100%) and a regressed loop drops the
score. Records-based (no live model), so it runs under bare python3 on the CPU CI tier."""
import os
import sys
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval"))
import run_eval  # noqa: E402
from tasks import EVAL_TASKS  # noqa: E402


class TestEvalHarness(unittest.TestCase):
    def test_fixtures_all_pass(self):
        earned, total, results = run_eval.run_all()
        self.assertGreaterEqual(len(results), 7)      # the declared task set
        self.assertEqual(earned, total)               # a healthy loop scores 100%
        for name, e, m, _checks in results:
            self.assertEqual(e, m, f"task {name} regressed: {e}/{m}")

    def test_regression_drops_score(self):
        # a "model" that refuses to call the required tool must lose points — proves the scorer is a
        # real regression signal, not a rubber stamp.
        broken = {
            "name": "broken", "goal": "Echo hi.", "turns": ["I will not use any tools."],
            "results": {"echo": "hi"}, "expect_final": True,
            "expect_tools": ["echo"], "expect_final_contains": ["Done"],
        }
        earned, total, _ = run_eval.run_all([broken])
        self.assertGreater(total, 0)
        self.assertLess(earned, total)

    def test_score_task_rubric(self):
        class _Reg:
            dispatched = ["a"]
        task = {"expect_final": True, "expect_tools": ["a"], "forbid_tools": ["b"],
                "forbid_final_contains": ["SECRET"], "expect_final_contains": ["ok"]}
        earned, total, checks = run_eval.score_task(task, [], _Reg(), {"result": "ok"})
        self.assertEqual(earned, total)               # all rubric checks satisfied
        # flip one expectation -> a miss
        task2 = {"expect_tools": ["missing"]}
        e2, t2, _ = run_eval.score_task(task2, [], _Reg(), {"result": "ok"})
        self.assertEqual((e2, t2), (0, 1))

    def test_main_non_gating_exit_zero(self):
        self.assertEqual(run_eval.main([]), 0)
        self.assertEqual(run_eval.main(["--json"]), 0)
        self.assertEqual(run_eval.main(["--gate", "0.5"]), 0)   # fixtures pass a lenient gate


if __name__ == "__main__":
    unittest.main()
