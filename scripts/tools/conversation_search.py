"""Bob tool: conversation_search — page earlier turns back into context. Semantic + keyword search over
the persisted run transcript (recall storage), the read side of conversation paging. Read-only; the
returned turns are a normal tool result, so they are themselves subject to compaction / tool-result
clearing and can't re-overflow the window. Gated by agent.conversationPaging (off by default)."""
import sys
from pathlib import Path

_cfg: dict = {}


def enabled(config: dict) -> bool:
    """Offered only when conversation paging is on AND memory is enabled (the transcript lives in the
    memory DB). Off by default."""
    return (bool(config.get("agent", {}).get("conversationPaging", False))
            and bool(config.get("memory", {}).get("enabled", False)))


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


def _run_ctx_attr(name):
    try:
        from tool_registry import get_run_context
        ctx = get_run_context()
        return getattr(ctx, name, None) if ctx else None
    except Exception:
        return None


def _conversation_search(query: str, k: int = 5) -> str:
    from bob_core import conversation_search
    return conversation_search(query, k=k, config=_cfg,
                               owner=_run_ctx_attr("owner"), scope=_run_ctx_attr("scope"))


def test() -> str:
    return _conversation_search("test query", k=2)


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "conversation_search",
            "description": ("Search earlier turns of this conversation that have scrolled out of your "
                            "current context (including past tool results). Use it to recover an earlier "
                            "decision, value, or detail you no longer see."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for in the earlier turns"},
                    "k": {"type": "integer", "description": "Number of turns to return (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
]

DISPATCH = {"conversation_search": _conversation_search}
