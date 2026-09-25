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


def service_port(config: dict, key: str) -> int:
    """THE port accessor for a service port key: reads it from the same config section the service itself
    reads (agent.agentPort / agent.mcpPort for the agent + MCP servers, top-level for the rest), falling back
    to config/defaults.json. bob_config places agentPort/mcpPort under `agent`, which is why those two are
    looked up there."""
    section = _SECTION_PORTS.get(key)
    if section:
        return _port((config or {}).get(section) or {}, key)
    return _port(config or {}, key)


# Port keys that live under a config section rather than at the top level (see bob_config).
_SECTION_PORTS = {"agentPort": "agent", "mcpPort": "agent"}


# --- token estimation ---------------------------------------------------------------------------
# One estimator for every budget in the runtime (history, injected memory, tool results, repo map):
# ~4 chars per token for English + JSON. No tokenizer dependency.
_CHARS_PER_TOKEN = 4


def est_tokens(text) -> int:
    """Rough token estimate of `text` (~4 chars/token). Empty -> 0."""
    if not text:
        return 0
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN


def tokens_to_chars(tokens) -> int:
    """The character budget matching `tokens` under est_tokens' ratio (never negative)."""
    return max(0, int(tokens) * _CHARS_PER_TOKEN)


def get_role(config: dict, task: str = "chat", pro: bool = False) -> str:
    """Resolve a model role from config for a task.

    task: a config/defaults.json roleTable key (chat | voice | code | ponder | writer | agent | vision).
    pro:  prefer the *-pro variant where one exists.
    Centralizes the routing lookup so the plugins don't each re-derive it. The task->key
    mapping and fallback literals live in config/defaults.json roleTable, not inline here. An unknown
    task raises ValueError: a typo must not silently route to the chat model.
    """
    table = load_defaults()["roleTable"]
    entry = table.get(task)
    if entry is None:
        raise ValueError(f"unknown routing task '{task}'; known: {', '.join(table)}")
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


# The environment variable the LiteLLM proxy reads its master key from (litellm.yaml says
# `master_key: os.environ/LITELLM_MASTER_KEY`); stack passes it to every proxy it starts.
LITELLM_KEY_ENV = "LITELLM_MASTER_KEY"


def _litellm_key(config: dict) -> str:
    """THE LiteLLM master key, for every client and every server that checks it. Precedence: the process
    env (litellmKey / BOB_LITELLMKEY), then an explicit `litellmKey` the user set in config/user.json,
    then the stored secret (OS keychain, <data_dir>/secrets.json), else a random key generated once and
    kept there (osenv.ensure_secret). There is no fixed default: a well-known key would open the proxy,
    the agent API and MCP to anyone who can reach them."""
    import os

    import osenv

    env = os.environ.get("litellmKey") or os.environ.get("BOB_LITELLMKEY")
    if env:
        return env
    explicit = (config or {}).get("litellmKey")
    if explicit:
        return str(explicit)
    return osenv.ensure_secret("litellmKey", nbytes=24, prefix="sk-bob-")


def get_llm_client(config: Optional[dict] = None):
    """Return an OpenAI client pointed at the LiteLLM proxy."""
    from openai import OpenAI

    cfg = config or load_config()
    port = _port(cfg, "litellmPort")
    return OpenAI(base_url=f"http://localhost:{port}/v1", api_key=_litellm_key(cfg))


def check_litellm(config: Optional[dict] = None) -> bool:
    """Return True if the LiteLLM proxy port is open (TCP connect; avoids slow /health backend checks)."""
    import osenv

    cfg = config or load_config()
    return osenv.is_port_in_use(_port(cfg, "litellmPort"))


def litellm_key_rejected(config: Optional[dict] = None) -> bool:
    """True when the proxy on litellmPort answers GET /v1/models with 401/403 for Bob's key: it is running
    with another master key (started before the key changed). Anything else, including no answer, is
    False, so a slow or absent proxy is never mistaken for a key mismatch."""
    import urllib.error
    import urllib.request

    cfg = config or load_config()
    req = urllib.request.Request(f"http://127.0.0.1:{_port(cfg, 'litellmPort')}/v1/models",
                                 headers={"Authorization": f"Bearer {_litellm_key(cfg)}"})
    try:
        urllib.request.urlopen(req, timeout=5).close()  # noqa: S310 (loopback only)
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except (urllib.error.URLError, OSError, ValueError):
        return False
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


def state_path(path: str) -> Path:
    """Resolve a configured state path (DB, log). Absolute paths stand; a relative one resolves under the
    state directories rather than the checkout: `logs/...` under osenv.cache_dir(), anything else (with or
    without a leading `data/`) under osenv.data_dir(). Both default to the repo's data/ and logs/, and
    move with BOB_DATA_DIR."""
    import osenv

    p = Path(str(path).replace("\\", "/"))
    if p.is_absolute():
        return p
    parts = p.parts
    if parts and parts[0] == "logs":
        return osenv.cache_dir().joinpath(*parts[1:])
    if parts and parts[0] == "data":
        return osenv.data_dir().joinpath(*parts[1:])
    return osenv.data_dir() / p


def session_db_path(config: dict) -> Path:
    """THE session DB path (agent.sessionDbPath), shared by the agent server, the shell and the token store."""
    return state_path((config or {}).get("agent", {}).get("sessionDbPath") or "data/sessions.db")


def _get_db_path(config: Optional[dict] = None) -> str:
    cfg = config or load_config()
    return str(state_path(_mem(cfg.get("memory", {}), "dbPath")))


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


# One shared frame for the agent-editable core-memory blocks (MemGPT/Letta), injected alongside the
# recalled notes through the single budget_injection seam.
CORE_BLOCKS_FRAME = (
    "Your core memory blocks (you maintain these across turns; edit with the memory_block tool):"
)


def _core_block_caps(config: dict) -> dict:
    """name -> char cap for the configured core-memory blocks. Empty dict == the feature is off."""
    return (config.get("memory", {}) or {}).get("coreBlocks") or {}


def memory_block_edit(action: str, name: str, content: str = "",
                      owner: Optional[str] = None, scope: Optional[str] = None,
                      config: Optional[dict] = None) -> str:
    """Append to / replace a named core-memory block. Only names declared in memory.coreBlocks
    (name -> char cap) are editable; the cap keeps a block from growing the prefix unboundedly (oldest
    chars trimmed). Owner/scope come from RunContext so a block is scoped to the acting identity/project,
    matching how it's injected."""
    cfg = config or load_config()
    caps = _core_block_caps(cfg)
    if name not in caps:
        known = ", ".join(sorted(caps)) or "(none configured)"
        return f"Unknown core-memory block '{name}'. Configured blocks: {known}."
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    import bob_memory  # type: ignore
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    current = bob_memory.block_get(name, db_path, owner=owner, scope=scope) or ""
    if action == "append":
        new = (current + "\n" + content).strip() if current else content.strip()
    elif action == "replace":
        new = content.strip()
    else:
        return f"Unknown action '{action}' (use 'append' or 'replace')."
    _stored, trimmed = bob_memory.block_set(name, new, db_path, owner=owner, scope=scope,
                                            cap=int(caps[name]))
    return f"Updated core-memory block '{name}'." + (" (oldest content trimmed to fit the cap)"
                                                      if trimmed else "")


def core_blocks_block(owner: Optional[str] = None, scope: Optional[str] = None,
                      config: Optional[dict] = None) -> Optional[str]:
    """The always-injected core-memory section (framed), or None when memory is off or no blocks are
    configured. Lists every configured block name in a STABLE (sorted) order so an unedited turn yields
    byte-identical output — preserving the prefix cache. Best-effort: any failure yields None."""
    cfg = config or load_config()
    mem = cfg.get("memory", {})
    caps = _core_block_caps(cfg)
    if not mem.get("enabled", False) or not caps:
        return None
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    try:
        import bob_memory  # type: ignore
        blocks = bob_memory.block_list(db_path, owner=owner, scope=scope)
    except Exception:
        return None
    lines = []
    for name in sorted(caps):
        body = (blocks.get(name) or "").strip()
        lines.append(f"[{name}]\n{body}" if body else f"[{name}]\n(empty)")
    return CORE_BLOCKS_FRAME + "\n" + "\n\n".join(lines)


def conversation_search(query: str, k: int = 5, config: Optional[dict] = None,
                        owner: Optional[str] = None, scope: Optional[str] = None) -> str:
    """Search the persisted conversation transcript (recall storage) and return the matching earlier
    turns as a formatted block the model can read back into context. Owner/scoped to the acting run.
    Returns a '(no matching earlier turns)' sentinel when nothing matches."""
    cfg = config or load_config()
    db_path = _get_db_path(cfg)
    _ensure_memory_importable()
    import bob_memory  # type: ignore
    owner = owner or cfg.get("agent", {}).get("defaultOwner", "local")
    hits = bob_memory.transcript_search(query, db_path, owner=owner, scope=scope, k=k)
    if not hits:
        return "(no matching earlier turns)"
    lines = []
    for h in hits:
        who = h["role"] if not h.get("tool_name") else f"tool:{h['tool_name']}"
        lines.append(f"[{who}] {h['content']}")
    return "\n".join(lines)


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
    # The reranker is served by llama-swap (LiteLLM's /rerank wants a cloud provider, not local llama.cpp),
    # so the rerank call targets the endpoint port directly; memory.rerankBaseUrl overrides for a remote one.
    rerank_on = bool(_mem(mem, "rerank"))
    rerank_url = (_mem(mem, "rerankBaseUrl") or f"http://localhost:{_port(cfg, 'port')}/v1") if rerank_on else None
    # memory.rerankThreshold gates the cross-encoder scores; unset leaves the recall default in charge.
    rerank_threshold = _mem(mem, "rerankThreshold")
    extra = {"rerank_threshold": float(rerank_threshold)} if rerank_threshold is not None else {}
    results = bob_memory.recall(
        query, k=k, db_path=db_path,
        threshold=float(_mem(mem, "recallThreshold")),
        owner=owner, scope=scope,
        weights=ranking, type_weights=mem.get("typeWeights"),
        half_lives=ranking.get("halfLifeDays"),
        # Hybrid recall (dense + BM25/FTS5 + RRF). Default 'dense' is the dense-only path.
        retrieval=_mem(mem, "retrieval"), rrf_k=int(_mem(mem, "rrfK")),
        # Optional cross-encoder second stage over the fused candidates (default off -> hybrid unchanged).
        rerank=rerank_on, rerank_top_n=int(_mem(mem, "rerankTopN")), rerank_url=rerank_url,
        **extra,
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
        body = bob_memory.profile_block(owner, db_path, max_chars=tokens_to_chars(max_tokens))
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
    body = "\n\n".join(parts)[: tokens_to_chars(_mem(mem, "bobMdMaxTokens"))]
    return "Project instructions (from BOB.md — follow these for this project):\n" + body


def budget_injection(blocks: list, max_tokens: int) -> tuple:
    """Fit optional injected-memory blocks into ~max_tokens (≈4 chars/token) before they are
    concatenated into the system prompt. `blocks` is a list of (label, text, priority); higher
    priority is kept longer. Greedy by priority desc; the single highest-priority block is always kept
    even if it alone exceeds the budget (so we never inject nothing when a large BOB.md is present).
    Trim order therefore drops autoRecall before profile before BOB.md. Returns
    (joined_text, kept_labels, dropped_labels)."""
    max_chars = tokens_to_chars(max_tokens)
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


# --- model windows, role availability, and the one non-agent completion path -----------------------

def slot_ctx(spec: dict, defaults: dict) -> int:
    """The window ONE request gets from a llama-server: -c split across its slots unless the KV cache is
    unified. Slots come from the role's own --parallel/-np flag or the registry defaults' `parallel`; a
    split is assumed unless --kv-unified is explicit, since overstating the window is the failure (the
    request overruns its slot) and understating it is not."""
    flags = [str(f) for f in (spec.get("flags") or [])]
    slots = int((defaults or {}).get("parallel") or 1)
    for i, f in enumerate(flags[:-1]):
        if f in ("--parallel", "-np"):
            slots = int(flags[i + 1])
    ctx = int(spec.get("ctx") or 0)
    if slots > 1 and not {"--kv-unified", "-kvu"} & set(flags):
        return ctx // slots
    return ctx


def _models_view():
    """(registry, active-profile name, role->spec) from bob_models, or (None, None, None) when the model
    registry can't be read (a stripped checkout, a malformed user.json). Callers then degrade to
    'unknown' rather than failing a run over a lookup."""
    try:
        import bob_models
        mcfg = bob_models.load_models_config()
        name = bob_models.resolve_profile_name(config=mcfg)
        return mcfg, name, bob_models.profile_roles(name, config=mcfg)
    except Exception:
        return None, None, None


def _pro_spec(mcfg: dict, role: str):
    """(peer, role spec) for a `<role>-pro` model: the first enabled peer that serves `<role>`, the same
    first-peer-wins rule the LiteLLM generator applies. (None, None) when no enabled peer serves it."""
    if not mcfg or not role or not role.endswith("-pro"):
        return None, None
    base = role[: -len("-pro")]
    for peer in (mcfg.get("peers") or {}).values():
        if not isinstance(peer, dict) or peer.get("enabled") is False:
            continue
        pro = peer.get("pro") or {}
        if base in pro:
            return peer, (pro[base] if isinstance(pro[base], dict) else {})
    return None, None


def role_window(config: dict, role: str) -> int:
    """The per-request context window (tokens) of the model serving `role`: a local role's ctx on the
    active profile divided across its slots (slot_ctx), or a pro role's contextWindow from the peer that
    serves it. 0 when the role is unknown or the registry can't be read."""
    mcfg, _name, roles = _models_view()
    if roles is None or not role:
        return 0
    spec = roles.get(role)
    if spec:
        return slot_ctx(spec, mcfg.get("defaults") or {})
    peer, rv = _pro_spec(mcfg, role)
    if peer is not None:
        return int(rv.get("contextWindow") or peer.get("contextWindow") or 0)
    return 0


def request_window(config: dict, role: str, explicit=None) -> int:
    """The context one request on `role` may fill: the model's per-request window (role_window), capped by
    agent.maxContextTokens when that is a positive number (0 / 'auto' means the model's own window).
    `explicit` stands in for the config value. 0 when neither is known."""
    window = role_window(config, role)
    if explicit is None:
        explicit = ((config or {}).get("agent", {}) or {}).get("maxContextTokens", 0)
    try:
        explicit = int(explicit or 0)
    except (TypeError, ValueError):
        explicit = 0                      # 'auto' (or junk) -> the model's own window
    if explicit > 0:
        return min(explicit, window) if window else explicit
    return window


def cap_output(max_out: int, window: int) -> int:
    """`max_out` clamped to half of `window`, leaving the other half for the prompt; unclamped when the
    window is unknown (0). Never below 1."""
    max_out = max(1, int(max_out))
    return min(max_out, max(1, window // 2)) if window else max_out


def role_output_tokens(config: dict, role: str) -> int:
    """The generation a request on `role` asks for by default. A pro role takes its peer's maxOutputTokens
    (the role's, else the peer's: the same value gen-litellm writes as that model's max_tokens), because
    a cloud model's reply is not what the local window has to hold. Everything else, and a pro role with
    no maxOutputTokens, takes agent.outputReserveTokens."""
    reserve = int(((config or {}).get("agent", {}) or {}).get("outputReserveTokens", 1024))
    if not role or not role.endswith("-pro"):
        return reserve
    mcfg, _name, _roles = _models_view()
    peer, rv = _pro_spec(mcfg, role)
    if peer is None:
        return reserve
    return int(rv.get("maxOutputTokens") or peer.get("maxOutputTokens") or reserve)


def is_local_role(model: str, config: dict = None) -> bool:
    """True if `model` is a locally served (llama-swap) role rather than a cloud peer. Scopes the
    reasoning chat-template kwarg to local models: llama-server consumes `enable_thinking`, cloud peers
    have their own reasoning behavior and don't take it. When the registry can't be read, treat it as
    local so local reasoning suppression (the common path) still applies."""
    _mcfg, _name, roles = _models_view()
    if roles is None:
        return True
    return model in roles


# A role the active profile doesn't serve falls back to the chat model (the one role every profile has):
# the cpu profile serves only chat/writer/agent, and 8gb/12gb have no vision model.
_PROFILE_FALLBACK = {"coder": "chat", "ponder": "chat", "writer": "chat", "agent": "chat"}


def served_role(config: dict, role: str, notice=None) -> str:
    """`role`, or the chat model when the active profile doesn't serve it (coder/ponder/writer/agent ->
    chat), announcing the swap once on stderr (or through `notice(text)`). Pro roles and names outside
    the fallback table pass through unchanged, as does everything when the registry can't be read."""
    if not role or role.endswith("-pro"):
        return role
    _mcfg, name, roles = _models_view()
    if roles is None or role in roles:
        return role
    fallback = _PROFILE_FALLBACK.get(role)
    if not fallback or fallback not in roles:
        return role
    msg = f"[bob] the '{name}' profile has no '{role}' model; using '{fallback}' instead."
    if notice is not None:
        notice(msg)
    else:
        print(msg, file=sys.stderr)
    return fallback


def image_refusal(config: dict, role: str) -> Optional[str]:
    """None when `role` may be sent image input, else an actionable message. Refuses when vision is
    switched off (vision.enabled=false), when the active profile serves no vision model, when a local role
    is text-only (neither supportsVision nor an mmproj in its spec, e.g. a pinned --role coder), and when a
    pro role routes to a peer that doesn't take images (supportsVision unset), instead of letting the
    backend answer with a 400."""
    vcfg = (config or {}).get("vision", {}) or {}
    if vcfg.get("enabled", True) is False:
        return "Vision is disabled (vision.enabled=false in config/user.json); image input is refused."
    mcfg, name, roles = _models_view()
    if roles is None or not role:
        return None
    if role.endswith("-pro"):
        peer, rv = _pro_spec(mcfg, role)
        if peer is None:
            return (f"No enabled cloud peer serves '{role}', so it can't take this image. "
                    "Drop --pro to use the local vision model.")
        if not bool(rv.get("supportsVision", peer.get("supportsVision", False))):
            return (f"'{role}' routes to {rv.get('model') or 'a cloud model'}, which takes no images. "
                    "Drop --pro to use the local vision model, or point vision.visionProRole at a "
                    "vision-capable peer.")
        return None
    if role in roles:
        spec = roles[role] or {}
        if spec.get("supportsVision") or spec.get("mmproj"):
            return None
        return (f"'{role}' on the '{name}' profile is a text-only model and can't read images. Drop the "
                "role override so the image routes to the vision model.")
    vision_role = vcfg.get("visionRole") or "vision"
    if role == vision_role or role == "vision":
        return (f"The '{name}' profile serves no vision model, so image input can't be read. Switch to a "
                "profile with one (bob profile 16gb) or use a vision-capable cloud role (--pro).")
    return None


def voice_disabled(config: dict) -> Optional[str]:
    """A clear refusal when voice is switched off (voice.enabled=false), else None. Gates `bob voice`
    and the shell's /voice."""
    if ((config or {}).get("voice", {}) or {}).get("enabled", True) is False:
        return "Voice is disabled (voice.enabled=false in config/user.json). Set it to true to use voice."
    return None


class CompletionError(RuntimeError):
    """A completion call failed (unreachable proxy, backend error). The message names the role."""


# Headroom kept between the fitted prompt and the window so template tokens never tip it over.
_COMPLETE_MARGIN_TOKENS = 64


def _message_tokens_est(m: dict) -> int:
    content = m.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content)
    return est_tokens(content) + 4


def fit_messages(messages: list, budget: int) -> list:
    """Fit a chat message list into `budget` tokens: the system message(s) and the newest message are
    always kept; older messages are dropped oldest-first, and a newest message that alone overflows is
    cut in the middle (head and tail survive). Returns a new list; `budget` <= 0 returns it unchanged."""
    if budget <= 0:
        return list(messages)
    system = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    room = budget - sum(_message_tokens_est(m) for m in system)
    kept: list = []
    for m in reversed(rest):
        t = _message_tokens_est(m)
        if room - t >= 0:
            kept.append(m)
            room -= t
            continue
        if not kept:
            content = m.get("content")
            if isinstance(content, str):
                chars = max(0, tokens_to_chars(max(room, 0) - 8))
                head = chars // 2
                marker = "\n\n[...middle truncated to fit the model's context window...]\n\n"
                cut = content[:head] + marker + (content[-(chars - head):] if chars - head > 0 else "")
                kept.append({**m, "content": cut})
            else:
                kept.append(m)
        break
    return system + list(reversed(kept))


def complete(config: dict, role: str, messages: list, max_out: int, *, timeout: int = None,
             think: bool = False) -> tuple:
    """One non-agent completion: the shared path for summaries, plan/verify turns and the plugins.
    Returns (text, finish_reason). The role falls back to one the active profile serves (served_role);
    the input is fitted to that model's per-request window minus `max_out` (fit_messages); thinking is
    switched off on local roles unless `think` (a reasoning model would otherwise spend max_out thinking
    and return nothing). The window honours agent.maxContextTokens (request_window). Empty content is
    logged with its finish_reason. Raises CompletionError on failure, never returns a silent ''."""
    import logging

    role = served_role(config, role)
    window = request_window(config, role)
    max_out = cap_output(max_out, window)
    if window:
        messages = fit_messages(messages, window - max_out - _COMPLETE_MARGIN_TOKENS)
    kwargs = dict(model=role, messages=messages, max_tokens=max_out, stream=False,
                  timeout=int(timeout or (config or {}).get("agent", {}).get("requestTimeout", 600)))
    if is_local_role(role, config):
        kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(think)}}
    try:
        resp = get_llm_client(config).chat.completions.create(**kwargs)
        choice = resp.choices[0]
    except Exception as e:
        raise CompletionError(f"completion on '{role}' failed: {e}") from e
    text = (getattr(choice.message, "content", None) or "") if getattr(choice, "message", None) else ""
    finish = getattr(choice, "finish_reason", None)
    if not text.strip():
        logging.getLogger("bob.agent").warning(
            "completion on '%s' returned no content (finish_reason=%s, max_tokens=%d)", role, finish, max_out)
    return text, finish
