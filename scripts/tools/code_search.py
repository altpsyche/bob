"""Bob tool: code_search -- navigate the codebase by symbol instead of grepping or reading whole files.

Offered to the agent only when agent.repoMap is on. Read-only (no MUTATING_TOOLS). Backed by bob_repomap
over the agent.allowedReadPaths roots, so it never touches a denied/secret path. Three actions: locate a
symbol's definition(s), list files that reference it, and render a ranked repo map for a query.
"""
import sys
from pathlib import Path

_scripts_dir = str(Path(__file__).parent.parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import bob_repomap  # noqa: E402

_roots: list = []
_map_tokens: int = 1024
_semantic: bool = False
_repo = None


def enabled(config: dict) -> bool:
    """Loaded only when the repo map feature is on (agent.repoMap). Default off -> the tool is absent."""
    return bool(config.get("agent", {}).get("repoMap", False))


def configure(config: dict) -> None:
    global _roots, _map_tokens, _semantic, _repo
    agent = config.get("agent", {})
    raw = agent.get("allowedReadPaths", [])
    if isinstance(raw, str):
        raw = [raw]
    _roots = [p for p in raw if p]
    _map_tokens = int(agent.get("repoMapTokens", 1024))
    _semantic = bool(agent.get("codeSearchSemantic", False))
    _repo = bob_repomap.RepoMap(_roots) if _roots else None


def _code_search(action: str = "map", symbol: str = "", query: str = "") -> str:
    if _repo is None:
        return "code_search: no allowedReadPaths configured"
    if action == "def":
        if not symbol:
            return "code_search: 'def' needs a symbol"
        hits = _repo.definitions(symbol)
        if not hits:
            return f"No definition found for '{symbol}'."
        return "\n".join(f"{_repo._rel(Path(f))}:{ln}" for f, ln in hits)
    if action == "refs":
        if not symbol:
            return "code_search: 'refs' needs a symbol"
        hits = _repo.references(symbol)
        if not hits:
            return f"No references found for '{symbol}'."
        return "\n".join(_repo._rel(Path(f)) for f in hits)
    if action == "semantic":
        if not _semantic:
            return "code_search: semantic mode is off (set agent.codeSearchSemantic and run 'bob code index')"
        q = query or symbol
        if not q:
            return "code_search: 'semantic' needs a query"
        try:
            hits = bob_repomap.search_semantic(q, _roots)
        except Exception as e:
            return f"code_search: semantic search unavailable ({e}). Is the embed server up and the index built?"
        if not hits:
            return "No semantic matches (has 'bob code index' been run?)."
        return "\n\n".join(f"[{h.get('score', 0):.2f}] {h.get('content', '')}" for h in hits)
    # default: ranked map for the query
    out = _repo.render_map(query=query or symbol, token_budget=_map_tokens)
    return out or "(repo map empty -- no indexable source files under allowedReadPaths)"


TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "code_search",
            "description": (
                "Navigate the codebase by symbol. action='def' finds where a symbol is defined, "
                "action='refs' lists files that reference it, action='map' returns a ranked map of the "
                "most relevant files and their definitions for a query. Prefer this over grepping or "
                "reading whole files to locate code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["def", "refs", "map", "semantic"],
                               "description": "def | refs | map (default) | semantic (needs the index)"},
                    "symbol": {"type": "string", "description": "Symbol name for def/refs"},
                    "query": {"type": "string", "description": "Query text for map ranking"},
                },
                "required": [],
            },
        },
    },
]

DISPATCH = {"code_search": _code_search}
