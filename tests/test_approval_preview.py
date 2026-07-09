"""Diff-preview in the approval path: a tool's PREVIEW renderer enriches the approval_required event
with a rendered preview (file_edit -> a diff), the registry collects/propagates previews, and a raising
renderer falls back to raw args so approvals never break."""
import unittest
from types import SimpleNamespace

import _common  # noqa: F401
import bob_loop
from tool_registry import ToolRegistry


class TestRegistryCollectsPreviews(unittest.TestCase):
    def test_file_edit_registers_a_preview(self):
        reg = ToolRegistry.build(
            _common.fake_config(agent={"toolFormat": "hermes", "maxSteps": 5, "maxToolResultTokens": 1000,
                                       "allowedWritePaths": ["."]}), set())
        self.assertIn("file_edit", reg.previews)
        self.assertTrue(callable(reg.previews["file_edit"]))

    def test_view_filters_previews(self):
        reg = ToolRegistry.build(
            _common.fake_config(agent={"toolFormat": "hermes", "maxSteps": 5, "maxToolResultTokens": 1000,
                                       "allowedWritePaths": ["."]}), set())
        hidden = reg.filtered(allow=["file_read"])   # file_edit not visible
        self.assertNotIn("file_edit", hidden.previews)


class TestApprovalCarriesPreview(unittest.TestCase):
    """Drive _dispatch_with_approval directly with a fake registry whose PREVIEW renders a marker."""

    def _tc(self, name, args):
        return SimpleNamespace(id="c1", function=SimpleNamespace(name=name, arguments=args))

    def _run(self, previews, args='{"path": "x"}'):
        reg = _common.FakeRegistry()
        reg.previews = previews
        reg.approval_required_tools = {"edit_tool"}   # force the ask branch
        ctx = SimpleNamespace(owner="local", policy=None, agent_depth=0, tracer=None, trace_span=None,
                              config={})
        events = list(bob_loop._dispatch_with_approval(
            self._tc("edit_tool", args), "c1", registry=reg, context=ctx, agency="silent",
            approve=lambda action: True, log=_Log(), rid="r1"))
        return [e for e in events if e["type"] == "approval_required"][0]

    def test_preview_included_when_renderer_present(self):
        ev = self._run({"edit_tool": lambda a: f"DIFF for {a['path']}"})
        self.assertEqual(ev["preview"], "DIFF for x")
        self.assertIn("arguments", ev)                 # raw args still present

    def test_no_preview_key_without_renderer(self):
        ev = self._run({})
        self.assertNotIn("preview", ev)

    def test_raising_renderer_falls_back_to_raw_args(self):
        def boom(a):
            raise RuntimeError("render bug")
        ev = self._run({"edit_tool": boom})
        self.assertNotIn("preview", ev)                # fail-safe: no preview, approval still emitted
        self.assertIn("arguments", ev)


class _Log:
    def info(self, *a, **k): pass
    def warning(self, *a, **k): pass
    def log(self, *a, **k): pass


if __name__ == "__main__":
    unittest.main()
