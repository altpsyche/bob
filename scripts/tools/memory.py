"""Bob tool: memory_recall and memory_store via bob_memory.py."""
import sys
from pathlib import Path

_cfg: dict = {}


def enabled(config: dict) -> bool:
    """Feature gate (read by ToolRegistry): the memory tools are only offered to the agent when the
    memory feature is on. With memory.enabled=false they are NOT loaded, so the model can't recall or
    recite saved notes unprompted. Enable via memory.enabled in config/defaults.json (or user config)."""
    return bool(config.get("memory", {}).get("enabled", False))


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


# memory_store mutates the DB (SQLite write + best-effort dedup race). Marking it lets the loop
# serialize it within a parallel tool batch and default it to `ask`. memory_recall is read-only.
MUTATING_TOOLS = {"memory_store"}


def _run_ctx_attr(name):
    """Read a field off the current run's RunContext, or None. Lets the memory tools scope
    recall/store to the acting identity (owner) and project (scope) without changing tool signatures."""
    try:
        from tool_registry import get_run_context
        ctx = get_run_context()
        return getattr(ctx, name, None) if ctx else None
    except Exception:
        return None


def _memory_recall(query: str, k: int = 5) -> str:
    from bob_core import MEMORY_CONTEXT_FRAME, memory_recall
    out = memory_recall(query, k=k, config=_cfg, owner=_run_ctx_attr("owner"),
                        scope=_run_ctx_attr("scope"))
    if not out or out.strip() in ("", "(no results)"):
        return "(no saved notes match)"
    # Frame the results as context ABOUT THE USER (not Bob's own identity). One shared frame across
    # every memory surface (autoRecall, this tool, profile injection) — bob_core.MEMORY_CONTEXT_FRAME.
    return MEMORY_CONTEXT_FRAME + "\n" + out


def _memory_store(content: str, tags: str = "", type: str = "fact") -> str:
    from bob_core import memory_store
    return memory_store(content, tags=tags, mem_type=type, config=_cfg,
                        owner=_run_ctx_attr("owner"), scope=_run_ctx_attr("scope"))


def test() -> str:
    return _memory_recall("test query", k=2)


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": ("Recall the user's saved notes (their stated preferences, tools, and "
                            "projects) when the current request needs that context. Do NOT call this "
                            "for greetings, small talk, or general questions."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "k": {"type": "integer", "description": "Number of results (default 5)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "memory_store",
            "description": ("Save a note ABOUT THE USER (a preference or fact they've shared) for "
                            "future sessions. Only when the user shares something worth remembering."),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Text to store"},
                    "tags": {"type": "string", "description": "Comma-separated tags (optional)"},
                    "type": {
                        "type": "string",
                        "enum": ["profile", "preference", "project", "fact", "episodic"],
                        "description": ("Kind of memory (optional, default 'fact'): 'profile'/'preference' "
                                        "for durable identity, 'project' for repo-scoped, 'fact' general, "
                                        "'episodic' session recap."),
                    },
                },
                "required": ["content"],
            },
        },
    },
]

DISPATCH = {
    "memory_recall": _memory_recall,
    "memory_store": _memory_store,
}
