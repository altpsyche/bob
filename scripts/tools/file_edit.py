"""Bob tool: file_edit -- apply precise partial edits (search/replace or whole-file) via bob_edit.

Enabled when agent.allowedWritePaths is configured (the same switch as file_write), and declared mutating
so it defaults to `ask` and is serialized within a parallel tool batch. Editing goes through the same
path allowlist + secrets denylist as file_write. Exports PREVIEW so the approval flow can render the diff.
"""
import sys
from pathlib import Path

_scripts_dir = str(Path(__file__).parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import bob_edit  # noqa: E402

_allowed_write: list = []
_edit_format: str = "search-replace"


def _write_paths(config: dict) -> list:
    raw = config.get("agent", {}).get("allowedWritePaths", [])
    if isinstance(raw, str):
        raw = [raw]
    return [Path(p) for p in raw if p]


def enabled(config: dict) -> bool:
    """Offered to the agent only when writing is enabled. With no allowedWritePaths the tool is not
    loaded, so behavior is unchanged until a user opts in (same gate as file_write)."""
    return bool(_write_paths(config))


def configure(config: dict) -> None:
    global _allowed_write, _edit_format
    _allowed_write = _write_paths(config)
    _edit_format = config.get("agent", {}).get("editFormat", "search-replace")


# file_edit mutates the working tree. Marking it declares the `ask` default + batch serialization; note
# file_write is not registered, so file_edit is the safer edit surface.
MUTATING_TOOLS = {"file_edit"}


def _render_preview(args: dict) -> str:
    """Rendered unified diff of a proposed edit, for the approval prompt. Shared renderer in bob_edit."""
    res = bob_edit.preview_edits(args, _allowed_write)
    if not res.ok:
        return res.message
    return res.diff or "(no textual change)"


PREVIEW = {"file_edit": _render_preview}


def _affected_paths(args: dict) -> list:
    """The file path(s) a file_edit call will touch, for pre-mutation checkpointing (Q4)."""
    try:
        return [bob_edit.bob_fsguard.abs_path(spec["path"], _allowed_write)
                for spec in bob_edit.normalize_edits(args)]
    except Exception:
        return []


AFFECTS = {"file_edit": _affected_paths}


def _file_edit(**kwargs) -> str:
    """Apply an edit. Accepts a single-file form (path + edits[] or content) or a multi-file form
    (files[]). Returns the applied summary, or a correctable rejection beginning with EDIT REJECTED."""
    res = bob_edit.apply_edits(kwargs, _allowed_write)
    return res.message


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "file_edit",
            "description": (
                "Apply a precise partial edit to one or more files using search/replace blocks. "
                "For each file, give 'path' and 'edits' (a list of {search, replace}); the search text "
                "must match the current file content exactly and uniquely. To create a file or replace it "
                "whole, give 'content' instead of 'edits'. Edit several files at once with 'files' (a list "
                "of per-file objects); if any hunk fails, nothing is written. Only paths within "
                "allowedWritePaths are editable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File to edit (single-file form)"},
                    "edits": {
                        "type": "array",
                        "description": "Search/replace hunks for the single file",
                        "items": {
                            "type": "object",
                            "properties": {
                                "search": {"type": "string",
                                           "description": "Exact current text to find (unique in the file)"},
                                "replace": {"type": "string", "description": "Replacement text"},
                            },
                            "required": ["search", "replace"],
                        },
                    },
                    "content": {"type": "string",
                                "description": "Whole-file content (creates or replaces the file)"},
                    "diff": {"type": "string",
                             "description": "A unified diff for the single file (applied by content, not line number)"},
                    "files": {
                        "type": "array",
                        "description": "Multi-file edit: a list of per-file objects (path + edits or content)",
                        "items": {"type": "object"},
                    },
                },
                "required": [],
            },
        },
    },
]

DISPATCH = {"file_edit": _file_edit}
