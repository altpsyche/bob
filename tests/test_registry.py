"""M13 — ToolRegistry: real-tool discovery/config + contract validation + dispatch."""
import shutil
import textwrap
import unittest
from pathlib import Path

import _common
from tool_registry import ToolRegistry


class TestRealTools(unittest.TestCase):
    """Every shipped tool must import, satisfy the contract, and configure() cleanly."""

    def setUp(self):
        self.reg = ToolRegistry.build(_common.fake_config(), set())

    def test_no_load_errors(self):
        self.assertEqual(self.reg.errors, [], f"tools failed to load: {self.reg.errors}")

    def test_expected_tools_present(self):
        for name in ("web", "file", "git", "shell", "fabric"):
            self.assertIn(name, self.reg._loaded_names)

    def test_memory_tool_gated_on_feature_flag(self):
        # memory.enabled is false in fake_config → the memory tools must NOT be offered to the agent
        # (so the model can't recall/recite saved notes unprompted). They return when the flag is on.
        self.assertNotIn("memory", self.reg._loaded_names)
        on = ToolRegistry.build(_common.fake_config(memory={"enabled": True, "dbPath": "data/bob.db"}), set())
        self.assertIn("memory", on._loaded_names)

    def test_schemas_have_names(self):
        for s in self.reg.tool_schemas:
            self.assertIn("function", s)
            self.assertTrue(s["function"].get("name"))


class TestContractValidation(unittest.TestCase):
    """A TOOL_DEFS name with no DISPATCH entry is a hard error (M9) — the tool is skipped."""

    def _write_tool(self, body: str) -> Path:
        d = Path(_common.REPO) / "tests" / "_tmp_tools"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "broken_tool.py"
        p.write_text(textwrap.dedent(body), encoding="utf-8")
        return p

    def tearDown(self):
        shutil.rmtree(Path(_common.REPO) / "tests" / "_tmp_tools", ignore_errors=True)

    def test_missing_dispatch_is_contract_error(self):
        p = self._write_tool(
            """
            TOOL_DEFS = [{"type": "function", "function": {"name": "ghost", "parameters": {}}}]
            DISPATCH = {}                 # 'ghost' declared but not dispatchable
            def configure(config): pass
            """
        )
        reg = ToolRegistry()
        reg._load_one("broken_tool", p, {})
        self.assertNotIn("broken_tool", reg._loaded_names)
        self.assertTrue(any(phase == "contract" for _, phase, _ in reg.errors))

    def test_missing_tool_defs_is_contract_error(self):
        p = self._write_tool(
            """
            DISPATCH = {}
            def configure(config): pass
            """
        )
        reg = ToolRegistry()
        reg._load_one("broken_tool", p, {})
        self.assertNotIn("broken_tool", reg._loaded_names)


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.reg.dispatch = {"echo": lambda text="": text.upper()}
        self.reg.max_result_chars = 10

    def test_unknown_tool(self):
        self.assertIn("Unknown tool", self.reg.dispatch_call("nope", "{}"))

    def test_bad_json(self):
        self.assertIn("Bad arguments JSON", self.reg.dispatch_call("echo", "{not json"))

    def test_parse_error_pseudo_tool(self):
        out = self.reg.dispatch_call("__parse_error__", '{"error": "boom"}')
        self.assertIn("malformed JSON", out)

    def test_result_truncated_to_cap(self):
        out = self.reg.dispatch_call("echo", '{"text": "abcdefghijklmnop"}')
        self.assertIn("truncated", out)
        self.assertIn("retained as", out)
        kept = out.split("\n[...", 1)[0]
        self.assertLessEqual(len(kept), 10)
        # NE0/O3 seam: the trimmed tail is retained (not silently lost) and re-readable by handle.
        handle = out.split("retained as ", 1)[1].rstrip("]").strip()
        self.assertEqual(len(self.reg.read_result(handle)), 16)

    def test_filtered_view_denies_tool(self):
        # NE0/O1 seam: a filtered() view hides denied tools and refuses to dispatch them, while the
        # allowed tool still runs through the shared dispatch — no rebuild.
        self.reg.dispatch = {"echo": lambda text="": text, "danger": lambda: "boom"}
        self.reg.tool_schemas = [
            {"type": "function", "function": {"name": "echo"}},
            {"type": "function", "function": {"name": "danger"}},
        ]
        self.reg.approval_required_tools = {"danger"}
        view = self.reg.filtered(deny={"danger"})
        self.assertEqual([s["function"]["name"] for s in view.tool_schemas], ["echo"])
        self.assertNotIn("danger", view.approval_required_tools)
        self.assertIn("not available", view.dispatch_call("danger", "{}"))
        self.assertEqual(view.dispatch_call("echo", '{"text": "hi"}'), "hi")


if __name__ == "__main__":
    unittest.main()
