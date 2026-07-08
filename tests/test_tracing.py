"""Tracing seam: no-op when disabled (byte-identical), spans record + nest when enabled, and the
run/tool spans wire through the real loop carrying the run-id. Hermetic: a fake sink stands in for
the OTLP exporter (the opentelemetry package is only touched by _otlp_sink, which is smoke-only), so
this runs under bare python3."""
import unittest

import _common
import bob_core
import bob_loop
from bob_tracing import Tracer, make_tracer, _NOOP_SPAN

ECHO_CALL = '<tool_call>{"name": "echo", "arguments": {"x": "hi"}}</tool_call>'


class TestTracerUnit(unittest.TestCase):
    def test_disabled_is_noop(self):
        rec = []
        t = Tracer(enabled=False, sink=rec.append)
        with t.span("x", {"a": 1}) as sp:
            self.assertIs(sp, _NOOP_SPAN)
            sp.set("k", "v").set_status("ok")   # all no-ops
        self.assertEqual(rec, [])               # sink never called
        self.assertEqual(t.spans, [])

    def test_enabled_records_span_with_attrs_and_duration(self):
        rec = []
        t = Tracer(enabled=True, sink=rec.append)
        with t.span("agent.tool", {"tool": "echo"}) as sp:
            sp.set("result_chars", 3).set_status("ok")
        self.assertEqual(len(rec), 1)
        s = rec[0]
        self.assertEqual(s.name, "agent.tool")
        self.assertEqual(s.attributes["tool"], "echo")
        self.assertEqual(s.attributes["result_chars"], 3)
        self.assertEqual(s.status, "ok")
        self.assertIn("duration_ms", s.attributes)
        self.assertIsNone(s.parent_id)          # root

    def test_child_nests_under_parent(self):
        t = Tracer(enabled=True)
        with t.span("agent.run") as run:
            with t.span("agent.tool", parent=run) as tool:
                self.assertEqual(tool.parent_id, run.span_id)
                self.assertEqual(tool.trace_id, run.trace_id)   # shared trace

    def test_cross_tracer_nesting_shares_trace(self):
        # a sub-agent uses its OWN tracer instance but nests under the parent's span (same trace tree).
        parent_t = Tracer(enabled=True)
        child_t = Tracer(enabled=True)
        run = parent_t.span("agent.run")
        sub = child_t.span("agent.run", parent=run)     # sub-run's root, parented across tracers
        self.assertEqual(sub.parent_id, run.span_id)
        self.assertEqual(sub.trace_id, run.trace_id)

    def test_exception_marks_error_and_reraises(self):
        rec = []
        t = Tracer(enabled=True, sink=rec.append)
        with self.assertRaises(ValueError):
            with t.span("boom") as sp:
                raise ValueError("nope")
        self.assertEqual(rec[0].status, "error")
        self.assertIn("nope", rec[0].attributes["exception"])

    def test_sink_failure_never_propagates(self):
        def bad(_s):
            raise RuntimeError("exporter down")
        t = Tracer(enabled=True, sink=bad)
        with t.span("x"):        # must not raise despite the failing sink
            pass

    def test_make_tracer_off_and_on(self):
        self.assertFalse(make_tracer({"agent": {"tracing": False}}).enabled)
        self.assertFalse(make_tracer({}).enabled)
        rec = []
        t = make_tracer({"agent": {"tracing": True}}, sink=rec.append)
        self.assertTrue(t.enabled)
        t.span("x").end()
        self.assertEqual(len(rec), 1)


class TestTracingInLoop(unittest.TestCase):
    """Run/tool spans through the real loop, with make_tracer stubbed to a recording tracer."""

    def setUp(self):
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        self._orig_make = bob_loop.make_tracer
        bob_core.check_litellm = lambda config=None: True
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([ECHO_CALL, "done."])

    def tearDown(self):
        bob_core.check_litellm = self._orig_check
        bob_core.get_llm_client = self._orig_client
        bob_loop.make_tracer = self._orig_make

    def _cfg(self):
        return _common.fake_config(agent={
            "toolFormat": "hermes", "maxSteps": 5,
            "maxContextTokens": 0, "maxToolResultTokens": 1000,
        })

    def _run(self, enabled, rec):
        bob_loop.make_tracer = lambda config: Tracer(enabled=enabled, sink=rec.append)
        reg = _common.FakeRegistry({"echo": "REAL RESULT"})
        list(bob_loop.run_agent_events("go", self._cfg(), agency="silent", registry=reg, run_id="rid123"))

    def test_run_and_tool_spans_nest_with_run_id(self):
        rec = []
        self._run(enabled=True, rec=rec)
        runs = [s for s in rec if s.name == "agent.run"]
        tools = [s for s in rec if s.name == "agent.tool"]
        self.assertEqual(len(runs), 1)
        self.assertGreaterEqual(len(tools), 1)
        self.assertEqual(runs[0].attributes["run_id"], "rid123")
        # every tool span is a child of the run span and shares its trace
        for t in tools:
            self.assertEqual(t.parent_id, runs[0].span_id)
            self.assertEqual(t.trace_id, runs[0].trace_id)
            self.assertEqual(t.attributes["tool"], "echo")
        self.assertIn("steps", runs[0].attributes)      # closed with run counters

    def test_disabled_emits_no_spans_but_logs_unchanged(self):
        rec = []
        with self.assertLogs("bob.agent", level="INFO") as cm:
            self._run(enabled=False, rec=rec)
        self.assertEqual(rec, [])                        # no OTLP/span activity
        # metrics line still emitted (file-log unchanged whether tracing is on or off)
        self.assertTrue(any("done steps=" in ln for ln in cm.output))


if __name__ == "__main__":
    unittest.main()
