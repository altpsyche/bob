"""Bob tool: file_read and file_write with path allowlist + secrets denylist enforcement.

The path/secret policy lives in bob_fsguard (shared with file_edit and the code index). The thin
wrappers below pass this module's `_home()` so tests can patch it to exercise ~/.ssh denial in a
temp tree."""
from pathlib import Path

import bob_fsguard

_allowed_read: list = []
_allowed_write: list = []


def _home() -> Path:
    """User home dir — overridable in tests so ~/.ssh denial can be exercised in a temp tree."""
    return Path.home()


def _abs(path: str, allowed: list) -> Path:
    return bob_fsguard.abs_path(path, allowed)


def _is_allowed(target: Path, allowed: list) -> bool:
    return bob_fsguard.is_allowed(target, allowed)


def _is_denied_secret(target: Path) -> bool:
    """True for sensitive files that must never be read or written even inside an allowed root."""
    return bob_fsguard.is_denied_secret(target, home=_home())


def configure(config: dict) -> None:
    global _allowed_read, _allowed_write
    agent = config.get("agent", {})

    raw_r = agent.get("allowedReadPaths", [])
    if isinstance(raw_r, str):
        raw_r = [raw_r]
    _allowed_read = [Path(p) for p in raw_r if p]

    raw_w = agent.get("allowedWritePaths", [])
    if isinstance(raw_w, str):
        raw_w = [raw_w]
    _allowed_write = [Path(p) for p in raw_w if p]


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def _file_list(path: str = ".") -> str:
    """List the entries of a directory within allowedReadPaths (directories first, then files with
    sizes). This is the tool for "what files are here" — file_read is for a single file's contents."""
    if not _allowed_read:
        return "file_list: no allowedReadPaths configured"
    p = _abs(path, _allowed_read)
    if not _is_allowed(p, _allowed_read):
        allowed_str = ", ".join(str(a) for a in _allowed_read)
        return f"Access denied: {path}\nAllowed paths: {allowed_str}"
    try:
        rp = p.resolve()
    except Exception:
        return f"Access denied: {path}"
    if not rp.exists():
        return f"Not found: {path}"
    if not rp.is_dir():
        return f"Not a directory: {path} (use file_read to read a file's contents)"
    try:
        entries = sorted(rp.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except OSError as e:
        return f"Error listing {path}: {e}"
    if not entries:
        return f"(empty directory: {rp})"
    lines = [f"{rp}  —  {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"]
    for e in entries[:500]:
        if e.is_dir():
            lines.append(f"  {e.name}/")
        else:
            try:
                lines.append(f"  {e.name}  ({_human(e.stat().st_size)})")
            except OSError:
                lines.append(f"  {e.name}")
    if len(entries) > 500:
        lines.append(f"  … and {len(entries) - 500} more")
    return "\n".join(lines)


def _file_read(path: str) -> str:
    p = _abs(path, _allowed_read)
    if not _allowed_read:
        return "file_read: no allowedReadPaths configured"
    if not _is_allowed(p, _allowed_read):
        allowed_str = ", ".join(str(a) for a in _allowed_read)
        return f"Access denied: {path}\nAllowed paths: {allowed_str}"
    if _is_denied_secret(p):
        return f"Access denied (sensitive file): {path}"
    if not p.exists():
        return f"File not found: {path}"
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > 6000:
            content = content[:6000] + f"\n... (truncated, {len(content)} chars total)"
        return content
    except Exception as e:
        return f"Error reading {path}: {e}"


def _file_write(path: str, content: str) -> str:
    if not _allowed_write:
        return (
            "file_write is disabled.\n"
            "Add paths to agent.allowedWritePaths in config/user.json to enable."
        )
    p = _abs(path, _allowed_write)
    if not _is_allowed(p, _allowed_write):
        allowed_str = ", ".join(str(a) for a in _allowed_write)
        return f"Access denied: {path}\nAllowed write paths: {allowed_str}"
    if _is_denied_secret(p):
        return f"Access denied (sensitive file): {path}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written: {path} ({len(content)} chars)"
    except Exception as e:
        return f"Error writing {path}: {e}"


def test() -> str:
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "bob_file_tool_test.txt"
    tmp.write_text("test content", encoding="utf-8")
    result = f"file_read test skipped (path not in allowedReadPaths)\nAllowed: {_allowed_read}"
    for allowed in _allowed_read:
        if tmp.resolve().is_relative_to(allowed.resolve()):
            result = _file_read(str(tmp))
            break
    tmp.unlink(missing_ok=True)
    return result


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "file_list",
            "description": (
                "List the files and subfolders in a directory (use this for 'what files are here', "
                "not file_read). Relative paths are relative to the workspace root. Only paths within "
                "allowedReadPaths are accessible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Directory path (default '.', the workspace root)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read the contents of a file. Only paths within allowedReadPaths are accessible.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_write",
            "description": (
                "Write content to a file. "
                "Disabled by default — requires allowedWritePaths to be configured."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
]

DISPATCH = {
    "file_list": _file_list,
    "file_read": _file_read,
    "file_write": _file_write,
}
