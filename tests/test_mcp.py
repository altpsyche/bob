"""MCP seam: registry tools -> MCP descriptors + a dispatch round-trip, the disabled gate, and the
Streamable HTTP transport's policy (who may call it, which Host headers it answers to, which transport
a bare `bob agent mcp` picks). Hermetic: the mcp package and the live transports are only touched by
serve()/serve_http() when enabled, and the one test that needs a real ASGI response skips without
starlette."""
import asyncio
import json
import unittest
from unittest import mock

import _common
import bob_mcp_server as mcp

try:
    import starlette  # noqa: F401
    HAS_STARLETTE = True
except ImportError:                     # pragma: no cover — depends on the interpreter running the suite
    HAS_STARLETTE = False

try:
    import mcp as _mcp_pkg  # noqa: F401
    HAS_MCP = True
except ImportError:                     # pragma: no cover — depends on the interpreter running the suite
    HAS_MCP = False


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

    def test_serve_http_refuses_when_disabled(self):
        """The HTTP transport is gated by the same switch as stdio — enabling one never enables the
        other by accident."""
        cfg = _common.fake_config()
        cfg["agent"]["mcpEnabled"] = False
        self.assertEqual(mcp.serve_http(cfg), 1)


def _cfg(**agent):
    cfg = _common.fake_config()
    cfg["litellmKey"] = "sk-test"
    cfg["agent"].update({"mcpEnabled": True, **agent})
    return cfg


class TestHttpAuth(unittest.TestCase):
    """The HTTP endpoint is reachable, so it is closed by default and shares the agent API's tokens."""

    def test_accepts_litellm_key_and_api_tokens(self):
        cfg = _cfg(apiTokens=[{"token": "t-remote", "owner": "harness"}])
        self.assertEqual(mcp.accepted_tokens(cfg), {"sk-test", "t-remote"})
        self.assertTrue(mcp.authorize(cfg, "Bearer sk-test"))
        self.assertTrue(mcp.authorize(cfg, "Bearer t-remote"))

    def test_rejects_missing_wrong_and_non_bearer(self):
        cfg = _cfg()
        for header in ("", None, "Bearer ", "Bearer nope", "Basic sk-test", "sk-test"):
            self.assertFalse(mcp.authorize(cfg, header), header)

    def test_same_token_map_as_the_agent_api(self):
        """One source (bob_authstore.config_token_owners): a token that opens the agent API opens MCP."""
        import bob_agent_server as srv
        cfg = _cfg(apiTokens=["legacy-token"])
        self.assertEqual(set(srv._build_token_owner(cfg)), mcp.accepted_tokens(cfg))


class TestAllowedHosts(unittest.TestCase):
    def test_loopback_and_port_variants(self):
        hosts = mcp.allowed_hosts(_cfg(), "127.0.0.1", 8085)
        for want in ("localhost", "localhost:8085", "127.0.0.1", "127.0.0.1:8085"):
            self.assertIn(want, hosts)

    def test_wildcard_bind_is_not_an_allowed_host(self):
        """0.0.0.0 is a bind address no client ever sends as Host; a remote name comes from config."""
        hosts = mcp.allowed_hosts(_cfg(mcpAllowedHosts=["bob.lan:8085"]), "0.0.0.0", 8085)
        self.assertNotIn("0.0.0.0", hosts)
        self.assertNotIn("0.0.0.0:8085", hosts)
        self.assertIn("bob.lan:8085", hosts)

    def test_bind_host_is_allowed_when_concrete(self):
        self.assertIn("192.168.1.5:8085", mcp.allowed_hosts(_cfg(), "192.168.1.5", 8085))


class _Capture:
    """Collects an ASGI response so the gate can be driven without a socket."""

    def __init__(self):
        self.status, self.body = None, b""

    async def send(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")

    async def receive(self):  # pragma: no cover — the gate refuses before reading a body
        return {"type": "http.request", "body": b"", "more_body": False}


@unittest.skipUnless(HAS_STARLETTE, "starlette not installed in this interpreter")
class TestBearerGate(unittest.TestCase):
    """The gate wraps the whole app so an anonymous caller gets 401, not the mount's 307 redirect."""

    def setUp(self):
        self.passed = []

        async def _inner(scope, receive, send):
            self.passed.append(scope["path"])

        self.gate = mcp.BearerGate(_inner, _cfg())

    def _call(self, path, headers=()):
        cap = _Capture()
        scope = {"type": "http", "path": path,
                 "headers": [(k.encode(), v.encode()) for k, v in headers]}
        asyncio.run(self.gate(scope, cap.receive, cap.send))
        return cap

    def test_anonymous_mcp_call_is_401_not_a_redirect(self):
        cap = self._call("/mcp")
        self.assertEqual(cap.status, 401)
        self.assertEqual(self.passed, [])

    def test_anonymous_subpath_is_also_401(self):
        self.assertEqual(self._call("/mcp/").status, 401)

    def test_authorized_call_reaches_the_transport(self):
        cap = self._call("/mcp", [("authorization", "Bearer sk-test")])
        self.assertIsNone(cap.status)
        self.assertEqual(self.passed, ["/mcp"])

    def test_health_needs_no_token(self):
        self._call("/health")
        self.assertEqual(self.passed, ["/health"])


@unittest.skipUnless(HAS_MCP and HAS_STARLETTE, "mcp/starlette not installed in this interpreter")
class TestHttpApp(unittest.TestCase):
    def setUp(self):
        self.app = mcp.build_http_app(_cfg(), _Reg(), "127.0.0.1", 8085)

    def test_endpoint_answers_its_own_path_without_a_redirect(self):
        """MCP_PATH is routed, not mounted: a mount would bounce every request through a 307 to the
        trailing-slash path, doubling the round trips and hiding the 401 behind a redirect."""
        cap = _Capture()
        scope = {"type": "http", "method": "POST", "path": mcp.MCP_PATH, "headers": [],
                 "query_string": b"", "root_path": "", "app": self.app}
        asyncio.run(self.app(scope, cap.receive, cap.send))
        self.assertEqual(cap.status, 401)

    def test_health_reports_the_exposed_tool_count(self):
        cap = _Capture()
        scope = {"type": "http", "method": "GET", "path": "/health", "headers": [],
                 "query_string": b"", "root_path": "", "app": self.app}
        asyncio.run(self.app(scope, cap.receive, cap.send))
        self.assertEqual(cap.status, 200)
        self.assertEqual(json.loads(cap.body)["tools"], len(_Reg.tool_schemas))


class TestTransportSelection(unittest.TestCase):
    """`bob agent mcp` picks stdio unless the flag or agent.mcpTransport says http."""

    def _run(self, argv, transport="stdio"):
        import bob_core
        cfg = _cfg(mcpTransport=transport)
        with mock.patch.object(bob_core, "load_config", return_value=cfg), \
             mock.patch.object(mcp, "serve", return_value=0) as stdio, \
             mock.patch.object(mcp, "serve_http", return_value=0) as http:
            mcp.main(argv)
        return stdio, http

    def test_default_is_stdio(self):
        stdio, http = self._run([])
        stdio.assert_called_once()
        http.assert_not_called()

    def test_http_flag_wins(self):
        stdio, http = self._run(["--http", "--port", "9000", "--host", "0.0.0.0"])
        stdio.assert_not_called()
        self.assertEqual(http.call_args.kwargs, {"host": "0.0.0.0", "port": 9000})

    def test_config_transport_needs_no_flag(self):
        stdio, http = self._run([], transport="http")
        http.assert_called_once()
        stdio.assert_not_called()

    def test_stdio_flag_overrides_config(self):
        stdio, http = self._run(["--stdio"], transport="http")
        stdio.assert_called_once()
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
