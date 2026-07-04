"""Bob tool: read_result — page back a tool result that was truncated-and-retained (M7/O3) or
O15-cleared from the transcript.

Gated on agent.clearToolResults (default off) so the default tool set is byte-identical to pre-O15.
When on, the loop may replace an old bulky tool-result message with a compact stub pointing at a
retained handle (rN); this tool lets the model re-fetch the full text on demand instead of losing it.
Reaches the run's ToolRegistry (which owns the retention store) via the NE0 RunContext seam, so its
fn signature stays plain."""


def enabled(config: dict) -> bool:
    """Feature gate (read by ToolRegistry): only offered when context-editing is on, so with
    clearToolResults=false the model never sees this tool and the default toolset is unchanged."""
    return bool(config.get("agent", {}).get("clearToolResults", False))


def configure(config: dict) -> None:
    pass


def _read_result(handle: str, offset: int = 0, length: int = 4000) -> str:
    from tool_registry import get_run_context

    ctx = get_run_context()
    reg = getattr(ctx, "registry", None) if ctx else None
    if reg is None or not hasattr(reg, "read_result"):
        return "read_result is unavailable in this context."
    try:
        return reg.read_result(str(handle), int(offset), int(length))
    except (TypeError, ValueError):
        return "read_result: offset and length must be integers."


def test() -> str:
    # Outside a dispatched call there's no RunContext — exercises the graceful no-context path.
    return _read_result("r0")


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "read_result",
            "description": ("Re-fetch the full text of an earlier tool result that was truncated or "
                            "cleared from the conversation, using its handle (e.g. 'r3' shown in a "
                            "'[tool result r3 cleared … read_result(\\'r3\\')]' or '…retained as r3]' "
                            "note). Call this only when you actually need that earlier content again."),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string", "description": "Result handle, e.g. 'r3'"},
                    "offset": {"type": "integer", "description": "Start char offset (default 0)"},
                    "length": {"type": "integer", "description": "Max chars to return (default 4000)"},
                },
                "required": ["handle"],
            },
        },
    },
]

DISPATCH = {"read_result": _read_result}
