#!/usr/bin/env python3
"""Bob MCP client (O7) — connect to configured MCP servers and expose THEIR tools as Bob tools.

The client analog of the N10 server (bob_mcp_server.py). Where the server exposes Bob's registry over
MCP, the client reaches OUT to other MCP servers (agent.mcpServers), lists their tools, and registers
each as a synthetic ToolRegistry entry namespaced ``mcp:<server>:<tool>`` — so the agent calls a remote
tool with the exact same dispatch path (and thus the same M7 truncation/retention, O6 policy, N3 timeout)
as a local one, with no per-tool wiring.

Same seam discipline as N10: the MCP wire protocol (the ``mcp`` package + a live transport) is touched
ONLY by ``_RealConnection`` when a server is actually configured. The seam below — name mapping, schema
translation, result flattening, dispatch routing, and the fail-closed skip — is import-light and unit
-tested (tests/test_mcp_client.py) against a fake in-process connector, no package or transport needed.

Gated by ``agent.mcpServers`` (default ``{}``): with none configured, ``register_mcp_tools`` is a no-op
and the toolset is byte-identical to pre-O7. A server that fails to connect is logged and skipped
(loud-fail) — one bad server never crashes startup or blocks the others.

Config shape (``config/defaults.json`` -> ``runtime.agent.mcpServers``; name -> spec)::

    "mcpServers": {
      "fs":   { "transport": "stdio", "cmd": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] },
      "docs": { "transport": "sse",   "url": "http://127.0.0.1:9000/sse" }
    }

Remote tools default to ``ask`` in the O6 policy (reaching an external server is a side effect worth a
prompt); a configured policy can promote them to ``allow`` or ``deny`` per tool/owner/depth.
"""
import json
import sys

MCP_PREFIX = "mcp:"


# ---------------------------------------------------------------------------
# Name mapping — the namespace that keeps remote tools distinct from local ones
# ---------------------------------------------------------------------------

def make_name(server: str, tool: str) -> str:
    """Namespaced tool name the model sees: mcp:<server>:<tool>."""
    return f"{MCP_PREFIX}{server}:{tool}"


def split_name(name: str) -> tuple:
    """Inverse of make_name -> (server, tool). Tolerates a tool name containing ':' (splits once)."""
    rest = name[len(MCP_PREFIX):] if name.startswith(MCP_PREFIX) else name
    server, _, tool = rest.partition(":")
    return server, tool


def is_remote(name: str) -> bool:
    return name.startswith(MCP_PREFIX)


# ---------------------------------------------------------------------------
# Translation — MCP tool descriptors <-> Bob's OpenAI-style tool_schemas
# ---------------------------------------------------------------------------

def tool_schemas_for(server: str, mcp_tools: list) -> list:
    """Map a server's MCP tool descriptors [{name, description, inputSchema}] to Bob OpenAI-style
    tool_schemas with namespaced names (the mirror of N10's build_mcp_tools, inverted)."""
    schemas = []
    for t in mcp_tools or []:
        tname = t.get("name")
        if not tname:
            continue
        schemas.append({
            "type": "function",
            "function": {
                "name": make_name(server, tname),
                "description": t.get("description") or f"MCP tool '{tname}' on server '{server}'.",
                "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return schemas


def result_to_text(result) -> str:
    """Flatten an MCP call result to the plain string Bob's dispatch expects. Handles a bare string, a
    list of content blocks, or a CallToolResult / dict carrying a ``content`` list (TextContent.text)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if content is None:
        content = result if isinstance(result, list) else [result]
    parts = []
    for block in content:
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        parts.append(text if text is not None else str(block))
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# The client — holds one connection per server and routes calls
# ---------------------------------------------------------------------------

class McpClient:
    """Owns the live connections to configured MCP servers and routes namespaced tool calls to them.
    A connection is any object with ``list_tools() -> [descriptor]``, ``call(tool, args, timeout)`` and
    ``close()`` — the real transport (_RealConnection) or a test fake."""

    def __init__(self, timeout: int = 30):
        self._conns: dict = {}
        self.schemas: list = []
        self._timeout = timeout

    def add(self, server: str, conn) -> None:
        self._conns[server] = conn

    def servers(self) -> list:
        return list(self._conns.keys())

    def call_tool(self, server: str, tool: str, arguments: dict, timeout: int = None) -> str:
        """Round-trip one remote call. Never raises — returns a model-readable string on any failure so
        a flaky remote server can't crash the agent step (the same contract as dispatch_call)."""
        conn = self._conns.get(server)
        if conn is None:
            return f"MCP server '{server}' is not connected."
        try:
            raw = conn.call(tool, arguments or {}, timeout if timeout is not None else self._timeout)
        except Exception as e:
            return f"MCP tool error ({make_name(server, tool)}): {e}"
        return result_to_text(raw)

    def close(self) -> None:
        for conn in self._conns.values():
            try:
                conn.close()
            except Exception:
                pass
        self._conns.clear()


def _make_dispatch(client: McpClient, server: str, tool: str, timeout: int):
    """Build the fn registered under the namespaced tool name. ToolRegistry.dispatch_call invokes it as
    ``fn(**parsed_arguments)``, so it takes **arguments and forwards them to the remote round-trip."""
    def _call(**arguments):
        return client.call_tool(server, tool, arguments, timeout)
    return _call


# ---------------------------------------------------------------------------
# Wiring — connect configured servers, register their tools into a ToolRegistry
# ---------------------------------------------------------------------------

def connect_servers(config: dict, connector=None) -> McpClient:
    """Connect to every configured server and collect its tool schemas. A server that fails to connect
    or list is logged and skipped (loud-fail) so one bad server never blocks the others or startup.
    ``connector(spec, timeout) -> connection`` is injectable for tests (defaults to the real transport)."""
    connector = connector or _open_connection
    agent_cfg = (config or {}).get("agent", {}) or {}
    servers = agent_cfg.get("mcpServers", {}) or {}
    timeout = int(agent_cfg.get("mcpTimeout", 30))
    client = McpClient(timeout=timeout)
    for name, spec in servers.items():
        try:
            conn = connector(spec, timeout)
            tools = conn.list_tools()
        except Exception as e:
            print(f"[warn] MCP server '{name}' failed to connect/list: {e} — skipping", file=sys.stderr)
            continue
        client.add(name, conn)
        client.schemas.extend(tool_schemas_for(name, tools))
    return client


def register_mcp_tools(registry, config: dict, connector=None, quiet: bool = False):
    """Register configured MCP servers' tools into ``registry`` as synthetic entries. No servers
    configured -> no-op (byte-identical toolset). The connected client is stashed on the registry
    (``_mcp_client``) so its connections stay alive for the registry's lifetime. Returns the registry."""
    agent_cfg = (config or {}).get("agent", {}) or {}
    servers = agent_cfg.get("mcpServers", {}) or {}
    if not servers:
        return registry

    client = connect_servers(config, connector=connector)
    timeout = int(agent_cfg.get("mcpTimeout", 30))
    for schema in client.schemas:
        name = schema["function"]["name"]
        server, tool = split_name(name)
        registry.tool_schemas.append(schema)
        registry.dispatch[name] = _make_dispatch(client, server, tool, timeout)
        registry.remote_tools.add(name)

    if client.schemas:
        registry._mcp_client = client   # keep the connections from being GC'd
    if not quiet and client.schemas:
        print(f"[bob] mcp: {len(client.schemas)} tool(s) from {len(client.servers())} server(s)",
              file=sys.stderr)
    return registry


# ---------------------------------------------------------------------------
# Real transport — the ONLY part that touches the `mcp` package / a live server.
# Not unit-tested (the seam above is, via a fake connector); smoke-validated against
# a live MCP server through `bob agent serve`.
# ---------------------------------------------------------------------------

def _open_connection(spec: dict, timeout: int):
    """Open a live MCP connection for one server spec (transport stdio|sse). Deferred so the import-light
    seam never pulls in the `mcp` package unless a server is actually configured."""
    return _RealConnection(spec, timeout)


class _RealConnection:
    """Sync adapter over the async `mcp` ClientSession. Runs the session on a dedicated background
    event-loop thread and marshals each sync call onto it via run_coroutine_threadsafe, so the
    synchronous agent loop can drive an inherently-async MCP client."""

    def __init__(self, spec: dict, timeout: int):
        import threading
        self._spec = spec
        self._timeout = timeout
        self._loop = None
        self._session = None
        self._stop = None            # asyncio.Event, created on the loop
        self._ready = threading.Event()
        self._err = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="bob-mcp")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError(f"MCP server did not initialize within {timeout}s")
        if self._err:
            raise self._err

    def _run(self):
        import asyncio
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        finally:
            loop.close()

    async def _serve(self):
        import asyncio
        self._stop = asyncio.Event()
        try:
            async with self._transport_ctx() as (read, write):
                from mcp import ClientSession  # type: ignore
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as e:              # surface a connect/init failure to __init__
            self._err = e
            self._ready.set()

    def _transport_ctx(self):
        transport = (self._spec.get("transport") or "stdio").lower()
        if transport == "stdio":
            from mcp import StdioServerParameters             # type: ignore
            from mcp.client.stdio import stdio_client         # type: ignore
            return stdio_client(StdioServerParameters(
                command=self._spec["cmd"], args=self._spec.get("args", []),
                env=self._spec.get("env") or None))
        if transport in ("sse", "http"):
            from mcp.client.sse import sse_client             # type: ignore
            return sse_client(self._spec["url"])
        raise ValueError(f"unknown MCP transport: {transport!r}")

    def _submit(self, coro, timeout=None):
        import asyncio
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout if timeout is not None else self._timeout)

    def list_tools(self) -> list:
        res = self._submit(self._session.list_tools())
        out = []
        for t in getattr(res, "tools", []) or []:
            out.append({
                "name": t.name,
                "description": getattr(t, "description", "") or "",
                "inputSchema": getattr(t, "inputSchema", None) or {"type": "object", "properties": {}},
            })
        return out

    def call(self, tool: str, arguments: dict, timeout: int = None):
        return self._submit(self._session.call_tool(tool, arguments or {}), timeout)

    def close(self) -> None:
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
