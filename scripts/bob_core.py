"""Shared Bob Python core: config, LLM client, memory access.

Import this in any Bob Python script instead of duplicating config loading
or calling bob_memory.py via subprocess.

Usage:
    from bob_core import load_config, get_llm_client, memory_recall, memory_store
"""
import json
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).parent.parent

# One neutral source of truth for the shared constants (ports + role table),
# read from config/defaults.json. No more hand-mirrored dicts. bob_config.py reads
# the same file's "runtime" section.
_DEFAULTS_FILE = REPO / "config" / "defaults.json"
_defaults_cache: Optional[dict] = None


def load_defaults() -> dict:
    """Load and cache config/defaults.json (the neutral shared-constants file).

    Raises RuntimeError with a clear message if the file is missing or lacks the required
    top-level keys — a dropped key fails loudly at import rather than resolving to None.
    """
    global _defaults_cache
    if _defaults_cache is None:
        if not _DEFAULTS_FILE.exists():
            raise RuntimeError(
                f"config/defaults.json not found at {_DEFAULTS_FILE}\n"
                "This file is the neutral single source of truth for ports + roles."
            )
        data = json.loads(_DEFAULTS_FILE.read_text(encoding="utf-8"))
        for key in ("ports", "roleTable"):
            if key not in data or not isinstance(data[key], dict):
                raise RuntimeError(f"config/defaults.json missing required '{key}' section")
        _defaults_cache = data
    return _defaults_cache


# Single source of truth for service-port defaults on the Python side, loaded from
# config/defaults.json rather than a mirrored literal. The resolved runtime config
# normally carries these; this dict is the only literal fallback, read via _port().
_PORT_DEFAULTS = load_defaults()["ports"]

# The memory-config defaults live once in config/defaults.json.runtime.memory;
# the per-key `.get(key, LITERAL)` fallbacks below read from here instead of re-inlining the literal, so
# a default is defined in exactly one place. (`memory.enabled` keeps an explicit fail-CLOSED False at its
# call site — that is a deliberate safety default when no memory config exists at all, not a mirror.)
_MEM_DEFAULTS = load_defaults().get("runtime", {}).get("memory", {})


def _mem(mem: dict, key: str):
    """A memory-config value from the caller's config, defaulting to the single-sourced neutral default
    (config/defaults.json.runtime.memory) rather than a re-inlined literal."""
    return mem.get(key, _MEM_DEFAULTS.get(key))


def _port(config: dict, name: str) -> int:
    """Resolve a service port from config, falling back to the one central default dict."""
    if name not in _PORT_DEFAULTS:
        raise KeyError(f"unknown port key '{name}'; known: {', '.join(_PORT_DEFAULTS)}")
    return int(config.get(name, _PORT_DEFAULTS[name]))


def get_role(config: dict, task: str = "chat", pro: bool = False) -> str:
    """Resolve a model role from config for a task.

    task: chat | code | think | voice | vision | agent
    pro:  prefer the *-pro variant where one exists.
    Centralizes the routing lookup so the plugins don't each re-derive it. The task->key
    mapping and fallback literals live in config/defaults.json roleTable, not inline here.
    """
    table = load_defaults()["roleTable"]
    entry = table.get(task) or table["chat"]
    # vision routing lives in its own config section, not under routing.
    section = config.get(entry.get("section", "routing"), {})
    base_key, pro_key = entry["base"], entry["pro"]
    if pro:
        return section.get(pro_key) or section.get(base_key) or entry["proFallback"]
    return section.get(base_key) or entry["fallback"]


def load_config() -> dict:
    """Resolve the merged runtime config in Python from the neutral sources — on EVERY OS.

    Config resolves the same way on every platform: config/defaults.json + config/user.json
    (the documented override), via bob_config. The runtime never *requires* `bob gen`.
    """
    import bob_config  # local import: avoids a cycle (bob_config imports bob_core)

    return bob_config.resolve_runtime_config()


def _litellm_key(config: dict) -> str:
    """Return the LiteLLM master key, resolved through the secret seam: env -> keychain
    -> data/secrets.json -> the config value (sk-local default). On Windows with no env/secret set
    this is unchanged (the config value wins as the default)."""
    import osenv

    return osenv.secret("litellmKey", default=config.get("litellmKey", "sk-local"), config=config)


def get_llm_client(config: Optional[dict] = None):
    """Return an OpenAI client pointed at the LiteLLM proxy."""
    from openai import OpenAI

    cfg = config or load_config()
    port = _port(cfg, "litellmPort")
    return OpenAI(base_url=f"http://localhost:{port}/v1", api_key=_litellm_key(cfg))


def check_litellm(config: Optional[dict] = None) -> bool:
    """Return True if the LiteLLM proxy port is open (TCP connect; avoids slow /health backend checks)."""
    import socket

    cfg = config or load_config()
    port = _port(cfg, "litellmPort")
    try:
        with socket.create_connection(("localhost", port), timeout=3):
            return True
    except OSError:
        return False


def capability_probe(config: Optional[dict] = None) -> tuple:
    """A startup readiness check. Returns (ok, message). The runtime's
    only hard needs are (a) a resolvable config (always true here — load_config resolves in Python)
    and (b) a reachable OpenAI-compatible endpoint. Callers print the
    message and degrade rather than assuming a provisioner ran."""
    cfg = config or load_config()
    port = _port(cfg, "litellmPort")
    if check_litellm(cfg):
        return (True, f"OK — LiteLLM endpoint reachable on :{port}.")
    return (
        False,
        f"LiteLLM endpoint not reachable on :{port}. Start the inference stack (`bob serve` on "
        "Windows) or point litellmPort at any running OpenAI-compatible endpoint (see docs/PORTABILITY.md).",
    )


def _get_db_path(config: Optional[dict] = None) -> str:
    cfg = config or load_config()
    rel = _mem(cfg.get("memory", {}), "dbPath")
    return str(REPO / rel.replace("\\", "/"))


def project_key(cwd: Optional[str] = None, config: Optional[dict] = None) -> Optional[str]:
    """The project scope key for a directory: the git repo root if inside one, else the
    directory itself. Returns None when memory.scopeByProject is off (→ everything global). Pure
    Python (no git subprocess, per CONTRIBUTING): walk up looking for a `.git` entry."""
    cfg = config or load_config()
    if not _mem(cfg.get("memory", {}), "scopeByProject"):
        return None
    start = (Path(cwd).resolve() if cwd else Path.cwd())
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return str(d)
    return str(start)


# One shared frame for every surface that feeds saved memory into the model — per-turn autoRecall
# (bob_loop), the memory_recall tool (tools/memory), and the once-per-session profile block.
# A single phrasing means the model never sees three variants of "this is about the user, not you".
MEMORY_CONTEXT_FRAME = (
    "Notes about the user (context only — about the user, not your own identity; "
    "use only if relevant, do not recite verbatim):"
)


def memory_store(content: str, tags: str = "", mem_type: str = "fact",
                 owner: Optional[str] = None, scope: Optional[str] = None,
                 salience: float = 1.0, config: Optional[dict] = None) -> str:
    """Store content in bob.db directly (no subprocess). Threads type/tags/owner/scope/salience and
    the configured dedup threshold through to the typed write path. `owner` defaults to
    agent.defaultOwner; the real per-run owner/scope are threaded from RunContext. Only
    type='project' facts are scoped to the project; identity/prefs/facts stay global."""
    cfg = config or load_config()
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    import bob_memory  # type: ignore

    mem = cfg.get("memory", {})
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    row_scope = scope if mem_type == "project" else None
    mid, is_new = bob_memory.store(
        content, db_path=db_path, mem_type=mem_type, owner=owner, scope=row_scope,
        tags=(tags or None), salience=salience, dedup_threshold=float(_mem(mem, "dedupThreshold")),
    )
    return f"Stored (id={mid}): {content[:80]}" if is_new else f"Already stored (similar id={mid})"


def memory_recall(query: str, k: int = 5, config: Optional[dict] = None,
                  owner: Optional[str] = None, scope: Optional[str] = None) -> str:
    """Recall top-k results from bob.db. Returns newline-joined content strings. Threads the
    configured blended-ranking threshold/weights and owner/scope through to the read path.
    `owner` defaults to agent.defaultOwner; the real per-run owner is threaded in from RunContext."""
    cfg = config or load_config()
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    import bob_memory  # type: ignore

    mem = cfg.get("memory", {})
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    ranking = mem.get("ranking") or {}
    results = bob_memory.recall(
        query, k=k, db_path=db_path,
        threshold=float(_mem(mem, "recallThreshold")),
        owner=owner, scope=scope,
        weights=ranking, type_weights=mem.get("typeWeights"),
        half_lives=ranking.get("halfLifeDays"),
        # Hybrid recall (dense + BM25/FTS5 + RRF). Default 'dense' is the dense-only path.
        retrieval=_mem(mem, "retrieval"), rrf_k=int(_mem(mem, "rrfK")),
    )
    if not results:
        return "(no results)"
    return "\n".join(r["content"] for r in results)


def memory_profile_block(owner: Optional[str] = None, config: Optional[dict] = None) -> Optional[str]:
    """The once-per-session stable-profile block (framed), or None. Gated on memory.enabled
    AND memory.injectProfileAtStart. Owner defaults to agent.defaultOwner; the real
    per-run owner. Best-effort: any failure (missing deps, embed server down for the DB open) yields
    None so a session never fails to start over memory."""
    cfg = config or load_config()
    mem = cfg.get("memory", {})
    if not mem.get("enabled", False) or not _mem(mem, "injectProfileAtStart"):  # enabled: fail-closed
        return None
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    max_tokens = int(_mem(mem, "profileMaxTokens"))
    try:
        import bob_memory  # type: ignore
        body = bob_memory.profile_block(owner, db_path, max_chars=max_tokens * 4)
    except Exception:
        return None
    if not body:
        return None
    return MEMORY_CONTEXT_FRAME + "\n" + body


def _project_memory_files(project_dir: str) -> list:
    """Ordered broad→specific: user-level BOB.md, then this project's AGENTS.md / .bob/BOB.md / BOB.md
    (Claude Code-style load order — the more specific file is read last so it reads as most salient)."""
    root = Path(project_dir)
    return [
        Path.home() / ".bob" / "BOB.md",     # user-level, all projects (broad)
        root / "AGENTS.md",                  # cross-agent standard, if present
        root / ".bob" / "BOB.md",
        root / "BOB.md",                     # project (specific)
    ]


def project_memory_block(project_dir: Optional[str], config: Optional[dict] = None) -> Optional[str]:
    """Concatenated, framed project instruction file(s) for `project_dir` (the git root/cwd),
    or None. Human-curated + git-committable (Claude Code CLAUDE.md analogue). Gated on
    memory.projectFiles; capped at memory.bobMdMaxTokens. Best-effort: unreadable files are skipped."""
    cfg = config or load_config()
    mem = cfg.get("memory", {})
    if not _mem(mem, "projectFiles") or not project_dir:
        return None
    parts = []
    for p in _project_memory_files(project_dir):
        try:
            txt = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if txt:
            parts.append(txt)
    if not parts:
        return None
    body = "\n\n".join(parts)[: int(_mem(mem, "bobMdMaxTokens")) * 4]
    return "Project instructions (from BOB.md — follow these for this project):\n" + body


def budget_injection(blocks: list, max_tokens: int) -> tuple:
    """Fit optional injected-memory blocks into ~max_tokens (≈4 chars/token) before they are
    concatenated into the system prompt. `blocks` is a list of (label, text, priority); higher
    priority is kept longer. Greedy by priority desc; the single highest-priority block is always kept
    even if it alone exceeds the budget (so we never inject nothing when a large BOB.md is present).
    Trim order therefore drops autoRecall before profile before BOB.md. Returns
    (joined_text, kept_labels, dropped_labels)."""
    max_chars = max(0, int(max_tokens) * 4)
    ordered = sorted([b for b in blocks if b[1] and b[1].strip()], key=lambda b: -b[2])
    kept, dropped, used = [], [], 0
    for label, text, _prio in ordered:
        need = len(text) + 2   # +2 for the blank-line separator
        if not kept or used + need <= max_chars:
            kept.append((label, text))
            used += need
        else:
            dropped.append(label)
    joined = "\n\n".join(text for _label, text in kept)
    return joined, [label for label, _ in kept], dropped


def consolidate_session(turns: list, config: Optional[dict] = None,
                        owner: Optional[str] = None, scope: Optional[str] = None,
                        session_id: Optional[str] = None) -> dict:
    """Extract durable facts from a session's turns and store them (deduped), plus one
    episodic recap. Resolves db path, summarizer model, owner, and dedup threshold from config, then
    calls the importable core. `scope` tags extracted type='project' facts; `session_id`
    stamps each stored row's provenance. Best-effort: returns {'facts': 0, 'summary': None}
    on any failure. The CALLER gates on memory.enabled && memory.autoConsolidate."""
    cfg = config or load_config()
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    mem = cfg.get("memory", {})
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    model = cfg.get("routing", {}).get("defaultRole", "chat")
    try:
        import bob_memory  # type: ignore
        result = bob_memory.consolidate_session(
            turns, db_path=db_path, model=model, owner=owner, scope=scope,
            dedup_threshold=float(_mem(mem, "dedupThreshold")),
            timeout=int(_mem(mem, "consolidateTimeout")),   # bound end-of-session stall
            reconcile_top_k=int(_mem(mem, "reconcileTopK")),  # existing-facts window
            max_tokens=int(_mem(mem, "maxSummaryTokens")),   # clears the reasoning-token budget
            source_session=session_id,                          # provenance stamp
        )
        # Opportunistic hygiene at end of consolidation: TTL prune + per-owner size cap.
        try:
            bob_memory.prune(db_path, owner=owner, forget_after_days=mem.get("forgetAfterDays"),
                             max_rows=int(_mem(mem, "maxRows")))
        except Exception:
            pass
        return result
    except Exception:
        return {"facts": 0, "summary": None}


def _ensure_memory_importable() -> None:
    scripts_dir = str(REPO / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
