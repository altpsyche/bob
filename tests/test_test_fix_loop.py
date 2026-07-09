"""Lint/test-fix support (bob_testfix): failure parsers, the generic exit-code fallback, the
forward-progress signature, and run_checks over the mocked sandbox seam."""
import subprocess
import unittest

import _common  # noqa: F401 — puts scripts/ on sys.path
import bob_core
import bob_loop
import bob_testfix
import sandbox


class TestParsers(unittest.TestCase):
    def test_pytest_summary_lines(self):
        out = ("=== FAILURES ===\n"
               "FAILED tests/test_x.py::test_adds - AssertionError: 1 != 2\n"
               "FAILED tests/test_y.py::TestZ::test_q\n")
        fails = bob_testfix.parse_pytest(out, "")
        self.assertEqual(len(fails), 2)
        self.assertEqual(fails[0]["file"], "tests/test_x.py")
        self.assertEqual(fails[0]["test"], "test_adds")
        self.assertIn("AssertionError", fails[0]["message"])

    def test_unittest_fail_and_error(self):
        err = "FAIL: test_adds (tests.test_x.TestX)\nERROR: test_boom (tests.test_x.TestX)\n"
        fails = bob_testfix.parse_unittest("", err)
        self.assertEqual({f["message"] for f in fails}, {"FAIL", "ERROR"})

    def test_tsc_error_lines(self):
        out = "src/a.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.\n"
        fails = bob_testfix.parse_tsc(out, "")
        self.assertEqual(fails[0]["file"], "src/a.ts")
        self.assertEqual(fails[0]["line"], 12)
        self.assertEqual(fails[0]["test"], "TS2322")

    def test_eslint_grouped_by_file(self):
        out = ("/repo/src/a.js\n"
               "  10:5  error  Unexpected var  no-var\n"
               "  12:1  warning  Missing semi  semi\n")
        fails = bob_testfix.parse_eslint(out, "")
        self.assertEqual(len(fails), 2)
        self.assertEqual(fails[0]["file"], "/repo/src/a.js")
        self.assertEqual(fails[0]["line"], 10)
        self.assertEqual(fails[0]["test"], "no-var")

    def test_sniff(self):
        self.assertEqual(bob_testfix.sniff("pytest -q"), "pytest")
        self.assertEqual(bob_testfix.sniff("npx tsc --noEmit"), "tsc")
        self.assertEqual(bob_testfix.sniff("eslint ."), "eslint")
        self.assertEqual(bob_testfix.sniff("make check"), "")


class TestSummarize(unittest.TestCase):
    def test_pass_has_no_failures_or_tail(self):
        s = bob_testfix.summarize("pytest -q", 0, "3 passed", "")
        self.assertTrue(s.passed)
        self.assertEqual(s.failures, [])
        self.assertEqual(s.tail, "")

    def test_unknown_tool_uses_exit_and_tail(self):
        s = bob_testfix.summarize("make check", 2, "line1\nline2\nBUILD FAILED", "")
        self.assertFalse(s.passed)
        self.assertEqual(s.failures, [])            # no parser -> generic
        self.assertIn("BUILD FAILED", s.tail)
        self.assertIn("exit 2", s.as_feedback())
        self.assertIn("BUILD FAILED", s.as_feedback())

    def test_named_parser_forced(self):
        s = bob_testfix.summarize("make check", 1, "FAILED t.py::test_a - boom", "", parser="pytest")
        self.assertEqual(len(s.failures), 1)

    def test_signature_stable_and_distinct(self):
        a = bob_testfix.summarize("pytest", 1, "FAILED t.py::test_a - x", "")
        a2 = bob_testfix.summarize("pytest", 1, "FAILED t.py::test_a - x", "")
        b = bob_testfix.summarize("pytest", 1, "FAILED t.py::test_b - y", "")
        self.assertEqual(a.signature, a2.signature)     # same failure -> same signature
        self.assertNotEqual(a.signature, b.signature)   # different failure -> different signature


class TestRunChecks(unittest.TestCase):
    def setUp(self):
        self._orig = sandbox.run_command

    def tearDown(self):
        sandbox.run_command = self._orig

    def _fake(self, rc, out):
        def rc_fn(argv, cfg, **kw):
            return subprocess.CompletedProcess(argv, rc, out, "")
        return rc_fn

    def test_runs_only_configured_commands(self):
        sandbox.run_command = self._fake(0, "ok")
        cfg = {"agent": {"testCmd": "pytest -q"}}          # no lintCmd
        res = bob_testfix.run_checks(cfg, which=("lint", "test"))
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0].passed)

    def test_failing_command_summarized(self):
        sandbox.run_command = self._fake(1, "FAILED t.py::test_a - boom")
        cfg = {"agent": {"testCmd": "pytest -q"}}
        res = bob_testfix.run_checks(cfg, which=("test",))
        self.assertFalse(res[0].passed)
        self.assertEqual(res[0].failures[0]["test"], "test_a")

    def test_no_commands_returns_empty(self):
        sandbox.run_command = self._fake(0, "")
        self.assertEqual(bob_testfix.run_checks({"agent": {}}), [])


class TestLoopWiring(unittest.TestCase):
    """The post-finalize test-fix gate: on a failing check it injects the failures and continues; on
    pass it finalizes; off is byte-identical; the forward-progress guard stops a stuck loop."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        self._orig_run_checks = bob_testfix.run_checks
        bob_core.check_litellm = lambda config=None: True

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        bob_testfix.run_checks = self._orig_run_checks

    def _cfg(self, **agent_over):
        cfg = _common.fake_config()
        cfg["agent"] = dict(cfg["agent"], agency="silent", maxSteps=6, **agent_over)
        return cfg

    def _fail(self):
        return bob_testfix.summarize("pytest", 1, "FAILED t.py::test_a - boom", "")

    def _pass(self):
        return bob_testfix.summarize("pytest", 0, "ok", "")

    def _final(self, events):
        return [e for e in events if e["type"] == "final"][-1]["result"]

    def test_fail_then_pass_reruns_to_green(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["first answer", "second answer"])
        seq = [[self._fail()], [self._pass()]]
        state = {"i": 0}

        def fake_run_checks(config, which=("lint", "test"), timeout=120):
            r = seq[min(state["i"], len(seq) - 1)]
            state["i"] += 1
            return r

        bob_testfix.run_checks = fake_run_checks
        events = list(bob_loop.run_agent_events(
            "go", self._cfg(autoFix=True, testCmd="pytest -q"), agency="silent",
            registry=_common.FakeRegistry(), approve=lambda action: True))
        self.assertEqual(self._final(events), "second answer")   # re-ran after the fix, then finalized
        self.assertEqual(state["i"], 2)                          # checks ran twice (fail, then pass)
        tr = [e for e in events if e["type"] == "tool_result" and e.get("name") == "run_checks"]
        self.assertTrue(any("test_a" in e["result"] for e in tr))   # failures reached the transcript

    def test_off_is_byte_identical(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["the answer"])
        called = {"n": 0}
        bob_testfix.run_checks = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or []
        events = list(bob_loop.run_agent_events(
            "go", self._cfg(autoFix=False, testCmd="pytest -q"), agency="silent",
            registry=_common.FakeRegistry(), approve=lambda action: True))
        self.assertEqual(self._final(events), "the answer")
        self.assertEqual(called["n"], 0)                         # gate never ran

    def test_denied_approval_skips_gate(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["the answer"])
        called = {"n": 0}
        bob_testfix.run_checks = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or [self._fail()]
        events = list(bob_loop.run_agent_events(
            "go", self._cfg(autoFix=True, testCmd="pytest -q"), agency="silent",
            registry=_common.FakeRegistry(), approve=lambda action: False))
        self.assertEqual(self._final(events), "the answer")      # not approved -> finalized, no rerun
        self.assertEqual(called["n"], 0)

    def test_forward_progress_guard_stops_on_repeat(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["a1", "a2", "a3"])
        bob_testfix.run_checks = lambda *a, **k: [self._fail()]   # always the same failure
        events = list(bob_loop.run_agent_events(
            "go", self._cfg(autoFix=True, testCmd="pytest -q", autoFixRounds=5), agency="silent",
            registry=_common.FakeRegistry(), approve=lambda action: True))
        # a1 -> fail (inject) -> a2 -> same failure signature -> stop and finalize a2.
        self.assertEqual(self._final(events), "a2")


if __name__ == "__main__":
    unittest.main()
