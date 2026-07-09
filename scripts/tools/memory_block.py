"""Bob tool: memory_block — the agent curates its own always-injected core-memory blocks
(MemGPT/Letta core memory). Edit-only (append/replace); reading is unnecessary because the blocks are
already injected into every turn's context. Logic lives in bob_core.memory_block_edit."""
import sys
from pathlib import Path

_cfg: dict = {}


def enabled(config: dict) -> bool:
    """Offered to the agent only when memory is on AND at least one core-memory block is configured
    (memory.coreBlocks, a name -> char-cap map). Off by default (empty map)."""
    mem = config.get("memory", {})
    return bool(mem.get("enabled", False)) and bool(mem.get("coreBlocks"))


def configure(config: dict) -> None:
    global _cfg
    _cfg = config
    scripts_dir = str(Path(__file__).parent.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)


# Editing a block writes the DB and reshapes the always-injected prefix — mutating, so the loop
# serializes it within a parallel batch and defaults it to `ask`.
MUTATING_TOOLS = {"memory_block"}


def _run_ctx_attr(name):
    """Read a field off the current run's RunContext (owner/scope), or None — so a block is scoped to
    the acting identity/project exactly as it is when injected."""
    try:
        from tool_registry import get_run_context
        ctx = get_run_context()
        return getattr(ctx, name, None) if ctx else None
    except Exception:
        return None


def _memory_block(action: str, name: str, content: str = "") -> str:
    from bob_core import memory_block_edit
    return memory_block_edit(action, name, content, config=_cfg,
                             owner=_run_ctx_attr("owner"), scope=_run_ctx_attr("scope"))


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "memory_block",
            "description": ("Update one of your core-memory blocks — short notes you keep in context "
                            "across turns (e.g. the current task or key facts about the user). Use "
                            "'append' to add a line, 'replace' to rewrite the whole block. The blocks "
                            "are always shown to you, so you never need to read them back."),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["append", "replace"],
                               "description": "append a line, or replace the block's whole content"},
                    "name": {"type": "string", "description": "which block to edit"},
                    "content": {"type": "string", "description": "text to append or the new content"},
                },
                "required": ["action", "name", "content"],
            },
        },
    },
]

DISPATCH = {"memory_block": _memory_block}
