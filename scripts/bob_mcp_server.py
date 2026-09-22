#!/usr/bin/env python3
"""Bob MCP server — exposes Bob's registry tools over the Model Context Protocol.

Thin seam: it reuses the same ToolRegistry the agent loop uses, so every Bob tool (file, git,
web, memory, ...) becomes an MCP tool with no per-tool wiring. Gated behind agent.mcpEnabled.
Start:  bob agent mcp             (stdio transport, one harness on this machine)
        bob agent mcp --http      (Streamable HTTP, many clients, remote-capable)

Two transports, one tool surface. stdio is the co-located default: the harness spawns Bob as a child
process. Streamable HTTP is the reach transport — a harness on another machine (or several harnesses at
once) can hold sessions against one running Bob, which stdio cannot do because it is one process per
client. The HTTP transport is authenticated with the SAME static bearer tokens as the agent API (one
map in bob_authstore) and carries DNS-rebinding protection, because unlike stdio it is reachable.

The MCP wire protocol is handled by the `mcp` package when installed; the tool-exposure + dispatch
seam below (build_mcp_tools / dispatch) and the HTTP auth + allowed-host policy (authorize /
allowed_hosts) are import-light and unit-tested (tests/test_mcp.py) without the package or a live
transport. Inherits the same tool hardening as the agent (file/git limits, web_fetch SSRF guard,
shell fail-closed)."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))

MCP_PATH = "/mcp"   # the Streamable HTTP endpoint clients POST to (and GET for the SSE stream)


def build_mcp_tools(registry) -> list:
    """Map the registry's OpenAI-style tool_schemas to MCP tool descriptors
    {name, description, inputSchema} — the 'list tools' half of the seam."""
    tools = []
    for schema in registry.tool_schemas:
        fn = schema.get("function", schema)
        tools.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "inputSchema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return tools


def dispatch(registry, name: str, arguments: dict) -> str:
    """Run an MCP tool call through the registry (same validated path the agent uses) — the
    'call tool' half of the seam. registry.dispatch_call never raises; it returns a string."""
    return registry.dispatch_call(name, json.dumps(arguments or {}))


def _build_registry(config: dict):
    from tool_registry import ToolRegistry
    agent = config.get("agent", {})
    disabled_raw = agent.get("disabledTools", [])
    disabled = set(disabled_raw) if isinstance(disabled_raw, list) else {
        t.strip() for t in disabled_raw.split(",") if t.strip()
    }
    return ToolRegistry.build(config, disabled)


def _build_server(registry):
    """The `mcp` Server with Bob's two handlers bound. Shared by both transports so stdio and HTTP
    can never expose a different tool surface."""
    from mcp.server import Server  # type: ignore
    import mcp.types as types      # type: ignore

    server = Server("bob")

    @server.list_tools()
    async def _list_tools():
        return [
            types.Tool(name=t["name"], description=t["description"], inputSchema=t["inputSchema"])
            for t in build_mcp_tools(registry)
        ]

    @server.call_tool()
    async def _call_tool(name, arguments):
        return [types.TextContent(type="text", text=dispatch(registry, name, arguments))]

    return server


def _enabled(config: dict) -> bool:
    return bool(config.get("agent", {}).get("mcpEnabled", False))


_DISABLED_MSG = "MCP disabled — set agent.mcpEnabled = true in config/user.json to enable."
_NO_PACKAGE_MSG = ("The 'mcp' package is not installed. Run: tools/venv-litellm/bin/pip install mcp "
                   r"(Windows: tools\venv-litellm\Scripts\pip install mcp)")


# --- HTTP transport policy (import-light, unit-tested without a live server) ----------------------

def accepted_tokens(config: dict) -> set:
    """The bearer tokens the HTTP transport accepts: exactly the agent API's static config tokens
    (the litellm key + agent.apiTokens), resolved by the one map in bob_authstore."""
    from bob_authstore import config_token_owners

    return set(config_token_owners(config))


def authorize(config: dict, authorization: str) -> bool:
    """True when `authorization` carries an accepted bearer token. The MCP HTTP endpoint is reachable
    (that is its point), so it is closed by default: no header, a non-Bearer scheme, or an unknown
    token all fail. stdio needs none of this — it is a child process of the client that spawned it."""
    header = authorization or ""
    if not header.startswith("Bearer "):
        return False
    token = header[7:].strip()
    return bool(token) and token in accepted_tokens(config)


def allowed_hosts(config: dict, host: str, port: int) -> list:
    """Host header values the HTTP transport accepts, for DNS-rebinding protection: the loopback names
    and the configured bind host, each bare and with the port, plus any agent.mcpAllowedHosts entries
    (that is how you name the LAN address or DNS name a REMOTE harness will use — the bind host alone
    is '0.0.0.0', which no client ever sends)."""
    names = ["localhost", "127.0.0.1", "[::1]"]
    if host and host not in ("0.0.0.0", "::"):  # noqa: S104 — comparison, not a bind
        names.append(host)
    out = []
    for n in names:
        for v in (n, f"{n}:{port}"):
            if v not in out:
                out.append(v)
    for extra in config.get("agent", {}).get("mcpAllowedHosts", []) or []:
        if extra and extra not in out:
            out.append(extra)
    return out


def _security_settings(config: dict, host: str, port: int):
    from mcp.server.transport_security import TransportSecuritySettings  # type: ignore

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts(config, host, port),
        allowed_origins=list(config.get("agent", {}).get("mcpAllowedOrigins", []) or []),
    )


class BearerGate:
    """Pure-ASGI bearer gate in front of MCP_PATH. It wraps the WHOLE app rather than sitting inside the
    mounted endpoint so an unauthenticated request is refused before routing — a mount would answer
    `/mcp` with a 307 to `/mcp/` first, handing an anonymous caller a redirect instead of a 401. Pure
    ASGI (not BaseHTTPMiddleware) because the transport streams SSE, which BaseHTTPMiddleware buffers.
    /health stays open so a supervisor can probe the port without a token."""

    def __init__(self, app, config: dict, path: str = MCP_PATH):
        self.app, self.config, self.path = app, config, path

    def _guarded(self, scope) -> bool:
        p = scope.get("path", "")
        return scope.get("type") == "http" and (p == self.path or p.startswith(self.path + "/"))

    async def __call__(self, scope, receive, send):
        if self._guarded(scope):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if not authorize(self.config, headers.get("authorization", "")):
                from starlette.responses import JSONResponse
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_http_app(config: dict, registry, host: str, port: int):
    """The ASGI app: bearer-authenticated Streamable HTTP at MCP_PATH, plus an unauthenticated /health
    so a supervisor can probe the port without a token."""
    import contextlib

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager  # type: ignore
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    manager = StreamableHTTPSessionManager(
        app=_build_server(registry),
        json_response=False,          # SSE streaming, so long tool calls report progress
        stateless=False,              # sessions, so a client can resume a stream
        security_settings=_security_settings(config, host, port),
    )

    class _Transport:
        """A Route endpoint that is an ASGI app, not a request/response function: the transport owns
        the raw stream (SSE), and a class instance is how Starlette tells the two apart. Routed rather
        than mounted so MCP_PATH itself answers — a mount would bounce every single request through a
        307 to the trailing-slash path."""

        async def __call__(self, scope, receive, send):
            await manager.handle_request(scope, receive, send)

    async def _health(_request):
        return JSONResponse({"status": "ok", "tools": len(registry.tool_schemas), "transport": "http"})

    @contextlib.asynccontextmanager
    async def _lifespan(_app):
        async with manager.run():     # owns the session task group for the app's lifetime
            yield

    inner = Starlette(routes=[Route("/health", _health),
                              Route(MCP_PATH, _Transport(), methods=["GET", "POST", "DELETE"])],
                      lifespan=_lifespan)
    return BearerGate(inner, config)


# --- entry points ---------------------------------------------------------------------------------

def serve(config: dict = None) -> int:
    """Start the MCP stdio server (requires the `mcp` package). Returns a process exit code.
    Refuses unless agent.mcpEnabled is set."""
    from bob_core import load_config
    config = config or load_config()
    if not _enabled(config):
        print(_DISABLED_MSG, file=sys.stderr)
        return 1
    try:
        from mcp.server.stdio import stdio_server  # type: ignore
    except ImportError:
        print(_NO_PACKAGE_MSG, file=sys.stderr)
        return 1

    registry = _build_registry(config)
    server = _build_server(registry)

    import anyio

    async def _run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    print(f"Bob MCP server (stdio) — {len(registry.tool_schemas)} tools exposed", file=sys.stderr)
    anyio.run(_run)
    return 0


def serve_http(config: dict = None, host: str = None, port: int = None) -> int:
    """Start the MCP Streamable HTTP server. Returns a process exit code. Refuses unless
    agent.mcpEnabled is set, exactly like stdio."""
    from bob_core import _port, load_config
    config = config or load_config()
    if not _enabled(config):
        print(_DISABLED_MSG, file=sys.stderr)
        return 1
    agent = config.get("agent", {})
    host = host or agent.get("mcpHost", "127.0.0.1")
    port = int(port or _port(agent, "mcpPort"))
    try:
        import uvicorn
        app = build_http_app(config, _build_registry(config), host, port)
    except ImportError as e:
        print(f"{_NO_PACKAGE_MSG}  ({e})", file=sys.stderr)
        return 1

    print(f"Bob MCP server (Streamable HTTP) on {host}:{port}{MCP_PATH}  (Authorization: Bearer <token>)",
          file=sys.stderr)
    if host == "0.0.0.0":  # noqa: S104 — comparison, not a bind
        print("  WARNING: bound to 0.0.0.0 (LAN-exposed). Every Bob tool is reachable to any holder of a "
              "token, so issue a dedicated agent.apiTokens entry rather than sharing the litellm key, and "
              "list the address remote clients use in agent.mcpAllowedHosts.", file=sys.stderr)
    uvicorn.run(app, host=host, port=port)
    return 0


def main(argv=None) -> int:
    """`bob agent mcp [--http|--stdio] [--host H] [--port N]`. Transport defaults to
    agent.mcpTransport (stdio unless configured otherwise), so a box that runs Bob for a remote
    harness needs no flag."""
    argv = list(sys.argv[1:] if argv is None else argv)

    def _opt(name):
        if name in argv:
            i = argv.index(name)
            return argv[i + 1] if i + 1 < len(argv) else None
        return None

    from bob_core import load_config
    config = load_config()
    transport = (config.get("agent", {}).get("mcpTransport") or "stdio").lower()
    if "--http" in argv:
        transport = "http"
    if "--stdio" in argv:
        transport = "stdio"
    if transport != "http":
        return serve(config)
    port = _opt("--port")
    return serve_http(config, host=_opt("--host"), port=int(port) if port else None)


if __name__ == "__main__":
    raise SystemExit(main())
