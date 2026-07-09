"""Repo map: a compact, ranked, code-aware picture of the codebase.

Extracts symbol definitions and references per file, builds a referencer->definer graph, and ranks files
by a personalized PageRank (files named in the query/chat are boosted) -- so a token-bounded map surfaces
the code most connected to what you are working on, the way Aider's repo map does.

Extraction has two backends behind one interface: a dependency-free regex extractor (the default, always
available) and an optional tree-sitter backend (added separately, lazy-imported). Everything is guarded by
bob_fsguard, so a denied/secret path is never indexed, and only files under the read allowlist are walked.
The per-file tag cache is keyed by (mtime, size) so only changed files are re-parsed.
"""
import re
from pathlib import Path

import bob_fsguard

# Source extensions worth mapping (kept small and mainstream; unknown extensions are skipped, not failed).
SOURCE_EXT = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".c", ".h", ".cpp",
              ".hpp", ".cs"}

# Definition patterns per language family. Each captures the symbol name in group 1. Regex is coarse by
# design -- the tree-sitter backend supersedes it when available.
_DEF_PATTERNS = {
    "py": [re.compile(r"^\s*def\s+([A-Za-z_]\w*)"), re.compile(r"^\s*class\s+([A-Za-z_]\w*)")],
    "js": [re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)"),
           re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_]\w*)"),
           re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=")],
    "go": [re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"),
           re.compile(r"^\s*type\s+([A-Za-z_]\w*)")],
    "rs": [re.compile(r"^\s*(?:pub\s+)?fn\s+([A-Za-z_]\w*)"),
           re.compile(r"^\s*(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)")],
    "c": [re.compile(r"^[A-Za-z_][\w\s\*]*\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*\{?\s*$")],
    "generic": [re.compile(r"^\s*(?:def|class|function|func|fn|type|struct)\s+([A-Za-z_]\w*)")],
}

_EXT_LANG = {".py": "py", ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".go": "go",
             ".rs": "rs", ".c": "c", ".h": "c", ".cpp": "c", ".hpp": "c",
             ".java": "generic", ".rb": "generic", ".cs": "generic"}

_IDENT_RE = re.compile(r"[A-Za-z_]\w*")

# Very common tokens that carry no navigational signal -- excluded from reference edges.
_STOPWORDS = {"if", "else", "for", "while", "return", "def", "class", "function", "const", "let", "var",
              "import", "from", "self", "this", "true", "false", "null", "none", "int", "str", "bool",
              "func", "type", "struct", "enum", "trait", "pub", "fn", "in", "and", "or", "not", "is",
              "new", "public", "private", "static", "void", "the", "a", "an"}


def _lang(path: str) -> str:
    return _EXT_LANG.get(Path(path).suffix.lower(), "generic")


def extract_tags_regex(path: str, source: str) -> dict:
    """Regex backend: return {'defs': [(name, line)], 'refs': set(names)} for one file's source."""
    lang = _lang(path)
    patterns = _DEF_PATTERNS.get(lang, _DEF_PATTERNS["generic"])
    defs, def_names = [], set()
    for i, line in enumerate(source.split("\n"), start=1):
        for pat in patterns:
            m = pat.match(line)
            if m:
                defs.append((m.group(1), i))
                def_names.add(m.group(1))
                break
    refs = {t for t in _IDENT_RE.findall(source) if t not in _STOPWORDS and len(t) > 1}
    refs -= def_names   # a file's own defs are not references to elsewhere
    return {"defs": defs, "refs": refs}


def extract_tags_treesitter(path: str, source: str) -> dict:
    """Optional tree-sitter backend via grep_ast (which wraps tree-sitter-language-pack and ships the
    .scm def/ref tag queries). Raises ImportError/other when grep_ast or a grammar is unavailable, so
    callers fall back to the regex extractor. Not installed by default -- `pip install grep_ast` enables it.
    """
    from grep_ast import filename_to_lang           # type: ignore
    from grep_ast.tsl import get_language, get_parser  # type: ignore

    lang_name = filename_to_lang(path)
    if not lang_name:
        raise ValueError(f"no tree-sitter grammar for {path}")
    language = get_language(lang_name)
    parser = get_parser(lang_name)
    tree = parser.parse(bytes(source, "utf-8"))
    # grep_ast bundles a tags query per language under its queries/ dir.
    from grep_ast import get_scm_fname               # type: ignore
    scm = get_scm_fname(lang_name)
    query = language.query(Path(scm).read_text(encoding="utf-8"))
    defs, refs = [], set()
    for node, tag in query.captures(tree.root_node):
        text = node.text.decode("utf-8", "replace")
        if tag.startswith("name.definition"):
            defs.append((text, node.start_point[0] + 1))
        elif tag.startswith("name.reference"):
            refs.add(text)
    def_names = {n for n, _ in defs}
    refs = {r for r in refs if r not in def_names and r not in _STOPWORDS and len(r) > 1}
    return {"defs": defs, "refs": refs}


def treesitter_available() -> bool:
    """True if the optional grep_ast/tree-sitter stack is importable."""
    try:
        import grep_ast  # noqa: F401
        return True
    except Exception:
        return False


def default_extractor():
    """Return the best available per-file extractor: tree-sitter when grep_ast is importable, else the
    dependency-free regex extractor. The tree-sitter path falls back to regex per-file on any failure."""
    if not treesitter_available():
        return extract_tags_regex

    def _hybrid(path, source):
        try:
            return extract_tags_treesitter(path, source)
        except Exception:
            return extract_tags_regex(path, source)

    return _hybrid


class RepoMap:
    """Builds and queries the symbol graph over the read-allowed, non-secret files under `roots`."""

    def __init__(self, roots, home=None, extractor=None, max_file_bytes=400_000):
        self.roots = [Path(r) for r in roots if r]
        self.home = home
        self.extractor = extractor or default_extractor()
        self.max_file_bytes = max_file_bytes
        self._cache = {}      # path -> {"key": (mtime, size), "tags": {...}}

    # --- file discovery ---------------------------------------------------------------------------
    def _indexable(self, p: Path) -> bool:
        if p.suffix.lower() not in SOURCE_EXT:
            return False
        if not bob_fsguard.is_allowed(p, self.roots):
            return False
        if bob_fsguard.is_denied_secret(p, home=self.home):
            return False
        return True

    def iter_files(self):
        seen = set()
        for root in self.roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file() or p in seen:
                    continue
                if any(part in {".git", "node_modules", "__pycache__", ".venv", "venv"}
                       for part in p.parts):
                    continue
                if self._indexable(p):
                    seen.add(p)
                    yield p

    # --- tag extraction with an mtime/size cache --------------------------------------------------
    def tags_for(self, p: Path) -> dict:
        try:
            st = p.stat()
            key = (st.st_mtime, st.st_size)
        except OSError:
            return {"defs": [], "refs": set()}
        hit = self._cache.get(str(p))
        if hit and hit["key"] == key:
            return hit["tags"]
        if key[1] > self.max_file_bytes:
            tags = {"defs": [], "refs": set()}
        else:
            try:
                src = p.read_text(encoding="utf-8", errors="replace")
                tags = self.extractor(str(p), src)
            except OSError:
                tags = {"defs": [], "refs": set()}
        self._cache[str(p)] = {"key": key, "tags": tags}
        return tags

    def build(self):
        """Populate per-file tags for every indexable file (uses the cache; only changed files reparse)."""
        files = list(self.iter_files())
        for p in files:
            self.tags_for(p)
        return files

    # --- graph + ranking --------------------------------------------------------------------------
    def _graph(self, files):
        """defs_by_name: symbol -> set(file); edges: (referencer, definer) -> weight."""
        tags = {str(p): self.tags_for(p) for p in files}
        defs_by_name = {}
        for f, t in tags.items():
            for name, _line in t["defs"]:
                defs_by_name.setdefault(name, set()).add(f)
        edges = {}
        for f, t in tags.items():
            for ref in t["refs"]:
                for definer in defs_by_name.get(ref, ()):
                    if definer == f:
                        continue
                    edges[(f, definer)] = edges.get((f, definer), 0) + 1
        return tags, defs_by_name, edges

    def _pagerank(self, files, edges, personalization, damping=0.85, iters=30):
        nodes = [str(p) for p in files]
        if not nodes:
            return {}
        n = len(nodes)
        # personalization vector, normalized (falls back to uniform when nothing is named)
        pers = {f: max(0.0, personalization.get(f, 0.0)) for f in nodes}
        tot = sum(pers.values())
        pers = {f: (v / tot if tot else 1.0 / n) for f, v in pers.items()}
        out_w = {}
        for (src, _dst), w in edges.items():
            out_w[src] = out_w.get(src, 0) + w
        rank = {f: 1.0 / n for f in nodes}
        for _ in range(iters):
            nxt = {f: (1 - damping) * pers[f] for f in nodes}
            for (src, dst), w in edges.items():
                if out_w.get(src):
                    nxt[dst] += damping * rank[src] * (w / out_w[src])
            # redistribute dangling mass by personalization so it doesn't leak away
            dangling = damping * sum(rank[f] for f in nodes if not out_w.get(f))
            for f in nodes:
                nxt[f] += dangling * pers[f]
            rank = nxt
        return rank

    def _personalization(self, files, query):
        """Boost files named in the query and files defining identifiers named in the query."""
        toks = {t.lower() for t in _IDENT_RE.findall(query or "")}
        pers = {}
        if not toks:
            return pers
        for p in files:
            f = str(p)
            score = 0.0
            if any(tok in p.name.lower() for tok in toks):
                score += 5.0
            for name, _line in self.tags_for(p)["defs"]:
                if name.lower() in toks:
                    score += 3.0
            if score:
                pers[f] = score
        return pers

    # --- public queries ---------------------------------------------------------------------------
    def definitions(self, symbol: str):
        """(file, line) pairs where `symbol` is defined."""
        out = []
        for p in self.build():
            for name, line in self.tags_for(p)["defs"]:
                if name == symbol:
                    out.append((str(p), line))
        return out

    def references(self, symbol: str):
        """Files that reference `symbol` (by the coarse identifier set)."""
        out = []
        for p in self.build():
            if symbol in self.tags_for(p)["refs"]:
                out.append(str(p))
        return out

    def ranked_files(self, query: str = ""):
        files = self.build()
        _tags, _defs, edges = self._graph(files)
        pers = self._personalization(files, query)
        rank = self._pagerank(files, edges, pers)
        return sorted(files, key=lambda p: rank.get(str(p), 0.0), reverse=True)

    def render_map(self, query: str = "", token_budget: int = 1024, exclude=()):
        """A token-bounded map: top-ranked files with their definitions. `exclude` drops files already in
        context. The budget is hit by binary search over how many files to include (real token count)."""
        exclude = {str(Path(e)) for e in exclude}
        ranked = [p for p in self.ranked_files(query) if str(p) not in exclude]
        ranked = [p for p in ranked if self.tags_for(p)["defs"]]
        if not ranked:
            return ""

        def render(k):
            lines = []
            for p in ranked[:k]:
                rel = self._rel(p)
                syms = ", ".join(f"{n}:{ln}" for n, ln in self.tags_for(p)["defs"][:40])
                lines.append(f"{rel}:\n  {syms}")
            return "\n".join(lines)

        # binary search the largest k whose rendered token estimate fits the budget
        lo, hi, best = 1, len(ranked), ""
        while lo <= hi:
            mid = (lo + hi) // 2
            text = render(mid)
            if _est_tokens(text) <= token_budget:
                best = text
                lo = mid + 1
            else:
                hi = mid - 1
        return best or render(1)

    def _rel(self, p: Path) -> str:
        for root in self.roots:
            try:
                return str(p.resolve().relative_to(root.resolve()))
            except ValueError:
                continue
        return str(p)


def _est_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) -- enough to bound the map without a tokenizer dep."""
    return max(1, len(text) // 4)


# --- Mode B: semantic code index over Module R's retrieval stack ----------------------------------
# Reuse bob_memory's store/recall (contextual-chunk embeddings + hybrid recall + rerank) rather than
# building a parallel index. Code chunks live in a SEPARATE db file under a synthetic owner + a project
# scope, so they never mix with (or evict) the user's own memories in bob.db.
CODE_OWNER = "__code__"
CODE_TYPE = "code"
CODE_DB = Path(__file__).parent.parent / "data" / "code.db"
_CHUNK_LINES = 40


def _project_scope(roots) -> str:
    """A stable per-repo scope id so multiple indexed repos stay separated in the shared code db."""
    if not roots:
        return "default"
    return Path(roots[0]).resolve().name or "default"


def index_semantic(repo: "RepoMap", db_path: Path = None, scope: str = None,
                   embed_optional: bool = False) -> int:
    """Embed one chunk per definition (a window of source starting at the def) into the code db, each
    carrying a situating `context` line. Returns the number of chunks stored. Calls bob_memory.store
    directly because the bob_core wrapper drops context=/scope."""
    import bob_memory
    db_path = db_path or CODE_DB
    scope = scope or _project_scope(repo.roots)
    stored = 0
    for p in repo.build():
        rel = repo._rel(p)
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
        except OSError:
            continue
        for name, line in repo.tags_for(p)["defs"]:
            chunk = "\n".join(lines[line - 1: line - 1 + _CHUNK_LINES]).strip()
            if not chunk:
                continue
            context = f"File {rel} - symbol {name}"
            bob_memory.store(chunk, db_path, source="repomap", mem_type=CODE_TYPE,
                             owner=CODE_OWNER, scope=scope, context=context,
                             embed_optional=embed_optional)
            stored += 1
    return stored


def search_semantic(query: str, roots, db_path: Path = None, scope: str = None, k: int = 8,
                    rerank: bool = False, rerank_url: str = None) -> list:
    """Hybrid dense+BM25 recall over the code index, scoped to this repo's code chunks only."""
    import bob_memory
    db_path = db_path or CODE_DB
    scope = scope or _project_scope([Path(r) for r in roots if r])
    return bob_memory.recall(query, db_path, owner=CODE_OWNER, scope=scope, type_filter=CODE_TYPE,
                             retrieval="hybrid", rerank=rerank, rerank_url=rerank_url, k=k)
