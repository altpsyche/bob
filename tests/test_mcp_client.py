"""O7 — MCP client seam: connect to configured MCP servers, translate their tools to Bob tool_schemas
namespaced mcp:<server>:<tool>, route dispatch to a live round-trip, and default remote tools to O6
'ask'. Hermetic: the `mcp` package + a live transport are only touched by _RealConnection; every test
here injects a fake in-process connector, so the whole seam runs under bare python3 (no deps)."""
import json
import unittest
from types import SimpleNamespace

import _common
import bob_mcp_client as mc
from tool_registry import ToolRegistry


# --------------------------------------------------------------------------- fakes

class FakeConn:
    """A fake MCP connection: lists canned tools, records calls, optionally fails on call."""

    def __init__(self, tools, results=None, fail_call=False):
        self._tools = tools
        self._results = results or {}
        self._fail_call = fail_call
        self.calls = []
        self.closed = False

    def list_tools(self):
        return self._tools

    def call(self, tool, arguments, timeout):
        self.calls.append((tool, arguments, timeout))
        if self._fail_call:
            raise RuntimeError("boom")
        return self._results.get(tool, f"ran {tool} {arguments}")

    def close(self):
        self.closed = True


ECHO_TOOL = {"name": "echo", "description": "echoes input",
             "inputSchema": {"type": "object", "properties": {"x": {"type": "string"}},
                             "required": ["x"]}}


def _cfg(servers, timeout=30):
    return {"agent": {"mcpServers": servers, "mcpTimeout": timeout}}


# --------------------------------------------------------------------------- name mapping

class TestNameMapping(unittest.TestCase):
    def test_make_and_split_round_trip(self):
        name = mc.make_name("fs", "read_file")
        self.assertEqual(name, "mcp:fs:read_file")
        self.assertEqual(mc.split_name(name), ("fs", "read_file"))

    def test_split_tolerates_colon_in_tool(self):
        # a tool name that itself contains ':' splits only on the first separator after the server.
        self.assertEqual(mc.split_name("mcp:srv:ns:tool"), ("srv", "ns:tool"))

    def test_is_remote(self):
        self.assertTrue(mc.is_remote("mcp:fs:read"))
        self.assertFalse(mc.is_remote("file_read"))


# --------------------------------------------------------------------------- schema translation

class TestSchemaTranslation(unittest.TestCase):
    def test_maps_and_namespaces(self):
        schemas = mc.tool_schemas_for("fs", [ECHO_TOOL])
        self.assertEqual(len(schemas), 1)
        fn = schemas[0]["function"]
        self.assertEqual(fn["name"], "mcp:fs:echo")
        self.assertEqual(fn["description"], "echoes input")
        self.assertIn("x", fn["parameters"]["properties"])

    def test_defaults_missing_fields(self):
        schemas = mc.tool_schemas_for("s", [{"name": "bare"}])
        fn = schemas[0]["function"]
        self.assertIn("bare", fn["description"])            # synthesized description
        self.assertEqual(fn["parameters"], {"type": "object", "properties": {}})

    def test_skips_nameless(self):
        self.assertEqual(mc.tool_schemas_for("s", [{"description": "no name"}]), [])


# --------------------------------------------------------------------------- result flattening

class TestResultToText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(mc.result_to_text("hello"), "hello")

    def test_none(self):
        self.assertEqual(mc.result_to_text(None), "")

    def test_list_of_content_dicts(self):
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        self.assertEqual(mc.result_to_text(blocks), "a\nb")

    def test_call_tool_result_object(self):
        # mirrors the mcp package's CallToolResult(content=[TextContent(text=...)])
        result = SimpleNamespace(content=[SimpleNamespace(text="from-object")])
        self.assertEqual(mc.result_to_text(result), "from-object")

    def test_dict_with_content(self):
        self.assertEqual(mc.result_to_text({"content": [{"text": "x"}]}), "x")


# --------------------------------------------------------------------------- client routing

class TestClientRouting(unittest.TestCase):
    def test_connect_and_call(self):
        conn = FakeConn([ECHO_TOOL], results={"echo": "ECHOED"})
        client = mc.connect_servers(_cfg({"fs": {"id": "fs"}}),
                                    connector=lambda spec, t: conn)
        self.assertEqual(client.servers(), ["fs"])
        self.assertEqual(len(client.schemas), 1)
        out = client.call_tool("fs", "echo", {"x": "hi"})
        self.assertEqual(out, "ECHOED")
        self.assertEqual(conn.calls, [("echo", {"x": "hi"}, 30)])   # timeout threaded through

    def test_unknown_server_is_graceful(self):
        client = mc.McpClient()
        self.assertIn("not connected", client.call_tool("nope", "x", {}))

    def test_call_failure_is_graceful_string(self):
        conn = FakeConn([ECHO_TOOL], fail_call=True)
        client = mc.connect_servers(_cfg({"fs": {"id": "fs"}}), connector=lambda spec, t: conn)
        out = client.call_tool("fs", "echo", {})
        self.assertIn("MCP tool error", out)
        self.assertIn("mcp:fs:echo", out)

    def test_bad_server_skipped_others_survive(self):
        good = FakeConn([ECHO_TOOL])

        def connector(spec, t):
            if spec["id"] == "bad":
                raise RuntimeError("connect refused")
            return good

        client = mc.connect_servers(
            _cfg({"bad": {"id": "bad"}, "good": {"id": "good"}}), connector=connector)
        self.assertEqual(client.servers(), ["good"])       # bad one skipped (loud-fail)
        self.assertEqual(len(client.schemas), 1)

    def test_close_closes_all(self):
        conn = FakeConn([ECHO_TOOL])
        client = mc.connect_servers(_cfg({"fs": {"id": "fs"}}), connector=lambda spec, t: conn)
        client.close()
        self.assertTrue(conn.closed)


# --------------------------------------------------------------------------- registry registration

class TestRegistryRegistration(unittest.TestCase):
    def test_no_servers_is_no_op(self):
        reg = ToolRegistry()
        before = (list(reg.tool_schemas), dict(reg.dispatch), set(reg.remote_tools))
        mc.register_mcp_tools(reg, _cfg({}))
        self.assertEqual((reg.tool_schemas, reg.dispatch, reg.remote_tools),
                         (before[0], before[1], before[2]))       # byte-identical toolset
        self.assertFalse(hasattr(reg, "_mcp_client"))

    def test_registers_and_dispatches(self):
        conn = FakeConn([ECHO_TOOL], results={"echo": "REMOTE OK"})
        reg = ToolRegistry()
        mc.register_mcp_tools(reg, _cfg({"fs": {"id": "fs"}}),
                              connector=lambda spec, t: conn, quiet=True)
        # schema registered under the namespaced name
        names = [s["function"]["name"] for s in reg.tool_schemas]
        self.assertEqual(names, ["mcp:fs:echo"])
        self.assertIn("mcp:fs:echo", reg.remote_tools)
        # the connected client is stashed on the registry so its connections outlive build()
        self.assertIsInstance(getattr(reg, "_mcp_client", None), mc.McpClient)
        self.assertEqual(reg._mcp_client.servers(), ["fs"])
        # dispatch routes through the registry's normal path (so M7 truncation/O6 policy apply for free)
        out = reg.dispatch_call("mcp:fs:echo", json.dumps({"x": "hi"}))
        self.assertEqual(out, "REMOTE OK")
        self.assertEqual(conn.calls, [("echo", {"x": "hi"}, 30)])

    def test_filtered_view_hides_denied_remote(self):
        conn = FakeConn([ECHO_TOOL])
        reg = ToolRegistry()
        mc.register_mcp_tools(reg, _cfg({"fs": {"id": "fs"}}),
                              connector=lambda spec, t: conn, quiet=True)
        view = reg.filtered(deny={"mcp:fs:echo"})
        self.assertNotIn("mcp:fs:echo", view.remote_tools)
        self.assertIn("not available", view.dispatch_call("mcp:fs:echo", "{}"))


# --------------------------------------------------------------------------- O6 policy default = ask

class TestPolicyDefault(unittest.TestCase):
    """A remote tool defaults to 'ask' end-to-end through the real loop; a local tool stays 'allow'."""

    def setUp(self):
        import bob_core
        self._bc = bob_core
        self._orig_check = bob_core.check_litellm
        self._orig_client = bob_core.get_llm_client
        bob_core.check_litellm = lambda config=None: True
        call = '<tool_call>{"name": "mcp:fs:echo", "arguments": {"x": "hi"}}</tool_call>'
        bob_core.get_llm_client = lambda config=None: _common.scripted_client([call, "done."])

    def tearDown(self):
        self._bc.check_litellm = self._orig_check
        self._bc.get_llm_client = self._orig_client

    def _cfg(self):
        return _common.fake_config(agent={
            "toolFormat": "hermes", "maxSteps": 5,
            "maxContextTokens": 0, "maxToolResultTokens": 1000, "permissions": {},
        })

    def _run(self, remote, approve):
        import bob_loop
        reg = _common.FakeRegistry({"mcp:fs:echo": "REMOTE OK"})
        if remote:
            reg.remote_tools = {"mcp:fs:echo"}
        events = list(bob_loop.run_agent_events(
            "go", self._cfg(), agency="silent", registry=reg, approve=approve))
        return reg, events

    def test_resolve_default_param(self):
        from bob_permissions import PermissionPolicy, ASK
        # empty policy + default='ask' -> ask (the remote-tool path); default stays 'allow' otherwise.
        self.assertEqual(PermissionPolicy({}).resolve("mcp:fs:echo", default=ASK), ASK)
        self.assertEqual(PermissionPolicy({}).resolve("file_read"), "allow")

    def test_remote_tool_prompts_and_runs_on_approval(self):
        reg, events = self._run(remote=True, approve=lambda a: True)
        self.assertTrue(any(e["type"] == "approval_required" for e in events))
        self.assertIn("mcp:fs:echo", reg.dispatched)

    def test_remote_tool_fails_closed_without_approver(self):
        reg, events = self._run(remote=True, approve=None)
        self.assertNotIn("mcp:fs:echo", reg.dispatched)
        result = [e for e in events if e["type"] == "tool_result"][0]["result"]
        self.assertIn("denied", result)

    def test_local_tool_does_not_prompt(self):
        # same setup WITHOUT remote_tools -> allow, byte-identical to pre-O7 (no approval event).
        reg, events = self._run(remote=False, approve=None)
        self.assertIn("mcp:fs:echo", reg.dispatched)
        self.assertFalse(any(e["type"] == "approval_required" for e in events))


if __name__ == "__main__":
    unittest.main()
