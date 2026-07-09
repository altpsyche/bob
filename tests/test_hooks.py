"""Agent-loop lifecycle hooks: PreToolUse (block / force-ask / rewrite args), PostToolUse (rewrite
result), and Stop (inject to continue). Hooks may only tighten, never loosen the approval floor; a
hook that raises is caught so it can't strand a run; with no hooks the loop is byte-identical."""
import unittest
from types import SimpleNamespace

import _common  # noqa: F401
import bob_core
import bob_loop
from tool_registry import ToolRegistry


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def log(self, *a, **k): pass


def _tc(name, args='{}'):
    return SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=args))


def _dispatch(reg, name="do_thing", args='{}', agency="silent", approve=lambda a: True):
    ctx = SimpleNamespace(owner="local", policy=None, agent_depth=0, tracer=None, trace_span=None,
                          config={})
    return list(bob_loop._dispatch_with_approval(
        _tc(name, args), "c1", registry=reg, context=ctx, agency=agency,
        approve=approve, log=_Log(), rid="r1"))


class TestPreToolUse(unittest.TestCase):
    def _reg(self, pre):
        reg = _common.FakeRegistry()
        reg.hooks = {"PreToolUse": pre, "PostToolUse": [], "Stop": []}
        return reg

    def test_hook_blocks_call(self):
        reg = self._reg([lambda name, args, ctx: {"decision": "deny"}])
        events = _dispatch(reg)
        tr = [e for e in events if e["type"] == "tool_result"][0]
        self.assertIn("blocked by a PreToolUse hook", tr["result"])
        self.assertEqual(reg.dispatched, [])            # never dispatched

    def test_hook_forces_ask(self):
        reg = self._reg([lambda name, args, ctx: {"decision": "ask"}])
        denials = _dispatch(reg, approve=lambda a: False)   # forced ask, then denied by approver
        self.assertTrue(any(e["type"] == "approval_required" for e in denials))
        self.assertEqual(reg.dispatched, [])

    def test_hook_rewrites_args(self):
        seen = {}

        class _Reg(_common.FakeRegistry):
            def dispatch_call(self, name, arguments_json, context=None):
                seen["args"] = arguments_json
                return "ok"

        reg = _Reg()
        reg.hooks = {"PreToolUse": [lambda name, args, ctx: {"updatedInput": {"x": 1}}],
                     "PostToolUse": [], "Stop": []}
        _dispatch(reg)
        self.assertEqual(seen["args"], '{"x": 1}')

    def test_raising_hook_is_ignored(self):
        def boom(name, args, ctx):
            raise RuntimeError("bad hook")
        reg = self._reg([boom])
        events = _dispatch(reg)
        tr = [e for e in events if e["type"] == "tool_result"][0]
        self.assertNotIn("blocked", tr["result"])       # dispatch proceeded normally
        self.assertEqual(reg.dispatched, ["do_thing"])


class TestPostToolUse(unittest.TestCase):
    def test_hook_rewrites_result(self):
        reg = _common.FakeRegistry()
        reg.hooks = {"PreToolUse": [], "PostToolUse":
                     [lambda name, args, result, ctx: {"result": "REDACTED"}], "Stop": []}
        events = _dispatch(reg)
        tr = [e for e in events if e["type"] == "tool_result"][0]
        self.assertEqual(tr["result"], "REDACTED")


class TestStopHook(unittest.TestCase):
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

    def _final(self, events):
        return [e for e in events if e["type"] == "final"][-1]["result"]

    def test_stop_inject_continues_once(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["first", "second"])
        reg = _common.FakeRegistry()
        calls = {"n": 0}

        def stop_hook(final, ctx):
            calls["n"] += 1
            return {"inject": "keep going"} if calls["n"] == 1 else None

        reg.hooks = {"PreToolUse": [], "PostToolUse": [], "Stop": [stop_hook]}
        events = list(bob_loop.run_agent_events("go", self._cfg(), agency="silent", registry=reg))
        self.assertEqual(self._final(events), "second")   # injected once -> one extra turn
        self.assertEqual(calls["n"], 1)                    # fired at most once (bounded)

    def test_no_hooks_is_byte_identical(self):
        bob_core.get_llm_client = lambda config=None: _common.scripted_client(["only answer"])
        events = list(bob_loop.run_agent_events("go", self._cfg(), agency="silent",
                                                registry=_common.FakeRegistry()))
        self.assertEqual(self._final(events), "only answer")


if __name__ == "__main__":
    unittest.main()
