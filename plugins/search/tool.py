"""Bob plugin tool: search_code — search files with ripgrep, synthesise via LLM."""
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO))

import bob_fsguard  # noqa: E402
from plugins.search.invoke import run_rg, synthesise  # noqa: E402

_cfg: dict = {}
_allowed_read: list = []


def _home() -> Path:
    """User home dir, overridable in tests so ~/.ssh denial can be exercised in a temp tree."""
    return Path.home()


def configure(config: dict) -> None:
    global _cfg, _allowed_read
    _cfg = config
    raw = (config or {}).get("agent", {}).get("allowedReadPaths", [])
    if isinstance(raw, str):
        raw = [raw]
    _allowed_read = [Path(p) for p in raw if p]


def _denied(p: Path) -> bool:
    """A file the agent must not see: outside allowedReadPaths or on the secrets denylist."""
    return (not bob_fsguard.is_allowed(p, _allowed_read)
            or bob_fsguard.is_denied_secret(p, home=_home()))


def _search_code(query: str, path: str = ".", ext: str = None) -> str:
    """Search under `path`, which goes through the same allowlist + secrets denylist as file_read;
    matches inside denied files are dropped from the result."""
    if not _allowed_read:
        return "search_code: no allowedReadPaths configured"
    target = bob_fsguard.abs_path(path or ".", _allowed_read)
    if not bob_fsguard.is_allowed(target, _allowed_read):
        allowed_str = ", ".join(str(a) for a in _allowed_read)
        return f"Access denied: {path}\nAllowed paths: {allowed_str}"
    if bob_fsguard.is_denied_secret(target, home=_home()):
        return f"Access denied (sensitive path): {path}"
    matches = run_rg(query, str(target.resolve()), ext, deny=_denied)
    if matches.startswith("("):
        return matches
    from bob_core import check_litellm
    if not check_litellm(_cfg):
        return matches
    return synthesise(query, matches, _cfg)


def test() -> str:
    import shutil
    rg_available = "ripgrep available" if shutil.which("rg") else "ripgrep not found (findstr/grep fallback active)"
    return f"search_code: OK, {rg_available} (CLI: bob search \"<query>\" --raw)"


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Search files in a directory using ripgrep, then synthesise the results via LLM. "
                "Use for searching local code, configs, or text files — NOT for web/internet searches. "
                "Returns an LLM-analysed summary of the matches with file names and line numbers."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search term or pattern. E.g. 'load_config', 'TODO', 'API endpoints'.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search (relative paths resolve against the workspace root). Only paths within allowedReadPaths are searchable.",
                    },
                    "ext": {
                        "type": "string",
                        "description": "File extension filter. E.g. '.py', '.ts', '.md'. Omit to search all files.",
                    },
                },
                "required": ["query"],
            },
        },
    }
]

DISPATCH = {
    "search_code": _search_code,
}
