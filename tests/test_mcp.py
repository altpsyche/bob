"""MCP seam: registry tools -> MCP descriptors + a dispatch round-trip, and the disabled
gate. Hermetic: the mcp package and stdio transport are only touched by serve() when enabled."""
import json
import unittest

import _common
import bob_mcp_server as mcp


class _Reg:
    tool_schemas = [
        {"type": "function", "function": {
            "name": "echo", "description": "echoes input",
            "parameters": {"type": "object",
                           "properties": {"x": {"type": "string"}}, "required": ["x"]},
        }},
    ]

    def dispatch_call(self, name, args_json):
        return f"ran {name} {json.loads(args_json)}"


class TestMcpSeam(unittest.TestCase):
    def test_build_mcp_tools_lists_registry_tools(self):
        tools = mcp.build_mcp_tools(_Reg())
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "echo")
        self.assertEqual(tools[0]["description"], "echoes input")
        self.assertIn("x", tools[0]["inputSchema"]["properties"])

    def test_dispatch_round_trip(self):
        out = mcp.dispatch(_Reg(), "echo", {"x": "hi"})
        self.assertIn("ran echo", out)
        self.assertIn("hi", out)

    def test_serve_refuses_when_disabled(self):
        cfg = _common.fake_config()
        cfg["agent"]["mcpEnabled"] = False
        self.assertEqual(mcp.serve(cfg), 1)


if __name__ == "__main__":
    unittest.main()
