#!/usr/bin/env python3
"""Bob MCP server (N10) — exposes Bob's registry tools over the Model Context Protocol.

Thin seam: it reuses the same ToolRegistry the agent loop uses, so every Bob tool (file, git,
web, memory, ...) becomes an MCP tool with no per-tool wiring. Gated behind agent.mcpEnabled.
Start:  bob agent mcp        (stdio transport)

The MCP wire protocol is handled by the `mcp` package when installed; the tool-exposure + dispatch
seam below (build_mcp_tools / dispatch) is import-light and unit-tested (tests/test_mcp.py) without
the package or a live transport. Inherits the same tool hardening as the agent (N9 file/git limits,
web_fetch SSRF guard, shell fail-closed)."""
import json
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "scripts" / "tools"))


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


def serve(config: dict = None) -> int:
    """Start the MCP stdio server (requires the `mcp` package). Returns a process exit code.
    Refuses unless agent.mcpEnabled is set."""
    from bob_core import load_config
    config = config or load_config()
    if not config.get("agent", {}).get("mcpEnabled", False):
        print("MCP disabled — set agent.mcpEnabled = true in config/user.json to enable.", file=sys.stderr)
        return 1
    try:
        from mcp.server import Server            # type: ignore
        from mcp.server.stdio import stdio_server  # type: ignore
        import mcp.types as types                 # type: ignore
    except ImportError:
        print("The 'mcp' package is not installed. Run: tools\\venv-litellm\\Scripts\\pip install mcp",
              file=sys.stderr)
        return 1

    registry = _build_registry(config)
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

    import anyio

    async def _run():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    print(f"Bob MCP server (stdio) — {len(registry.tool_schemas)} tools exposed", file=sys.stderr)
    anyio.run(_run)
    return 0


def main():
    raise SystemExit(serve())


if __name__ == "__main__":
    main()
