"""Bob memory: store/recall via SQLite + the `embed` role's embeddings.

Usage:
  bob_memory.py [--db PATH] store "text" [--source user|session]
  bob_memory.py [--db PATH] recall "query" [--top 5] [--threshold 0.3]
  bob_memory.py [--db PATH] status
  bob_memory.py [--db PATH] clear [--yes]          (facts, core blocks and transcript)
  bob_memory.py [--db PATH] forget --query "q" [--yes]
  bob_memory.py [--db PATH] init-profile --name "Siva" --work "game dev"

Runs inside venv-litellm (has requests). Requires: sqlite-utils.
Embed endpoint resolved from config (litellmPort), model=memory.embedModel (default `embed`); the
backing GGUF comes from the active profile's embed role and is stamped per row (see _V4_COLUMNS). A
profile with no embed role (cpu) still stores and recalls, keyword-only (semantic_available()).
"""

# Lazy annotations so `-> sqlite_utils.Database` doesn't evaluate (and need the import) at def time.
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Optional deps: memory needs requests + sqlite-utils. IMPORT-SAFE — never sys.exit at import.
# bob_core imports this module (memory_store/recall), so a missing memory dep must not kill the whole
# agent/runtime; and it must not turn a dep-less CI box into a whole-suite collection failure. Absence
# is surfaced as a clean RuntimeError at the call boundary (CONTRIBUTING §2) via _require_deps().
try:
    import requests
    import sqlite_utils
    _DEPS_ERROR = None
except ImportError as e:  # noqa: BLE001 — capture, don't exit
    requests = None
    sqlite_utils = None
    _DEPS_ERROR = e


def _require_deps() -> None:
    """Raise a clean RuntimeError if the optional memory deps are absent (caught by cmd_*/main)."""
    if _DEPS_ERROR is not None:
        raise RuntimeError(
            f"memory requires sqlite-utils + requests ({_DEPS_ERROR}). "
            "Install: pip install sqlite-utils requests"
        )

_DEFAULT_DB = Path(__file__).parent.parent / "data" / "bob.db"
# LiteLLM model names of the embed / rerank roles. The embed name is read from memory.embedModel at
# call time (_embed_role); this is only its default.
EMBED_MODEL = "embed"
RERANK_MODEL = "rerank"

log = logging.getLogger("bob.memory")

# Warnings keyed by message -> last emitted (monotonic seconds). A reason repeats at most once per
# _WARN_INTERVAL, so a broken reranker stays visible without flooding every recall.
_warned: dict = {}
_WARN_INTERVAL = 600.0


def _warn(msg: str) -> None:
    now = time.monotonic()
    last = _warned.get(msg)
    if last is not None and now - last < _WARN_INTERVAL:
        return
    _warned[msg] = now
    log.warning(msg)
    print(f"[warn] {msg}", file=sys.stderr)


class SemanticUnavailable(RuntimeError):
    """The active profile has no embed role (e.g. the cpu profile), so there are no vectors to make.
    Write paths persist the row without a vector and recall degrades to keyword search."""


# --- Schema (v2 typed/owner-scoped; v3 provenance; v4 embed stamp; v5 embed context) ---
# `get_db` migrates a legacy DB in place (additive ALTERs + one-time backfill), gated by PRAGMA
# user_version so the common path is a cheap version read. Migrations run as an incremental ladder
# (v1→v2→v3→v4→v5), each step idempotent and column-presence-guarded.
SCHEMA_VERSION = 5

# Columns added to the v1 `memories` table (id/content/embedding/source/created_at/last_used/
# use_count already exist). NOT NULL columns carry a literal default so ALTER ADD COLUMN is legal
# on a populated table; owner_id's 'local' matches agent.defaultOwner's default (the write path
# threads the real owner into NEW writes — this backfill just stamps legacy rows).
_V2_COLUMNS = [
    ("content_hash", "TEXT"),                          # sha256(normalized) — exact-dedup fast path
    ("type", "TEXT NOT NULL DEFAULT 'fact'"),          # profile|preference|project|fact|episodic
    ("subject", "TEXT NOT NULL DEFAULT 'user'"),
    ("owner_id", "TEXT NOT NULL DEFAULT 'local'"),
    ("scope", "TEXT"),                                 # optional project/cwd key (type='project')
    ("tags", "TEXT"),
    ("salience", "REAL NOT NULL DEFAULT 1.0"),
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("superseded_by", "INTEGER"),                      # soft-update: id of the replacing row
    ("updated_at", "TEXT"),
    ("expires_at", "TEXT"),
]

# v3 — per-row provenance: the originating session id, stamped by consolidation.
_V3_COLUMNS = [
    ("source_session", "TEXT"),                        # session that produced this row (audit / forget --session)
]

# v4: which embedding model produced this row's vector. Vectors from two different models are not
# comparable, and same-dimension models (bge-m3 and Qwen3-Embedding-0.6B are both 1024) make the
# mismatch SILENT: cosine() zips them happily and returns a meaningless score. Stamping the model
# lets the recall and dedup paths treat a stale vector as "no vector yet" instead.
# NULL means "no evidence", NOT stale: only _migrate_to_v4 knows a pre-v4 row is stale (it says so by
# stamping the old name), and every write since v4 stamps the current one. Reading NULL as stale would
# silently drop vectors we have no reason to distrust.
_V4_COLUMNS = [
    ("embed_model", "TEXT"),                           # gguf filename of the embed role that made the vector
]
# v5: the situating line store() prepended to the embed input (contextual chunks, e.g. the code index),
# so a re-embed reproduces the same input instead of embedding the bare content.
_V5_COLUMNS = [
    ("embed_context", "TEXT"),
]
# The embed model in use before v4; every pre-v4 row's vector came from it.
_PRE_V4_EMBED_MODEL = "bge-m3-q8_0.gguf"


# §2.3 third-person normalization — deterministic, leading-pronoun-anchored, conservative. Specific
# forms first so `I'm`/`I've`/`I am` win over the bare `I `. NOTE: this is the cheap fast path — it
# swaps the pronoun but does NOT conjugate the verb ("I prefer" -> "User prefer", not "prefers").
# The conjugated forms in the design doc's §7 table are the LLM/consolidation ideal; the
# deterministic path stays conservative and anything unmatched is stored as-is (framed at read time).
_PRONOUN_PREFIXES = [
    ("I'm ", "User is "),
    ("I've ", "User has "),
    ("I am ", "User is "),
    ("My ", "User's "),
    ("I ", "User "),
]


def _normalize_third_person(content: str) -> str:
    """Rewrite a first-person note to third person via the §2.3 deterministic rules. Reused by both
    `migrate --normalize` and the write path."""
    text = content.strip()
    for prefix, repl in _PRONOUN_PREFIXES:
        if text.startswith(prefix):
            text = repl + text[len(prefix):]
            break
    return text.replace(" my ", " the user's ")


def _content_hash(content: str) -> str:
    """sha256 of the (normalized) content — the exact-dedup key and a migration audit field."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- Blended ranking defaults ----------------------------------------
# Mirror config/defaults.json memory.ranking / memory.typeWeights. recall() reads config-supplied
# overrides when given, else these. A near-immortal half-life (~100y) makes profile/preference decay
# negligible; episodic decays in weeks.
_DEFAULT_WEIGHTS = {"wSemantic": 1.0, "wRecency": 0.3, "wType": 0.2, "wUsage": 0.1, "wSalience": 0.3}
_DEFAULT_TYPE_WEIGHTS = {"profile": 1.0, "preference": 0.9, "project": 0.8, "fact": 0.7, "episodic": 0.5}
_DEFAULT_HALF_LIVES = {"profile": 36500, "preference": 36500, "project": 90, "fact": 365, "episodic": 30}


_MAX_AGE_DAYS = 3_650_000.0   # ~10000y — a missing/unparseable timestamp ages to "ancient"


def _age_days(created_at, now: datetime) -> float:
    """Age of a row in days. Tolerates both store()'s ISO8601 timestamps and SQLite's
    'YYYY-MM-DD HH:MM:SS' default form. A missing/unparseable value ages to _MAX_AGE_DAYS
    (ranks oldest, decay≈0) instead of 0.0 — the old 'fresh' default let corrupt rows rank as the
    freshest."""
    if not created_at:
        return _MAX_AGE_DAYS
    try:
        dt = datetime.fromisoformat(str(created_at).replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (now - dt).total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return _MAX_AGE_DAYS

# Resolve the LiteLLM base URL + auth from config (single source of truth, CONTRIBUTING §8)
# instead of hardcoding :8081. Memoized per process; falls back to the central port default
# if config.json isn't present yet.
_LITELLM: dict = {}


def _litellm() -> "tuple[str, dict]":
    """Return (base_url, headers) for the LiteLLM proxy, resolved from config and memoized.

    If the config isn't readable yet (missing or corrupt), return the central port default and the key
    from the secret seam WITHOUT memoizing, so a later call re-reads once the real config exists."""
    cached = _LITELLM.get("v")
    if cached is not None:
        return cached
    import bob_core
    try:
        cfg = bob_core.load_config()
    except Exception:  # FileNotFoundError, JSONDecodeError, ... — fall back but don't cache it
        base = f"http://localhost:{bob_core._PORT_DEFAULTS['litellmPort']}/v1"
        return base, {"Authorization": f"Bearer {bob_core._litellm_key({})}"}
    val = (
        f"http://localhost:{bob_core._port(cfg, 'litellmPort')}/v1",
        {"Authorization": f"Bearer {bob_core._litellm_key(cfg)}"},
    )
    _LITELLM["v"] = val
    return val


def get_db(db_path) -> sqlite_utils.Database:
    """Open (and migrate) a memory DB. WAL + a busy timeout, like sessions.db, so a TUI, the agent
    server and a CLI verb can share the file: readers never block the writer, and a writer waits out a
    transient lock instead of failing with "database is locked". The caller owns the connection;
    internal paths use _open(), which commits and closes it."""
    _require_deps()
    db_path = Path(db_path)  # accept str (e.g. bob_core._get_db_path) or Path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(db_path)
    db.execute("PRAGMA busy_timeout = 5000")
    _ensure_wal(db)
    _ensure_schema(db)
    return db


def _ensure_wal(db: sqlite_utils.Database, attempts: int = 20) -> None:
    """Put the DB in WAL mode. The mode persists in the file, so it is read first and only switched when
    it differs; the switch needs an exclusive lock and does not wait on busy_timeout, so two processes
    opening a brand-new DB at once can see "database is locked" and it is retried with a short backoff."""
    import sqlite3

    for attempt in range(attempts):
        try:
            if str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
                return
            db.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if attempt == attempts - 1 or ("locked" not in msg and "busy" not in msg):
                raise
            time.sleep(0.02 * (attempt + 1))


@contextlib.contextmanager
def _open(db_path):
    """get_db scoped to one operation: commit on success, roll back on error, always close, so no
    connection outlives the call holding a write transaction."""
    db = get_db(db_path)
    try:
        yield db
        db.conn.commit()
    except BaseException:
        db.conn.rollback()
        raise
    finally:
        db.conn.close()


def _ensure_schema(db: sqlite_utils.Database) -> None:
    """Create the tables (fresh DBs get the full column set directly) and run the migration ladder on
    an existing legacy DB. Idempotent and cheap on the hot path: a current DB short-circuits on the
    version read."""
    # A fresh DB gets every column up front. On an existing older DB this is a no-op and the columns
    # are added by the _migrate_to_v* steps instead.
    db.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            content TEXT NOT NULL,
            embedding TEXT NOT NULL,
            source TEXT DEFAULT 'user',
            created_at TEXT DEFAULT (datetime('now')),
            last_used TEXT,
            use_count INTEGER DEFAULT 0,
            content_hash TEXT,
            type TEXT NOT NULL DEFAULT 'fact',
            subject TEXT NOT NULL DEFAULT 'user',
            owner_id TEXT NOT NULL DEFAULT 'local',
            scope TEXT,
            tags TEXT,
            salience REAL NOT NULL DEFAULT 1.0,
            pinned INTEGER NOT NULL DEFAULT 0,
            superseded_by INTEGER,
            updated_at TEXT,
            expires_at TEXT,
            source_session TEXT,
            embed_model TEXT,
            embed_context TEXT
        )
    """)
    # Identity lives as type='profile' rows in `memories` (cmd_init_profile + consolidation); there is
    # no separate profile table.
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    if version < 2:
        _migrate_to_v2(db)
    if version < 3:
        _migrate_to_v3(db)
    if version < 4:
        _migrate_to_v4(db)
    if version < 5:
        _add_missing_columns(db, _V5_COLUMNS)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.conn.commit()   # persist the ALTERs/backfill/version across this and future connections


def _add_missing_columns(db: sqlite_utils.Database, columns: list) -> None:
    """ALTER in any columns not already present (column-presence-guarded, so safe to re-run — mirrors
    SessionStore's table_info pattern in bob_session._ensure_schema)."""
    existing = {row[1] for row in db.execute("PRAGMA table_info(memories)").fetchall()}
    for name, decl in columns:
        if name not in existing:
            db.execute(f"ALTER TABLE memories ADD COLUMN {name} {decl}")


def _migrate_to_v2(db: sqlite_utils.Database) -> None:
    """v1 -> v2: add the typed/owner columns, backfill type from the legacy `source`, create the v2
    indexes. Version stamping is done by the caller (_ensure_schema)."""
    _add_missing_columns(db, _V2_COLUMNS)
    # Backfill type from the legacy source (owner_id/subject already carry their column defaults).
    db.execute("UPDATE memories SET type='preference' WHERE source='user'")
    db.execute("UPDATE memories SET type='episodic'  WHERE source='session'")
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_mem_owner_type ON memories(owner_id, type)",
        "CREATE INDEX IF NOT EXISTS idx_mem_hash       ON memories(content_hash)",
        "CREATE INDEX IF NOT EXISTS idx_mem_scope      ON memories(owner_id, scope)",
        "CREATE INDEX IF NOT EXISTS idx_mem_active     ON memories(owner_id, superseded_by)",
    ):
        db.execute(stmt)


def _migrate_to_v3(db: sqlite_utils.Database) -> None:
    """v2 -> v3: add the source_session provenance column + its index. Version stamping is
    done by the caller (_ensure_schema)."""
    _add_missing_columns(db, _V3_COLUMNS)
    db.execute("CREATE INDEX IF NOT EXISTS idx_mem_session ON memories(owner_id, source_session)")


def _migrate_to_v4(db: sqlite_utils.Database) -> None:
    """v3 -> v4: add embed_model and stamp existing rows with the model that produced their vectors.
    Backfilling (rather than leaving NULL) is what makes the staleness check decidable: after an embed
    model swap those rows compare as stale and drop out of semantic recall until `bob memory migrate
    --reembed` rebuilds them. Version stamping is done by the caller (_ensure_schema)."""
    _add_missing_columns(db, _V4_COLUMNS)
    db.execute("UPDATE memories SET embed_model=? WHERE embed_model IS NULL AND embedding != ''",
               [_PRE_V4_EMBED_MODEL])


def stale_vector_count(db_path) -> int:
    """How many rows hold a vector from a DIFFERENT embed model than the active one.

    Read-only and side-effect free ON PURPOSE: `bob doctor` calls this, and a health check must not
    open the DB through get_db and silently run the migration ladder. Uses stdlib sqlite3 so it works
    without the optional deps, and returns 0 for a missing file, a pre-v4 schema (no embed_model
    column yet, so nothing has been marked stale), a profile with no embed role (nothing to compare
    against), or anything unreadable.
    """
    import sqlite3
    path = Path(db_path)
    if not path.exists():
        return 0
    current = current_embed_model()
    if current is None:
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        if "embed_model" not in cols:
            return 0
        return conn.execute(
            "SELECT COUNT(*) FROM memories WHERE embedding != '' "
            "AND embed_model IS NOT NULL AND embed_model IS NOT ?",
            [current],
        ).fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


# --- Config + model registry (read-only) -----------------------------------------------------
_CFG: dict = {}


def _app_config() -> dict:
    """The resolved app config (memoized once readable, like _litellm); {} while it isn't."""
    cached = _CFG.get("v")
    if cached is not None:
        return cached
    try:
        import bob_core
        cfg = bob_core.load_config()
    except Exception:
        return {}
    _CFG["v"] = cfg
    return cfg


def _mem_setting(key: str, default, config: dict = None):
    mem = (config if config is not None else _app_config()).get("memory") or {}
    val = mem.get(key)
    return default if val is None or val == "" else val


def _embed_role(config: dict = None) -> str:
    """LiteLLM model name (== registry role) of the embedder: memory.embedModel, default 'embed'."""
    return str(_mem_setting("embedModel", EMBED_MODEL, config))


_REGISTRY: dict = {}


def _registry_key() -> tuple:
    """What the active profile's roles depend on: $BOB_PROFILE plus the registry, user overlay and
    active-profile files. A `bob profile` switch changes the key, so a long-lived TUI or agent server
    picks up the new roles on its next call instead of keeping the old embed stamp."""
    import bob_models
    stamp = []
    for f in (bob_models.MODELS_FILE, bob_models.USER_FILE, bob_models._active_profile_file()):
        try:
            st = f.stat()
            stamp.append((str(f), st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append((str(f), None))
    return (os.environ.get("BOB_PROFILE"), tuple(stamp))


def _registry() -> "dict | None":
    """{'roles': role->spec, 'defaults': {...}} for the active profile, or None when the registry
    can't be read. Cached on _registry_key()."""
    try:
        import bob_models
        key = _registry_key()
        hit = _REGISTRY.get("v")
        if hit and hit[0] == key:
            return hit[1]
        cfg = bob_models.load_models_config()
        val = {"roles": bob_models.profile_roles(config=cfg), "defaults": cfg.get("defaults") or {}}
    except Exception:
        return None
    _REGISTRY["v"] = (key, val)
    return val


def _role_spec(role: str) -> "dict | None":
    reg = _registry()
    return (reg or {}).get("roles", {}).get(role)


def current_embed_model() -> "str | None":
    """Identity of the embed role's GGUF in the active profile: the stamp that says whether a stored
    vector is still comparable to a fresh one. None when the profile has no embed role or the registry
    can't be read; None means "no basis to judge", so nothing is treated as stale."""
    spec = _role_spec(_embed_role())
    return spec.get("gguf") if spec else None


def semantic_available(config: dict = None) -> bool:
    """Whether the active profile serves an embedder, i.e. whether semantic recall, near-dedup and
    vector stamping can work at all. False on the cpu profile (no embed role): memory still stores and
    keyword-searches, it just has no vectors. An unreadable registry counts as available, so a
    transient read error never switches semantic memory off; the embed call decides."""
    reg = _registry()
    if reg is None:
        return True
    return _embed_role(config) in reg["roles"]


def _fresh_vec_sql(current: "str | None") -> "tuple[str, list]":
    """SQL predicate (+ params) for "this row's vector is comparable to a fresh one": its stamp is NULL
    (no evidence) or the current model. With no current model there is nothing to compare, so every
    vector passes."""
    if current is None:
        return "1", []
    return "(embed_model IS NULL OR embed_model = ?)", [current]


# --- Input limits ------------------------------------------------------------------------------
# A conservative estimate: code and punctuation-dense text tokenize denser than prose, and truncating a
# little early costs far less than a rejected request.
_CHARS_PER_TOKEN = 2.5
# Qwen3-Reranker's prompt template around (query, doc): measured at ~82 tokens for a 5-word query + doc.
_RERANK_TEMPLATE_TOKENS = 96
_RERANK_MIN_TOKENS = 128   # below this a rerank would score truncated fragments: fail instead
_EMBED_MARGIN_TOKENS = 32


def _flag_value(flags, names) -> "int | None":
    flags = [str(f) for f in (flags or [])]
    for i, f in enumerate(flags):
        for n in names:
            if f == n and i + 1 < len(flags):
                try:
                    return int(flags[i + 1])
                except ValueError:
                    return None
            if f.startswith(n + "="):
                try:
                    return int(f.split("=", 1)[1])
                except ValueError:
                    return None
    return None


def _role_limits(role: str) -> "tuple[int | None, int | None]":
    """(ctx, n_ubatch) of a served role, from the registry: `ctx` (-c) and the smallest of the role's
    -ub/-b flags or the profile-wide ubatch/batch defaults (llama.cpp caps n_ubatch at n_batch).
    (None, None) when the role isn't in the active profile."""
    reg = _registry()
    spec = (reg or {}).get("roles", {}).get(role)
    if not spec:
        return None, None
    d = reg.get("defaults") or {}
    flags = spec.get("flags") or []
    ub = _flag_value(flags, ("-ub", "--ubatch-size")) or int(d.get("ubatch") or 512)
    b = _flag_value(flags, ("-b", "--batch-size")) or int(d.get("batch") or 2048)
    ctx = spec.get("ctx")
    return (int(ctx) if ctx else None), min(ub, b)


def _fit_text(text: str, max_chars: int) -> str:
    """Head + tail of `text` within max_chars (the middle is dropped): the opening says what the
    text is about, the ending often holds the conclusion."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars < 16:
        return text[:max_chars]
    head = (max_chars * 2) // 3
    tail = max_chars - head - 5
    return text[:head] + "\n...\n" + text[-tail:]


# --- HTTP ----------------------------------------------------------------------------------------
_HTTP_TIMEOUT = 30


class MemoryHTTPError(RuntimeError):
    """A non-2xx reply from an inference endpoint; carries the status and the body's start."""

    def __init__(self, url: str, status: int, body: str):
        super().__init__(f"{url} returned HTTP {status}: {body}")
        self.status = status
        self.body = body


def _post_json(path: str, payload: dict, timeout: float = None, base_url: str = None) -> dict:
    """POST `payload` to `<base>/<path>` (base: the LiteLLM proxy, or `base_url`) and return the parsed
    JSON body. The one HTTP path for embed, rerank and summarize: every transport failure, non-2xx
    status or non-JSON body raises RuntimeError naming the URL."""
    _require_deps()
    base, headers = _litellm()
    url = f"{(base_url or base).rstrip('/')}/{path.lstrip('/')}"
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout or _HTTP_TIMEOUT)
    except requests.RequestException as e:
        raise RuntimeError(f"{url} unreachable: {e}") from e
    if resp.status_code >= 400:
        raise MemoryHTTPError(url, resp.status_code, (resp.text or "")[:300])
    try:
        return resp.json()
    except ValueError as e:
        raise RuntimeError(f"{url} returned a non-JSON body: {e}") from e


def _input_too_large(err: Exception) -> bool:
    """llama-server's rejection of an input over n_ctx / n_ubatch."""
    if not isinstance(err, MemoryHTTPError):
        return False
    body = err.body.lower()
    return any(s in body for s in ("too large", "exceed", "context size", "context length"))


def embed(text: str) -> list[float]:
    """The embed role's vector for `text`, truncated (head + tail) to fit the role's context with a
    margin, since llama-server rejects an input at or over n_ctx. A rejection that still says the input
    is too large (a tokenizer denser than the estimate) retries once at half the size. Raises
    SemanticUnavailable when the profile has no embed role, RuntimeError on any other failure."""
    if not semantic_available():
        raise SemanticUnavailable("the active profile has no embed role; memory runs keyword-only")
    ctx, _ub = _role_limits(_embed_role())
    max_chars = int(((ctx or 2048) - _EMBED_MARGIN_TOKENS) * _CHARS_PER_TOKEN)
    for attempt in range(2):
        payload = {"model": _embed_role(), "input": [_fit_text(text, max_chars)]}
        try:
            return _post_json("embeddings", payload)["data"][0]["embedding"]
        except RuntimeError as e:
            if attempt == 0 and _input_too_large(e):
                max_chars //= 2
                continue
            raise RuntimeError(f"embedding failed: {e}") from e
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"embedding server returned bad data: {e}") from e
    raise RuntimeError("embedding failed: input still too large after truncation")


def _rerank_budget() -> "tuple[int, int]":
    """(query_chars, doc_chars) that keep template + query + doc inside the rerank role's limit.
    RANK pooling can't split a sequence across ubatches, so the limit is min(ctx, n_ubatch), not ctx.
    Raises RuntimeError when that leaves too little room to rank anything meaningful."""
    ctx, ub = _role_limits(RERANK_MODEL)
    if ctx is None and ub is None:
        raise RuntimeError("the active profile has no rerank role")
    limit = min(x for x in (ctx, ub) if x)
    avail = limit - _RERANK_TEMPLATE_TOKENS
    if avail < _RERANK_MIN_TOKENS:
        raise RuntimeError(f"rerank input budget is {max(avail, 0)} tokens (ctx={ctx}, n_ubatch={ub}); "
                           "raise the rerank role's -ub to at least its ctx")
    q_tokens = max(16, avail // 4)
    return int(q_tokens * _CHARS_PER_TOKEN), int((avail - q_tokens) * _CHARS_PER_TOKEN)


def _rerank_scores(query: str, docs: list[str], base_url: str = None) -> list[float]:
    """Cross-encoder relevance of each doc to `query` via a /rerank endpoint (a reranker model served
    by llama-swap; LiteLLM's /rerank expects a cohere/jina/infinity provider, not the local llama.cpp
    one, so `base_url` points at the llama-swap endpoint). Query and docs are truncated to the role's
    input budget (_rerank_budget) so no pair is rejected. Returns one score per doc in input order.
    Raises on any transport / bad-shape error so the caller can loud-fail back to the un-reranked
    order. Overridable in tests, like `embed`."""
    q_chars, d_chars = _rerank_budget()
    q = _fit_text(query, q_chars)
    for attempt in range(2):
        payload = {"model": RERANK_MODEL, "query": q, "documents": [_fit_text(d, d_chars) for d in docs]}
        try:
            results = _post_json("rerank", payload, base_url=base_url)["results"]
            break
        except RuntimeError as e:
            if attempt == 0 and _input_too_large(e):
                q_chars, d_chars = q_chars // 2, d_chars // 2
                q = _fit_text(query, q_chars)
                continue
            raise
    scores: list = [None] * len(docs)
    for r in results:
        scores[r["index"]] = r["relevance_score"]
    if any(s is None for s in scores):
        raise ValueError("rerank response did not score every document")
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Importable core — one implementation for both the CLI (cmd_*) and
# bob_core.memory_store/recall. Neither prints; callers format their own output.
# ---------------------------------------------------------------------------

def _is_user_memory(owner: str, mem_type: str) -> bool:
    """Whether a row is a note about the user (normalized to third person) rather than indexed source
    text such as a code chunk, which must be stored verbatim. Synthetic owners are '__'-prefixed."""
    return mem_type != "code" and not str(owner).startswith("__")


def _stale_or_missing(emb: str, stamp: "str | None", current: "str | None") -> bool:
    """A row whose vector is absent, or was made by a model other than the current one."""
    if not emb:
        return True
    return current is not None and stamp is not None and stamp != current


def store(content: str, db_path: Path, source: str = "user", mem_type: str = "fact",
          owner: str = "local", scope: str = None, tags: str = None, salience: float = 1.0,
          dedup_threshold: float = 0.92, source_session: str = None,
          embed_optional: bool = False, context: str = None) -> tuple[int, bool]:
    """Insert a typed, owner-scoped memory. Returns (id, is_new).

    A note about the user is normalized to third person (§2.3) before hashing/embedding, so recalled
    text never reads as Bob's own identity; indexed source text (code chunks, synthetic '__' owners) is
    stored verbatim. Two-tier dedup over ACTIVE rows (not superseded, not forgotten), both returning the
    existing id with is_new=False:
      - exact: a content_hash lookup scoped to the owner, *before* any embedding call. A hit whose
        vector is missing or from an older embed model is re-embedded in place (best-effort), so
        re-indexing after an embed swap refreshes it;
      - near:  cosine >= dedup_threshold, scoped to (owner, mem_type).
    Dedup stays best-effort: read-then-insert isn't transactional, which is benign for a personal DB.

    Raises RuntimeError if the embed server is unreachable, UNLESS `embed_optional` is set, in which
    case the row is stored with no vector and near-dedup is skipped. That keeps durable identity
    persistable when inference isn't up yet (onboarding during a fresh setup): profile_block injection
    is a plain SQL read, so a vectorless row is fully usable at session start; it just won't surface
    in *semantic* recall until `bob memory migrate --reembed` fills it in. A profile with no embed role
    (SemanticUnavailable) always stores vectorless: keyword recall still finds the row.

    `context` (optional): a short chunk-situating line prepended ONLY to the text sent to the embedder,
    not to the stored content (Anthropic's Contextual Retrieval). It is persisted in embed_context so a
    re-embed reproduces the same input. Atomic facts pass none; chunked sources (the code index) do."""
    normalized = _normalize_third_person(content) if _is_user_memory(owner, mem_type) else content.strip()
    chash = _content_hash(normalized)
    embed_text = f"{context}\n{normalized}" if context else normalized
    current = current_embed_model()
    now_iso = datetime.now(timezone.utc).isoformat()
    active = "superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)"
    with _open(db_path) as db:
        # 1) exact dedup: a hash hit for the same owner short-circuits before we pay for an embedding.
        hit = db.execute(
            f"SELECT id, embedding, embed_model FROM memories WHERE content_hash=? AND owner_id=? AND {active}",
            [chash, owner, now_iso],
        ).fetchone()
        if hit:
            if _stale_or_missing(hit[1], hit[2], current) and semantic_available():
                try:
                    vec = embed(embed_text)
                    db.execute("UPDATE memories SET embedding=?, embed_model=?, embed_context=? WHERE id=?",
                               [json.dumps(vec), current, context, hit[0]])
                except RuntimeError:
                    pass
            return hit[0], False
        # 2) near dedup: cosine over this owner's rows of the same type only (scoped, not a full scan).
        try:
            vec = embed(embed_text)
        except SemanticUnavailable:
            vec = None
        except RuntimeError:
            if not embed_optional:
                raise
            vec = None   # embed server down + caller opted in: persist without a vector, skip near-dedup
        if vec is not None:
            fresh, fresh_params = _fresh_vec_sql(current)
            for eid, emb_json in db.execute(
                f"SELECT id, embedding FROM memories WHERE owner_id=? AND type=? AND {active} AND {fresh}",
                [owner, mem_type, now_iso, *fresh_params],
            ).fetchall():
                try:
                    if cosine(vec, json.loads(emb_json)) >= dedup_threshold:
                        return eid, False
                except Exception:
                    continue
        # embedding is TEXT NOT NULL; an empty string is the "no vector yet" sentinel: every recall path
        # does json.loads(embedding) inside try/except, so a "" row is skipped by semantic recall (not
        # surfaced with a bogus 0-cosine) until re-embedded, while profile_block and keyword recall use it.
        cur = db.execute(
            "INSERT INTO memories (content, content_hash, embedding, embed_model, embed_context, type, "
            "subject, owner_id, scope, tags, salience, source, source_session, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [normalized, chash, json.dumps(vec) if vec is not None else "",
             current if vec is not None else None, context, mem_type, "user", owner, scope, tags,
             salience, source, source_session, now_iso])
        return cur.lastrowid, True


# --- hybrid recall (dense + BM25/FTS5 + Reciprocal Rank Fusion) ----------

def _nonsemantic_score(w, tw, hl, mtype, created_at, use_count, salience, last_used, now) -> float:
    """The recency+type+usage+salience half of the blended score. Hybrid recall adds this on
    top of the RRF-fused relevance; the dense path inlines the identical math (kept inline there so its
    float arithmetic stays byte-for-byte the dense-only result)."""
    age = _age_days(created_at, now)
    if last_used:
        age = min(age, _age_days(last_used, now))
    decay = math.exp(-age / max(1.0, float(hl.get(mtype, 365))))
    usage = min((use_count or 0) / 10.0, 1.0)
    return (w["wRecency"] * decay + w["wType"] * tw.get(mtype, 0.5)
            + w["wUsage"] * usage + w["wSalience"] * (salience if salience is not None else 1.0))


def _fts_match_query(query: str) -> "str | None":
    """A safe FTS5 MATCH expression from arbitrary natural language: an OR of quoted word tokens.
    Quoting each token avoids FTS5 treating punctuation / bare operators as syntax (which would raise).
    None when the query has no word characters."""
    tokens = re.findall(r"\w+", query.lower())
    return " OR ".join(f'"{t}"' for t in tokens) if tokens else None


# Words that carry no lexical evidence. BM25's OR-of-tokens matches a row on any shared word, so the
# recall gate asks for real coverage of the query's content words instead (_lexical_coverage).
_LEX_STOPWORDS = {
    "the", "and", "for", "are", "was", "were", "what", "who", "whom", "how", "why", "when", "where",
    "which", "you", "your", "with", "this", "that", "these", "those", "have", "has", "had", "does",
    "did", "can", "could", "would", "should", "its", "about", "from", "into", "not", "but", "all",
    "any", "some", "our", "their", "them", "they", "there", "here", "then", "than", "too", "very",
    "just", "also", "user", "users", "like", "likes", "tell", "know", "me", "my", "is", "do",
}
_LEXICAL_GATE = 0.5   # share of the query's content words a keyword-only hit must contain


def _lexical_tokens(text: str) -> set:
    return {t for t in re.findall(r"\w+", text.lower()) if len(t) >= 3 and t not in _LEX_STOPWORDS}


def _lexical_coverage(query_tokens: set, content: str) -> float:
    """Fraction of the query's content words present in `content` (0.0 when the query has none)."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & _lexical_tokens(content)) / len(query_tokens)


def _fts5_available(db) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        db.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:
        return False


def _ensure_fts_index(db, fts_table: str, ddl: list, backfill_sql: str, current_sql: str = None) -> bool:
    """Create an external-content FTS5 index + its sync triggers once, safely under concurrency: the
    DDL runs in a BEGIN IMMEDIATE transaction with IF NOT EXISTS guards, so two processes racing the
    first hybrid recall serialize on the write lock and the loser finds the work done. The backfill runs
    only for the process that created the table. `current_sql` (a query returning a row when the
    installed triggers are current) lets a DB built with older trigger definitions upgrade in place.
    Returns False if this SQLite build lacks FTS5 (caller falls back to dense)."""
    exists_sql = "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?"
    try:
        exists = db.execute(exists_sql, [fts_table]).fetchone()
        if exists and (current_sql is None or db.execute(current_sql).fetchone()):
            return True
        if not exists and not _fts5_available(db):
            return False
        if db.conn.in_transaction:
            db.conn.commit()
        db.execute("BEGIN IMMEDIATE")
        try:
            created = not db.execute(exists_sql, [fts_table]).fetchone()
            for stmt in ddl:
                db.execute(stmt)
            if created:
                db.execute(backfill_sql)
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return True
    except Exception:
        return False


def _ensure_fts(db) -> bool:
    """Lazily build the FTS5 index over memories.content the FIRST time hybrid recall runs. Deliberately
    NOT in _ensure_schema, so dense-mode DBs are byte-unchanged (no extra table, no per-write trigger
    overhead). The update trigger fires only on a content change: recall bumps use_count/last_used on
    every hit, which must not re-index the row."""
    return _ensure_fts_index(
        db, "memories_fts",
        ["CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(content, content='memories', content_rowid='id')",
         "CREATE TRIGGER IF NOT EXISTS memories_fts_ai AFTER INSERT ON memories BEGIN "
         "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END",
         "CREATE TRIGGER IF NOT EXISTS memories_fts_ad AFTER DELETE ON memories BEGIN "
         "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); END",
         "DROP TRIGGER IF EXISTS memories_fts_au",
         "CREATE TRIGGER memories_fts_au AFTER UPDATE OF content ON memories BEGIN "
         "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); "
         "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END"],
        "INSERT INTO memories_fts(rowid, content) SELECT id, content FROM memories",
        current_sql=("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='memories_fts_au' "
                     "AND sql LIKE '%UPDATE OF content%'"),
    )


def _bm25_ranked_ids(db, query, owner, scope, now_iso, type_filter, limit) -> list:
    """Lexical retrieval: memory ids matching `query` best-first by BM25, under the SAME owner/scope/
    active prefilter recall() applies. [] on no match, no word tokens, or any FTS error."""
    match = _fts_match_query(query)
    if not match:
        return []
    sql = ("SELECT f.rowid FROM memories_fts f JOIN memories m ON m.id = f.rowid "
           "WHERE memories_fts MATCH ? AND m.owner_id = ? AND m.superseded_by IS NULL "
           "AND (m.expires_at IS NULL OR m.expires_at > ?)")
    params = [match, owner, now_iso]
    if scope is not None:
        sql += " AND (m.scope IS NULL OR m.scope = ?)"
        params.append(scope)
    if type_filter:
        sql += " AND m.type = ?"
        params.append(type_filter)
    sql += " ORDER BY f.rank, f.rowid LIMIT ?"   # bm25 best-first, id tiebreak for determinism
    params.append(limit)
    try:
        return [r[0] for r in db.execute(sql, params).fetchall()]
    except Exception:
        return []


def _recall_hybrid(query, q_vec, rows, db, owner, scope, type_filter, now, w, tw, hl,
                   threshold, k, rrf_k, rerank=False, rerank_top_n=20, rerank_url=None,
                   rerank_threshold=None) -> list:
    """Fuse the dense (cosine) and lexical (BM25) rankings with Reciprocal Rank Fusion, then apply the
    recency/type/usage/salience terms on top of the fused candidates. Falls back to a dense scan over
    the candidate set when FTS5 is unavailable or the query has no lexical hits, and to lexical-only when
    `q_vec` is None (no embedder). `rows` already carries the owner/scope/active prefilter.

    Gate first, blend after: a candidate is returned only on real relevance evidence, a cosine at or
    above `threshold` or a BM25 hit covering at least half the query's content words. When `rerank` is
    on, the cross-encoder's RAW score over the top `rerank_top_n` candidates is that evidence instead
    (gated at `rerank_threshold`, default `threshold`), and only the survivors are rescaled for the
    blend, so normalization can never lift an irrelevant doc past the gate."""
    meta = {}   # id -> (content, mtype, created_at, use_count, salience, last_used)
    cos = {}
    for row_id, content, emb_json, mtype, created_at, use_count, salience, last_used in rows:
        meta[row_id] = (content, mtype, created_at, use_count, salience, last_used)
        if q_vec is None or not emb_json:
            continue
        try:
            cos[row_id] = cosine(q_vec, json.loads(emb_json))
        except Exception:
            pass
    per_list = max(k * 5, 25)
    dense_ranked = sorted(cos, key=lambda i: (-cos[i], i))[:per_list]
    bm25_ranked = [i for i in (_bm25_ranked_ids(db, query, owner, scope, now.isoformat(), type_filter,
                                                per_list) if _ensure_fts(db) else []) if i in meta]

    def _blend(sem, i):
        return round(w["wSemantic"] * sem + _nonsemantic_score(w, tw, hl, *meta[i][1:], now), 4)

    if not bm25_ranked:
        rel = {i: cos[i] for i in dense_ranked}
    else:
        rrf = {}
        for rank, i in enumerate(dense_ranked):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
        for rank, i in enumerate(bm25_ranked):
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
        max_rrf = max(rrf.values())           # normalize so the best candidate ~= a perfect cosine (1.0)
        rel = {i: rrf[i] / max_rrf for i in rrf}

    q_tokens = _lexical_tokens(query)
    lexical = set(bm25_ranked)
    passed = {i for i in rel
              if cos.get(i, float("-inf")) >= threshold
              or (i in lexical and _lexical_coverage(q_tokens, meta[i][0]) >= _LEXICAL_GATE)}

    if rerank and rel:
        top = sorted(rel, key=lambda i: (-rel[i], i))[:rerank_top_n]
        try:
            raw = _rerank_scores(query, [meta[i][0] for i in top], base_url=rerank_url)
        except Exception as e:
            _warn(f"rerank unavailable, falling back to hybrid recall: {e}")
            raw = None
        if raw is not None:
            gate = threshold if rerank_threshold is None else rerank_threshold
            survivors = []
            for i, s in zip(top, raw):
                if s >= gate:
                    passed.add(i)
                    survivors.append((i, s))
                else:
                    passed.discard(i)
            if survivors:
                if all(0.0 <= s <= 1.0 for _i, s in survivors):
                    scaled = {i: s for i, s in survivors}     # already a probability (Qwen3-Reranker)
                else:                                         # unbounded logits: rescale survivors only
                    lo = min(s for _i, s in survivors)
                    span = (max(s for _i, s in survivors) - lo) or 1.0
                    scaled = {i: (s - lo) / span if len(survivors) > 1 else 1.0 for i, s in survivors}
                rel.update(scaled)
    return [{"id": i, "content": meta[i][0], "score": _blend(rel[i], i)} for i in rel if i in passed]


def recall(query: str, db_path: Path, k: int = 5, threshold: float = 0.35,
           owner: str = "local", scope: str = None, weights: dict = None,
           type_weights: dict = None, half_lives: dict = None, type_filter: str = None,
           retrieval: str = "dense", rrf_k: int = 60,
           rerank: bool = False, rerank_top_n: int = 20, rerank_url: str = None,
           rerank_threshold: float = None, touch: bool = True,
           lexical_fallback: bool = True) -> list[dict]:
    """Return up to k memories matching query as {id, content, score} dicts (highest blended score
    first) and, when `touch`, bump last_used/use_count on the hits. Raises RuntimeError if the embed
    server is unreachable. Returns [] for an empty query or no candidates.

    Two steps. The GATE: only rows with real relevance evidence survive, a raw cosine >= `threshold`
    (or, in hybrid/rerank mode, a strong keyword match or a reranker score >= `rerank_threshold`).
    The non-semantic terms never count toward it, so an unrelated query returns nothing. Then the
    BLEND orders the survivors:
        score = wSemantic*relevance + wRecency*exp(-age_days/halfLife[type]) + wType*typeWeight
                + wUsage*usage + wSalience*salience
    Recency ages off max(created_at, last_used) so a re-accessed fact stops decaying, and salience
    (importance/10, set by consolidation) is a live ranking term. An owner/scope SQL prefilter keeps
    owners apart and skips superseded and forgotten rows before scoring.

    `retrieval='hybrid'` fuses dense cosine + BM25 (SQLite FTS5) via Reciprocal Rank Fusion
    (`rrf_k`) before the blend, catching lexically-exact hits a dense-only scan misses. `retrieval=
    'dense'` (**default**) is the dense-only path; it also backstops hybrid when this SQLite build lacks
    FTS5. `rerank=True` adds a cross-encoder rescoring pass over the top `rerank_top_n` fused candidates,
    so it uses the hybrid candidate path regardless of `retrieval`, and loud-fails back to hybrid
    ordering if no reranker is reachable. On a profile with no embedder (SemanticUnavailable) recall is
    keyword-only when `lexical_fallback` is set, else it raises."""
    if not query.strip():
        return []
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    tw = {**_DEFAULT_TYPE_WEIGHTS, **(type_weights or {})}
    hl = {**_DEFAULT_HALF_LIVES, **(half_lives or {})}
    now = datetime.now(timezone.utc)
    with _open(db_path) as db:
        # A vector made by a different embed model is not comparable to a fresh query vector, and with
        # two same-dimension models the mismatch is silent, so blank it to the "no vector yet" sentinel
        # here. The row still reaches keyword/FTS recall; only its bogus cosine is suppressed, until
        # `bob memory migrate --reembed`.
        fresh, fresh_params = _fresh_vec_sql(current_embed_model())
        sql = ("SELECT id, content, "
               f"CASE WHEN {fresh} THEN embedding ELSE '' END, "
               "type, created_at, use_count, salience, last_used "
               "FROM memories "
               "WHERE owner_id=? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)")
        params = [*fresh_params, owner, now.isoformat()]
        if scope is not None:
            sql += " AND (scope IS NULL OR scope = ?)"   # global rows + this project's rows
            params.append(scope)
        if type_filter:
            sql += " AND type = ?"
            params.append(type_filter)
        rows = list(db.execute(sql, params).fetchall())
        if not rows:
            return []
        try:
            q_vec = embed(query)
        except SemanticUnavailable:
            if not lexical_fallback:
                raise
            q_vec = None
        if q_vec is None or retrieval == "hybrid" or rerank:
            # rerank implies the broad-recall candidate path to rescore; no vector means keyword-only
            scored = _recall_hybrid(query, q_vec, rows, db, owner, scope, type_filter,
                                    now, w, tw, hl, threshold, k, rrf_k,
                                    rerank=rerank, rerank_top_n=rerank_top_n, rerank_url=rerank_url,
                                    rerank_threshold=rerank_threshold)
        else:
            scored = []
            for row_id, content, emb_json, mtype, created_at, use_count, salience, last_used in rows:
                try:
                    cos = cosine(q_vec, json.loads(emb_json))
                except Exception:
                    continue
                if cos < threshold:
                    continue
                # Age off the more recent of created_at / last_used: a reinforced fact stops decaying.
                age = _age_days(created_at, now)
                if last_used:
                    age = min(age, _age_days(last_used, now))
                decay = math.exp(-age / max(1.0, float(hl.get(mtype, 365))))
                usage = min((use_count or 0) / 10.0, 1.0)
                score = (w["wSemantic"] * cos + w["wRecency"] * decay
                         + w["wType"] * tw.get(mtype, 0.5) + w["wUsage"] * usage
                         + w["wSalience"] * (salience if salience is not None else 1.0))
                scored.append({"id": row_id, "content": content, "score": round(score, 4)})
        scored.sort(key=lambda x: x["score"], reverse=True)
        results = scored[:k]
        if results and touch:
            stamp = now.isoformat()
            for r in results:
                db.execute(
                    "UPDATE memories SET last_used=?, use_count=use_count+1 WHERE id=?",
                    [stamp, r["id"]],
                )
        return results


def profile_block(owner: str, db_path: Path, limit: int = 5, max_chars: int = 800) -> "str | None":
    """The once-per-session profile body: up to `limit` durable identity rows
    (type in profile/preference) for this owner, as third-person bullets, capped at ~max_chars.
    Embedding-free (a single SQL read); returns None when there's nothing to inject. The caller
    wraps this in the shared context frame. Forgotten and superseded rows are excluded."""
    with _open(db_path) as db:
        rows = db.execute(
            "SELECT content FROM memories WHERE owner_id=? AND type IN ('profile','preference') "
            "AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY pinned DESC, salience DESC, created_at DESC LIMIT ?",
            [owner, datetime.now(timezone.utc).isoformat(), int(limit)],
        ).fetchall()
    if not rows:
        return None
    lines, used = [], 0
    for (content,) in rows:
        line = f"- {content}"
        if lines and used + len(line) + 1 > max_chars:   # keep at least one; then respect the cap
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines) if lines else None


# --- Core-memory blocks (agent-editable, always-injected) -----------
# Named, size-capped strings the agent rewrites in its loop (MemGPT/Letta core memory). Kept OUT of the
# `memories` table on purpose: facts decay / dedup / supersede, a live-edited block must not. The table
# is built lazily on first use (like the FTS index) so recall-only DBs stay byte-unchanged.

def _ensure_core_blocks(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS core_blocks ("
        " owner_id TEXT NOT NULL DEFAULT 'local', scope TEXT NOT NULL DEFAULT '',"
        " name TEXT NOT NULL, content TEXT NOT NULL DEFAULT '', updated_at TEXT,"
        " PRIMARY KEY (owner_id, scope, name))")


def block_get(name: str, db_path: Path, owner: str = "local", scope: str = None) -> "str | None":
    """The current content of one named block, or None if unset. Scope None == the global block."""
    with _open(db_path) as db:
        _ensure_core_blocks(db)
        row = db.execute("SELECT content FROM core_blocks WHERE owner_id=? AND scope=? AND name=?",
                         [owner, scope or "", name]).fetchone()
        return row[0] if row else None


def block_list(db_path: Path, owner: str = "local", scope: str = None) -> dict:
    """All blocks for this owner/scope as {name: content}, name-ordered (deterministic for injection)."""
    with _open(db_path) as db:
        _ensure_core_blocks(db)
        rows = db.execute("SELECT name, content FROM core_blocks WHERE owner_id=? AND scope=? ORDER BY name",
                          [owner, scope or ""]).fetchall()
        return {name: content for name, content in rows}


def block_set(name: str, content: str, db_path: Path, owner: str = "local", scope: str = None,
              cap: int = None) -> "tuple[str, bool]":
    """Upsert block `name` to `content`. When `cap` (max chars) is set and the content exceeds it, the
    OLDEST leading chars are trimmed so the newest text is kept within the cap (append-friendly). Returns
    (stored_content, was_trimmed)."""
    trimmed = False
    if cap and cap > 0 and len(content) > cap:
        content = content[-cap:]
        trimmed = True
    with _open(db_path) as db:
        _ensure_core_blocks(db)
        db.execute(
            "INSERT INTO core_blocks (owner_id, scope, name, content, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(owner_id, scope, name) DO UPDATE SET content=excluded.content, "
            "updated_at=excluded.updated_at",
            [owner, scope or "", name, content, datetime.now(timezone.utc).isoformat()])
    return content, trimmed


# --- Conversation transcript (recall storage; page-back over dropped turns) -----------
# The full run transcript (user, assistant, AND intermediate tool turns) captured as it happens so a
# turn that compaction later drops from context is still searchable and can be paged back. This is a
# SEPARATE tier from `memories` (facts): transcript turns are voluminous and must not decay, dedup, or
# count against the facts cap. Built lazily (like the FTS index) so recall-only DBs stay byte-unchanged.
# Retention is bounded (memory.transcriptMaxRows / transcriptMaxDays), pruned as turns are appended.
_TRANSCRIPT_MAX_ROWS = 20000
_TRANSCRIPT_MAX_DAYS = 90
_TRANSCRIPT_PRUNE_EVERY = 50   # appends between retention passes

# Columns added after the table first shipped; ALTERed in when missing.
_TRANSCRIPT_COLUMNS = [
    ("embed_model", "TEXT"),   # embed role GGUF that made the vector (same stamp as memories)
    ("session_id", "TEXT"),    # shell/server session the turn belongs to (forget --session)
]


def _ensure_transcript(db) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS transcript ("
        " id INTEGER PRIMARY KEY, run_id TEXT, owner_id TEXT NOT NULL DEFAULT 'local', scope TEXT,"
        " seq INTEGER, role TEXT NOT NULL, content TEXT NOT NULL, tool_name TEXT,"
        " created_at TEXT, embedding TEXT, embed_model TEXT, session_id TEXT)")
    existing = {row[1] for row in db.execute("PRAGMA table_info(transcript)").fetchall()}
    for name, decl in _TRANSCRIPT_COLUMNS:
        if name not in existing:
            try:
                db.execute(f"ALTER TABLE transcript ADD COLUMN {name} {decl}")
            except Exception as e:   # a concurrent process added it first
                if "duplicate column" not in str(e).lower():
                    raise
    db.execute("CREATE INDEX IF NOT EXISTS idx_transcript_owner ON transcript(owner_id, scope, run_id, seq)")


def _ensure_transcript_fts(db) -> bool:
    """Lazily build an external-content FTS5 index over transcript.content (mirrors _ensure_fts). The
    lexical index is the always-present search floor: a turn captured while the embed server was down
    (no vector) still matches by keyword. Returns False if this SQLite build lacks FTS5."""
    return _ensure_fts_index(
        db, "transcript_fts",
        ["CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(content, content='transcript', content_rowid='id')",
         "CREATE TRIGGER IF NOT EXISTS transcript_fts_ai AFTER INSERT ON transcript BEGIN "
         "INSERT INTO transcript_fts(rowid, content) VALUES (new.id, new.content); END",
         "CREATE TRIGGER IF NOT EXISTS transcript_fts_ad AFTER DELETE ON transcript BEGIN "
         "INSERT INTO transcript_fts(transcript_fts, rowid, content) VALUES('delete', old.id, old.content); END"],
        "INSERT INTO transcript_fts(rowid, content) SELECT id, content FROM transcript",
    )


def prune_transcript(db_path: Path, max_rows: int = None, max_days: float = None) -> int:
    """Bound the transcript: drop turns older than `max_days`, then all but the newest `max_rows`.
    Defaults come from memory.transcriptMaxRows / transcriptMaxDays; 0 disables that bound. Returns
    the number of turns deleted."""
    max_rows = int(_mem_setting("transcriptMaxRows", _TRANSCRIPT_MAX_ROWS) if max_rows is None else max_rows)
    max_days = float(_mem_setting("transcriptMaxDays", _TRANSCRIPT_MAX_DAYS) if max_days is None else max_days)
    with _open(db_path) as db:
        _ensure_transcript(db)
        return _prune_transcript(db, max_rows, max_days)


def _prune_transcript(db, max_rows: int, max_days: float) -> int:
    deleted = 0
    if max_days and max_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_days)).isoformat()
        deleted += db.execute("DELETE FROM transcript WHERE created_at < ?", [cutoff]).rowcount
    if max_rows and max_rows > 0:
        deleted += db.execute(
            "DELETE FROM transcript WHERE id <= (SELECT id FROM transcript ORDER BY id DESC LIMIT 1 OFFSET ?)",
            [int(max_rows)]).rowcount
    return deleted


def transcript_append(run_id: str, role: str, content: str, db_path: Path, owner: str = "local",
                      scope: str = None, tool_name: str = None, embed_optional: bool = True,
                      session_id: str = None) -> int:
    """Persist one transcript turn. Embedding is best-effort by default (`embed_optional`): if the embed
    server is down, or the profile has no embedder, the turn is still stored without a vector (lexical FTS
    keeps it searchable), so capture never blocks or breaks a run. The per-run seq is assigned inside the
    INSERT itself, so concurrent appends to one run can't collide. Empty content is skipped. Returns the
    new row id (0 if skipped)."""
    if not content or not content.strip():
        return 0
    try:
        vec = embed(content)
    except SemanticUnavailable:
        vec = None
    except Exception as e:
        if not embed_optional:
            raise
        _warn(f"transcript turn stored without a vector: {e}")
        vec = None
    with _open(db_path) as db:
        _ensure_transcript(db)
        # Take the write lock before the INSERT reads MAX(seq): a deferred transaction that reads first
        # cannot upgrade once another writer commits under WAL, and fails at once with "database is
        # locked" instead of waiting out busy_timeout.
        if db.conn.in_transaction:
            db.conn.commit()
        db.execute("BEGIN IMMEDIATE")
        cur = db.execute(
            "INSERT INTO transcript (run_id, owner_id, scope, seq, role, content, tool_name, created_at, "
            "embedding, embed_model, session_id) "
            "SELECT ?, ?, ?, COALESCE(MAX(seq), -1) + 1, ?, ?, ?, ?, ?, ?, ? FROM transcript WHERE run_id=?",
            [run_id, owner, scope, role, content, tool_name, datetime.now(timezone.utc).isoformat(),
             json.dumps(vec) if vec is not None else "",
             current_embed_model() if vec is not None else None, session_id, run_id])
        new_id = cur.lastrowid
        if new_id % _TRANSCRIPT_PRUNE_EVERY == 0:
            _prune_transcript(db, int(_mem_setting("transcriptMaxRows", _TRANSCRIPT_MAX_ROWS)),
                              float(_mem_setting("transcriptMaxDays", _TRANSCRIPT_MAX_DAYS)))
        return new_id


def _transcript_bm25_ids(db, query, owner, scope, limit) -> list:
    match = _fts_match_query(query)
    if not match:
        return []
    sql = ("SELECT f.rowid FROM transcript_fts f JOIN transcript t ON t.id = f.rowid "
           "WHERE transcript_fts MATCH ? AND t.owner_id = ?")
    params = [match, owner]
    if scope is not None:
        sql += " AND (t.scope IS NULL OR t.scope = ?)"
        params.append(scope)
    sql += " ORDER BY f.rank, f.rowid LIMIT ?"
    params.append(limit)
    try:
        return [r[0] for r in db.execute(sql, params).fetchall()]
    except Exception:
        return []


def transcript_search(query: str, db_path: Path, owner: str = "local", scope: str = None,
                      k: int = 5, rrf_k: int = 60) -> list[dict]:
    """Hybrid (dense + BM25) search over the persisted transcript of every run for one owner/scope
    (not only the current conversation). Returns up to k {run_id, seq, role, tool_name, content,
    created_at} dicts, best match first (recency breaks ties). Owner/scope-prefiltered (no cross-owner
    leak). Lexical-only when the embed server is unreachable or a row has no comparable vector, so a
    page-back still works offline."""
    if not query.strip():
        return []
    with _open(db_path) as db:
        _ensure_transcript(db)
        try:
            q_vec = embed(query)
        except Exception:
            q_vec = None            # embed server down or no embedder -> lexical-only search
        fresh, fresh_params = _fresh_vec_sql(current_embed_model())
        emb_col = f"CASE WHEN {fresh} THEN embedding ELSE '' END" if q_vec is not None else "''"
        sql = (f"SELECT id, run_id, seq, role, content, tool_name, created_at, {emb_col} "
               "FROM transcript WHERE owner_id=?")
        params = [*(fresh_params if q_vec is not None else []), owner]
        if scope is not None:
            sql += " AND (scope IS NULL OR scope = ?)"
            params.append(scope)
        rows = list(db.execute(sql, params).fetchall())
        if not rows:
            return []
        meta, cos = {}, {}
        for rid_, run_id, seq, role, content, tool_name, created_at, emb in rows:
            meta[rid_] = {"run_id": run_id, "seq": seq, "role": role, "content": content,
                          "tool_name": tool_name, "created_at": created_at}
            if q_vec is not None and emb:
                try:
                    cos[rid_] = cosine(q_vec, json.loads(emb))
                except Exception:
                    pass
        per_list = max(k * 5, 25)
        dense_ranked = sorted(cos, key=lambda i: (-cos[i], i))[:per_list]
        bm25_ranked = (_transcript_bm25_ids(db, query, owner, scope, per_list)
                       if _ensure_transcript_fts(db) else [])
    if not dense_ranked and not bm25_ranked:
        return []
    rrf = {}
    for rank, i in enumerate(dense_ranked):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
    for rank, i in enumerate(bm25_ranked):
        if i in meta:
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
    ranked = sorted(rrf, key=lambda i: (-rrf[i], meta[i]["created_at"] or "", i))[:k]
    return [{k2: v for k2, v in meta[i].items()} for i in ranked]


# --- Hygiene + inspect/edit surface ----------------------------------

def list_memories(db_path: Path, owner: str = "local", type_filter: str = None,
                  limit: int = 50, include_inactive: bool = False) -> list[dict]:
    """Rows for one owner (active by default: not superseded, not expired). For `bob memory list`."""
    sql = "SELECT id, content, type, salience, pinned, created_at, source FROM memories WHERE owner_id=?"
    params = [owner]
    if not include_inactive:
        sql += " AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)"
        params.append(datetime.now(timezone.utc).isoformat())
    if type_filter:
        sql += " AND type=?"
        params.append(type_filter)
    sql += " ORDER BY pinned DESC, created_at DESC LIMIT ?"
    params.append(int(limit))
    keys = ["id", "content", "type", "salience", "pinned", "created_at", "source"]
    with _open(db_path) as db:
        return [dict(zip(keys, r)) for r in db.execute(sql, params).fetchall()]


def get_memory(mem_id: int, db_path: Path) -> "dict | None":
    cols = ("id, content, type, owner_id, scope, tags, salience, pinned, source, source_session, "
            "created_at, updated_at, last_used, use_count, superseded_by, expires_at")
    with _open(db_path) as db:
        r = db.execute(f"SELECT {cols} FROM memories WHERE id=?", [mem_id]).fetchone()
    return dict(zip([c.strip() for c in cols.split(",")], r)) if r else None


def export_memories(db_path: Path, owner: str = None) -> list[dict]:
    keys = ["id", "content", "type", "owner_id", "scope", "tags", "salience", "pinned", "source",
            "source_session", "created_at"]
    sql = "SELECT " + ", ".join(keys) + " FROM memories"
    params: list = []
    if owner:
        sql += " WHERE owner_id=?"
        params.append(owner)
    sql += " ORDER BY id"
    with _open(db_path) as db:
        return [dict(zip(keys, r)) for r in db.execute(sql, params).fetchall()]


def forget(mem_id: int, db_path: Path) -> bool:
    """Soft-delete: mark a memory expired so recall, the profile block and dedup skip it, but keep the
    row (audit/export)."""
    stamp = datetime.now(timezone.utc).isoformat()
    with _open(db_path) as db:
        cur = db.execute("UPDATE memories SET expires_at=?, updated_at=? WHERE id=?", [stamp, stamp, mem_id])
        return cur.rowcount > 0


# forget --query acts on a single best match, so it needs stronger evidence than recall does.
_FORGET_MIN_COSINE = 0.5


def forget_candidates(query: str, db_path: Path, owner: str = "local", threshold: float = 0.5,
                      k: int = 1) -> list:
    """The memories `forget --query` would hide: dense recall gated on a raw cosine of at least
    max(threshold, _FORGET_MIN_COSINE), without bumping use counts. Raises RuntimeError when there is
    no embedder (a keyword match is too weak a basis for deleting something)."""
    return recall(query, db_path, k=k, threshold=max(float(threshold), _FORGET_MIN_COSINE), owner=owner,
                  retrieval="dense", touch=False, lexical_fallback=False)


def forget_by_query(query: str, db_path: Path, owner: str = "local", threshold: float = 0.5) -> list:
    """Soft-delete the best-matching active memory for a query. Returns the forgotten ids (0 or 1)."""
    ids = [h["id"] for h in forget_candidates(query, db_path, owner=owner, threshold=threshold)]
    for i in ids:
        forget(i, db_path)
    return ids


def forget_by_session(session_id: str, db_path: Path, owner: str = "local") -> int:
    """Soft-delete every active memory a given session produced (provenance-based forget).
    Rows stay for audit/export; recall skips them. Returns the count hidden. The session's
    transcript turns are removed separately by forget_transcript_session."""
    stamp = datetime.now(timezone.utc).isoformat()
    with _open(db_path) as db:
        cur = db.execute(
            "UPDATE memories SET expires_at=?, updated_at=? "
            "WHERE owner_id=? AND source_session=? AND expires_at IS NULL",
            [stamp, stamp, owner, session_id])
        return cur.rowcount


def forget_transcript_session(session_id: str, db_path: Path, owner: str = "local") -> int:
    """Hard-delete a session's transcript turns (raw conversation text is not kept for audit).
    Returns the count deleted; 0 when the DB has no transcript yet."""
    with _open(db_path) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='transcript'").fetchone():
            return 0
        _ensure_transcript(db)
        return db.execute("DELETE FROM transcript WHERE owner_id=? AND session_id=?",
                          [owner, session_id]).rowcount


def edit(mem_id: int, new_content: str, db_path: Path) -> "int | None":
    """Soft-update: insert a re-embedded replacement (inheriting type/owner/scope/tags/salience/pinned)
    and point the old row's superseded_by at it. The old row stays for audit; recall sees only the new
    one. The new vector is stamped with the current embed model; with no embedder the replacement is
    stored vectorless. Returns the new id, or None if mem_id is unknown."""
    with _open(db_path) as db:
        old = db.execute(
            "SELECT type, owner_id, scope, tags, salience, pinned, embed_context FROM memories WHERE id=?",
            [mem_id]).fetchone()
        if not old:
            return None
        mtype, owner, scope, tags, salience, pinned, context = old
        normalized = (_normalize_third_person(new_content) if _is_user_memory(owner, mtype)
                      else new_content.strip())
        try:
            vec = embed(f"{context}\n{normalized}" if context else normalized)
        except SemanticUnavailable:
            vec = None
        stamp = datetime.now(timezone.utc).isoformat()
        cur = db.execute(
            "INSERT INTO memories (content, content_hash, embedding, embed_model, embed_context, type, "
            "subject, owner_id, scope, tags, salience, pinned, source, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [normalized, _content_hash(normalized), json.dumps(vec) if vec is not None else "",
             current_embed_model() if vec is not None else None, context, mtype, "user", owner, scope,
             tags, salience, pinned, "edit", stamp])
        new_id = cur.lastrowid
        db.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?", [new_id, stamp, mem_id])
        return new_id


def set_pinned(mem_id: int, db_path: Path, pinned: bool) -> bool:
    """Pin/unpin a memory. Pinned rows are never TTL/size-pruned and rank first in the
    profile block. Returns False if mem_id is unknown."""
    with _open(db_path) as db:
        cur = db.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=?",
                         [1 if pinned else 0, datetime.now(timezone.utc).isoformat(), mem_id])
        return cur.rowcount > 0


def prune(db_path: Path, owner: str = "local", forget_after_days: dict = None,
          max_rows: int = 2000) -> dict:
    """Hygiene: hard-delete rows past their per-type TTL, then enforce a per-owner size cap by
    dropping the lowest-salience/oldest rows. NEVER removes pinned rows or type in (profile, preference).
    Run opportunistically at end of consolidation. Returns {'ttl_pruned', 'capped'}."""
    now = datetime.now(timezone.utc)
    ttl_pruned = 0
    with _open(db_path) as db:
        for mtype, days in (forget_after_days or {}).items():
            if mtype in ("profile", "preference"):
                continue   # identity never TTL-expires
            # Compare PARSED datetimes, not raw strings: store() writes ISO 'T' timestamps but
            # SQLite's column default writes a space-separated form, so a naive string compare against an
            # ISO cutoff over-prunes legacy rows (space < 'T' at the date/time boundary).
            rows = db.execute(
                "SELECT id, created_at FROM memories WHERE owner_id=? AND type=? AND pinned=0",
                [owner, mtype]).fetchall()
            for rid, created_at in rows:
                if _age_days(created_at, now) > float(days):
                    db.execute("DELETE FROM memories WHERE id=?", [rid])
                    ttl_pruned += 1
        capped = 0
        # The live size cap counts and drops ACTIVE rows only; superseded/expired rows are already
        # inactive and must not inflate the count or be chosen as victims.
        active = "superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)"
        now_iso = now.isoformat()
        total = db.execute(
            f"SELECT COUNT(*) FROM memories WHERE owner_id=? AND {active}", [owner, now_iso]).fetchone()[0]
        if max_rows and total > max_rows:
            victims = db.execute(
                f"SELECT id FROM memories WHERE owner_id=? AND {active} AND pinned=0 "
                "AND type NOT IN ('profile','preference') ORDER BY salience ASC, created_at ASC LIMIT ?",
                [owner, now_iso, total - max_rows]).fetchall()
            for (vid,) in victims:
                db.execute("DELETE FROM memories WHERE id=?", [vid])
            capped = len(victims)
    return {"ttl_pruned": ttl_pruned, "capped": capped}


def clear_all(db_path: Path) -> dict:
    """Delete everything memory holds: facts, core-memory blocks and the conversation transcript, and
    empty their FTS indexes. Returns {table: rows_deleted} for the tables that exist."""
    counts = {}
    with _open(db_path) as db:
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table in ("memories", "core_blocks", "transcript"):
            if table in present:
                counts[table] = db.execute(f"DELETE FROM {table}").rowcount
        # The delete triggers already emptied them; 'delete-all' also covers an index whose triggers
        # were never installed.
        for fts in ("memories_fts", "transcript_fts"):
            if fts in present:
                db.execute(f"INSERT INTO {fts}({fts}) VALUES('delete-all')")
    return counts


def cmd_store(text: str, source: str, db_path: Path, mem_type: str = "fact") -> None:
    try:
        mid, is_new = store(text, db_path, source=source, mem_type=mem_type)
    except RuntimeError as e:
        print(f"Cannot store memory — {e}", file=sys.stderr)
        return
    print(f"Stored memory (id={mid})" if is_new else f"Already stored (similar entry id={mid})")


def cmd_recall(query: str, top: int, threshold: float, db_path: Path, type_filter: str = None) -> None:
    try:
        results = recall(query, db_path, k=top, threshold=threshold, type_filter=type_filter)
    except RuntimeError as e:
        print(f"Cannot recall — {e}", file=sys.stderr)
        print("[]")
        return
    print(json.dumps(results, ensure_ascii=False))


def cmd_list(db_path: Path, type_filter: str, limit: int, owner: str, include_inactive: bool) -> None:
    rows = list_memories(db_path, owner=owner, type_filter=type_filter, limit=limit,
                         include_inactive=include_inactive)
    if not rows:
        print("No memories.")
        return
    for r in rows:
        pin = "*" if r["pinned"] else " "
        print(f"{r['id']:>4} {pin} [{r['type']:<10}] {r['content'][:70]}")


def cmd_show(mem_id: int, db_path: Path) -> None:
    m = get_memory(mem_id, db_path)
    if not m:
        print(f"No memory id={mem_id}")
        return
    for key, value in m.items():
        print(f"{key:>14}: {value}")


def _confirm(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_forget(mem_id, query: str, db_path: Path, owner: str, session: str = None,
               yes: bool = False) -> None:
    try:
        if session:
            n = forget_by_session(session, db_path, owner=owner)
            t = forget_transcript_session(session, db_path, owner=owner)
            print(f"Forgot {n} memory(ies) and {t} transcript turn(s) from session {session}." if n or t else
                  f"No memories from session {session}.")
        elif query:
            hits = forget_candidates(query, db_path, owner=owner)
            if not hits:
                print("No matching memory to forget.")
                return
            for h in hits:
                print(f"{h['id']:>4}  {h['content'][:100]}")
            if not yes and not _confirm("Forget this memory? [y/N] "):
                print("Aborted.")
                return
            ids = [h["id"] for h in hits if forget(h["id"], db_path)]
            print(f"Forgot {ids}")
        elif mem_id is not None:
            print("Forgotten." if forget(mem_id, db_path) else f"No memory id={mem_id}")
        else:
            print("Provide an id, --query, or --session.", file=sys.stderr)
    except RuntimeError as e:
        print(f"Cannot forget — {e}", file=sys.stderr)


def cmd_edit(mem_id: int, new_text: str, db_path: Path) -> None:
    try:
        new_id = edit(mem_id, new_text, db_path)
    except RuntimeError as e:
        print(f"Cannot edit — {e}", file=sys.stderr)
        return
    print(f"Edited: new id={new_id} (old {mem_id} superseded)." if new_id else f"No memory id={mem_id}")


def cmd_pin(mem_id: int, db_path: Path, pinned: bool) -> None:
    ok = set_pinned(mem_id, db_path, pinned)
    verb = "Pinned" if pinned else "Unpinned"
    print(f"{verb} id={mem_id}." if ok else f"No memory id={mem_id}")


def cmd_export(db_path: Path, owner: str) -> None:
    print(json.dumps(export_memories(db_path, owner=owner), ensure_ascii=False, indent=2))


def cmd_status(db_path: Path) -> None:
    with _open(db_path) as db:
        count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        last_row = db.execute("SELECT created_at FROM memories ORDER BY id DESC LIMIT 1").fetchone()
        # Breakdown by type of the active rows.
        by_type = db.execute(
            "SELECT type, COUNT(*) FROM memories WHERE superseded_by IS NULL "
            "AND (expires_at IS NULL OR expires_at > ?) GROUP BY type ORDER BY type",
            [datetime.now(timezone.utc).isoformat()],
        ).fetchall()
    size_kb = db_path.stat().st_size / 1024 if db_path.exists() else 0
    last_stored = last_row[0] if last_row else "none"
    print(f"DB:           {db_path}")
    print(f"Size:         {size_kb:.1f} KB")
    print(f"Memories:     {count}")
    print(f"Last stored:  {last_stored}")
    if not semantic_available():
        print("Semantic:     off (the active profile has no embed role; keyword recall only)")
    if by_type:
        print("By type:")
        for mtype, n in by_type:
            print(f"  {mtype}: {n}")


def cmd_clear(yes: bool, db_path: Path) -> None:
    if not yes:
        ans = input("Delete ALL memories, core-memory blocks and the conversation transcript? "
                    "This cannot be undone. Type 'yes' to confirm: ")
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return
    counts = clear_all(db_path)
    detail = ", ".join(f"{n} {t}" for t, n in counts.items())
    print(f"Memory cleared ({detail})." if detail else "Memory cleared.")


# --- Consolidation ---------------------------------------------------
_SUMMARY_SYSTEM = (
    "Summarize the following conversation into 2-5 bullet points capturing key facts, decisions, "
    "or preferences expressed by the user. Be concise."
)
# Reconcile, not just extract. The model is shown the owner's existing durable facts (by id)
# and tags each new fact NEW or REPLACES:<id> so contradictions supersede instead of piling up.
_CONSOLIDATE_SYSTEM = (
    "From the conversation, extract 0-5 DURABLE facts about the USER worth remembering for future "
    "sessions (identity, stable preferences, projects, tools). Write each on its own line as a "
    "third-person statement prefixed with a type and a colon, then a ' | ' NEW/REPLACES tag, then a "
    "' | ' importance score from 1 (mundane) to 10 (core identity / long-term goal):\n"
    "preference: User prefers dark mode | NEW | 5\n"
    "preference: User uses vscode | REPLACES:12 | 6\n"
    "profile: User's name is Siva | NEW | 10\n"
    "Allowed types: profile, preference, project, fact. You are given the user's EXISTING saved "
    "facts (each with a numeric id). For every fact you extract, decide whether it is brand NEW or "
    "whether it REPLACES an existing fact because it updates or contradicts it — use REPLACES:<id> "
    "ONLY for a direct supersede (a changed preference, an updated status); when unsure, use NEW. "
    "Omit greetings, small talk, and anything ephemeral. If nothing durable was shared, return an "
    "empty response."
)
_CONSOLIDATE_TYPES = {"profile", "preference", "project", "fact"}
_AUTO_PIN_IMPORTANCE = 9   # consolidation pins a profile fact at/above this importance


def _build_reconcile_prompt(existing: list) -> str:
    """The consolidation system prompt with the owner's existing durable facts appended (as
    `<id>: <content>` lines) so the model can tag each extracted fact NEW or REPLACES:<id>."""
    if existing:
        block = "\n".join(f"{rid}: {content}" for rid, _mtype, content in existing)
        return f"{_CONSOLIDATE_SYSTEM}\n\nEXISTING FACTS:\n{block}"
    return f"{_CONSOLIDATE_SYSTEM}\n\nEXISTING FACTS: (none yet)"


def _is_local_role(model: str) -> bool:
    """True if `model` is a role served by the local llama-swap (it takes llama-server's
    `enable_thinking` chat-template kwarg) rather than a cloud peer. Unknown registry: assume local,
    the common path. Mirrors bob_loop._is_local_model."""
    reg = _registry()
    return True if reg is None else model in reg["roles"]


_SUMMARY_MARGIN_TOKENS = 128


def _fit_turns(convo: list, max_chars: int) -> list:
    """The most recent turns whose JSON fits in max_chars. When even the newest turn alone is too big,
    it is kept with its content cut (head + tail) to fit, so there is always something to summarize."""
    kept, used = [], 2   # the enclosing [] of the JSON array
    for m in reversed(convo):
        size = len(json.dumps(m)) + 2
        if used + size > max_chars:
            break
        kept.append(m)
        used += size
    if not kept:
        last = dict(convo[-1])
        overhead = len(json.dumps({**last, "content": ""})) + 16
        last["content"] = _fit_text(str(last.get("content", "")), max(64, max_chars - overhead))
        kept = [last]
    return list(reversed(kept))


def summarize_turns(turns: list, model: str = "chat", system_prompt: str = None,
                    max_tokens: int = 256, timeout: int = 60) -> str:
    """One LLM summarization call over a list of message dicts; returns the text ("" on failure or
    no usable turns). The shared summarizer core: consolidation and context compaction both call this
    instead of re-implementing the LLM plumbing. Never raises (best-effort); a failure, or an empty
    answer cut off by the token cap, is logged. `timeout` bounds the call so an end-of-session
    consolidation can't stall exit indefinitely.

    The input is fitted to the role's context (ctx - max_tokens - the system prompt - a margin),
    keeping the most recent turns, so a long session on a 4096-ctx model is summarized instead of
    rejected. Local roles get thinking switched off: a reasoning model would otherwise spend max_tokens
    thinking and return empty content."""
    _require_deps()
    convo = [m for m in turns if m.get("role") in ("user", "assistant")]
    if not convo:
        return ""
    system = system_prompt or _SUMMARY_SYSTEM
    ctx, _ub = _role_limits(model)
    if ctx:
        budget_tokens = ctx - int(max_tokens) - _SUMMARY_MARGIN_TOKENS - int(len(system) / _CHARS_PER_TOKEN)
        convo = _fit_turns(convo, int(max(256, budget_tokens) * _CHARS_PER_TOKEN))
    payload = {"model": model, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": json.dumps(convo)}]}
    if _is_local_role(model):
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    try:
        choice = _post_json("chat/completions", payload, timeout=timeout)["choices"][0]
        content = (choice.get("message") or {}).get("content")
    except (RuntimeError, KeyError, IndexError, TypeError, AttributeError) as e:
        _warn(f"memory summarizer call failed (model={model}): {e}")
        return ""
    text = (content or "").strip() if isinstance(content, str) else ""
    finish = choice.get("finish_reason") if isinstance(choice, dict) else None
    if not text and finish not in (None, "stop"):
        _warn(f"memory summarizer returned no content (model={model}, finish_reason={finish})")
    return text


def _parse_typed_bullets(text: str) -> "list[tuple[str, str]]":
    """Parse the consolidation output into (type, statement) pairs. Lenient: a leading bullet glyph is
    stripped; a recognized `type:` prefix sets the type, otherwise the whole line is a 'fact'."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line:
            continue
        if ":" in line:
            head, rest = line.split(":", 1)
            t, stmt = head.strip().lower(), rest.strip()
            if t in _CONSOLIDATE_TYPES and stmt:
                out.append((t, stmt))
                continue
        out.append(("fact", line))
    return out


def _parse_reconciled_bullets(text: str) -> "list[tuple[str, str, int | None, int | None]]":
    """Parse reconciliation output into (type, statement, replaces_id, importance) quads.
    Line: `<type>: <statement> | <NEW|REPLACES:id> | <importance 1-10>`. The `|`-delimited metadata
    tokens (NEW, REPLACES:<id>, a bare 1-10 int) are consumed from the RIGHT and are order-
    independent; parsing stops at the first token that isn't recognized metadata, so a statement
    containing a literal '|' survives intact. Missing tag → NEW (conservative); missing/garbled
    importance → None. The pre-':' part reuses the same lenient type detection as _parse_typed_bullets."""
    out = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if not line:
            continue
        parts = line.split("|")
        replaces, importance = None, None
        while len(parts) > 1:
            tok = parts[-1].strip()
            up = tok.upper()
            if up == "NEW":
                parts.pop()
            elif up.startswith("REPLACES:"):
                try:
                    replaces = int(up.split(":", 1)[1].strip())
                except ValueError:
                    replaces = None            # bad id → treat as NEW
                parts.pop()
            elif tok.isdigit() and 1 <= int(tok) <= 10 and importance is None:
                importance = int(tok)
                parts.pop()
            else:
                break                          # not metadata → the rest is the statement
        stmt = "|".join(parts).strip()
        mtype = "fact"
        if ":" in stmt:
            head, rest = stmt.split(":", 1)
            head_l = head.strip().lower()
            if head_l in _CONSOLIDATE_TYPES and rest.strip():
                mtype, stmt = head_l, rest.strip()
        if stmt:
            out.append((mtype, stmt, replaces, importance))
    return out


def _active_durable_facts(db, owner: str, limit: int, scope: str = None) -> list:
    """Top-K active durable facts for the reconciliation prompt: owner-scoped, not
    superseded/expired, excluding episodic recaps. Global rows plus this project's scope. Ordered
    pinned, then salience, then recency. Returns (id, type, content) rows."""
    now = datetime.now(timezone.utc).isoformat()
    sql = ("SELECT id, type, content FROM memories WHERE owner_id=? AND type != 'episodic' "
           "AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)")
    params: list = [owner, now]
    if scope is not None:
        sql += " AND (scope IS NULL OR scope = ?)"
        params.append(scope)
    sql += " ORDER BY pinned DESC, salience DESC, created_at DESC LIMIT ?"
    params.append(int(limit))
    return db.execute(sql, params).fetchall()


def _prose_recap(raw: str, convo: list) -> str:
    """A prose episodic recap. Strip the `type:`/`| tag` scaffolding off the extracted facts into
    plain sentences; if extraction produced nothing (LLM down or nothing durable), fall back to a
    deterministic recap (turn count + first user line) so a session is never silently dropped."""
    if raw and raw.strip():
        stmts = [stmt for _mtype, stmt, _rep, _imp in _parse_reconciled_bullets(raw)]
        if stmts:
            return "Session recap: " + " ".join(s if s.endswith(".") else s + "." for s in stmts)
    first_user = next((str(m.get("content", "")) for m in convo if m.get("role") == "user"), "")
    return f"Session recap: {len(convo)} turn(s); opened with: {first_user[:200]}".strip()


def consolidate_session(turns: list, db_path: Path, model: str = "chat", owner: str = "local",
                        dedup_threshold: float = 0.92, timeout: int = 60,
                        scope: str = None, reconcile_top_k: int = 20,
                        max_tokens: int = 512, source_session: str = None) -> dict:
    """Extract durable typed facts from a session's turns and RECONCILE them against the
    owner's existing facts (supersede, don't accumulate), plus one prose episodic recap. Returns
    {'facts': n_new, 'superseded': n, 'summary': text}. No-op (facts=0) for <2 turns.

    One LLM call: the owner's top-K active durable facts are fed into the extraction prompt so the
    model tags each fact NEW or REPLACES:<id> and rates its importance 1-10 (→ salience).
    REPLACES invalidates the old row via superseded_by (kept for audit, like edit()); ambiguous →
    NEW (conservative). A very-high-importance profile fact is auto-pinned (survives prune, ranks
    top). `scope` tags extracted type='project' facts to the project; other types stay
    global. The episodic recap is real prose with a deterministic fallback when the summarizer
    returns nothing. Idempotent: re-running the same turns dedups new facts to 0. Called in-process
    by the interactive shell /exit hook, the agent server on session delete, and the CLI — no temp file, no
    subprocess (CONTRIBUTING §2)."""
    convo = [m for m in turns if m.get("role") in ("user", "assistant")]
    if len(convo) < 2:
        return {"facts": 0, "superseded": 0, "summary": None}
    with _open(db_path) as db:
        existing = _active_durable_facts(db, owner, reconcile_top_k, scope=scope)
    valid_ids = {row[0] for row in existing}
    # max_tokens must clear the reasoning budget too: reasoning models spend it on hidden thinking
    # before the answer, so a tight cap (256) yields an empty completion (finish_reason=length).
    raw = summarize_turns(convo, model=model, system_prompt=_build_reconcile_prompt(existing),
                          max_tokens=max_tokens, timeout=timeout)
    stored, superseded = 0, 0
    supersede_pairs: list = []
    pin_ids: list = []
    if raw:
        for mtype, stmt, replaces, importance in _parse_reconciled_bullets(raw):
            # Importance 1-10 → salience 0.1-1.0 (default 1.0 when the model omits it).
            salience = (importance / 10.0) if importance else 1.0
            # embed_optional: with the embed server down the fact still persists (keyword-searchable,
            # vector filled in by `migrate --reembed`) instead of the session's facts being dropped.
            try:
                new_id, is_new = store(stmt, db_path, source="consolidation", mem_type=mtype,
                                       owner=owner, scope=(scope if mtype == "project" else None),
                                       salience=salience, dedup_threshold=dedup_threshold,
                                       source_session=source_session, embed_optional=True)
            except Exception as e:
                _warn(f"consolidation could not store a fact: {e}")
                continue
            stored += 1 if is_new else 0
            # Only supersede a real, still-active existing row, and never point a row at itself
            # (store() may have deduped the "new" fact back onto the row we were asked to replace).
            if replaces is not None and replaces in valid_ids and replaces != new_id:
                supersede_pairs.append((new_id, replaces))
            # Auto-pin a core-identity profile fact so hygiene never prunes it.
            if is_new and mtype == "profile" and importance and importance >= _AUTO_PIN_IMPORTANCE:
                pin_ids.append(new_id)
        # Apply the invalidations/pins AFTER all store() writes committed; the superseded row stays
        # for audit/export.
        if supersede_pairs or pin_ids:
            stamp = datetime.now(timezone.utc).isoformat()
            with _open(db_path) as db:
                for new_id, old_id in supersede_pairs:
                    cur = db.execute(
                        "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=? AND superseded_by IS NULL",
                        [new_id, stamp, old_id])
                    superseded += cur.rowcount
                for pid in pin_ids:
                    db.execute("UPDATE memories SET pinned=1, updated_at=? WHERE id=?", [stamp, pid])
    # A real prose recap (no type:/tag scaffolding), deterministic when extraction was empty,
    # so a session is never silently dropped.
    try:
        store(_prose_recap(raw, convo), db_path, source="consolidation", mem_type="episodic",
              owner=owner, dedup_threshold=dedup_threshold, source_session=source_session,
              embed_optional=True)
    except Exception as e:
        _warn(f"consolidation could not store the session recap: {e}")
    return {"facts": stored, "superseded": superseded, "summary": raw or None}


def cmd_summarize_session(messages_file: str, model: str, db_path: Path) -> None:
    """CLI/legacy-REPL entry: read the turns file and run consolidation. Kept under the old
    'summarize-session' verb; the in-memory surfaces call the core directly."""
    _require_deps()
    with open(messages_file, encoding="utf-8") as f:
        messages = json.load(f)
    result = consolidate_session(messages, db_path=db_path, model=model)
    if result["facts"] or result["summary"]:
        print(f"Session consolidated: {result['facts']} new fact(s) stored.")
    else:
        print("Not enough to consolidate.")


def cmd_init_profile(name: str, work: str, db_path: Path) -> None:
    """Seed durable identity as type='profile' memory rows — so they rank as profile
    and get injected at session start, instead of the dead `profile` key/value table nothing read."""
    facts = []
    if name:
        facts.append(f"The user's name is {name}")
    if work:
        facts.append(f"The user works on {work}")
    # embed_optional: onboarding runs during a fresh setup, before the inference/embed server is up.
    # Identity must persist anyway — profile_block injects it with a plain SQL read (no vector needed).
    try:
        stored = sum(1 for f in facts
                     if store(f, db_path, source="user", mem_type="profile", embed_optional=True)[1])
    except RuntimeError as e:
        print(f"Cannot save profile — {e}", file=sys.stderr)
        return
    print(f"Profile saved as {stored} durable memory(ies) (type=profile).")


def rebuild_vectors(db_path: Path, include_missing: bool = True, everything: bool = False) -> int:
    """Rebuild vectors: rows stamped with a different embed model (what an embed-model swap leaves
    behind), plus, with `include_missing`, rows stored without one (embed server down, or written on a
    profile with no embedder). `everything` re-embeds every row. The embed input is rebuilt exactly as
    store() made it (embed_context + content). Raises RuntimeError on the first embed failure (better
    than half a rebuild) and SemanticUnavailable when the profile has no embedder. Returns the count."""
    if not semantic_available():
        raise SemanticUnavailable("the active profile has no embed role; nothing to re-embed with")
    current = current_embed_model()
    if everything:
        where, params = "1", []
    else:
        clauses = ["(embedding != '' AND embed_model IS NOT NULL AND embed_model IS NOT ?)"]
        params = [current]
        if include_missing:
            clauses.append("embedding = ''")
        where = " OR ".join(clauses)
    now = datetime.now(timezone.utc).isoformat()
    with _open(db_path) as db:
        rows = db.execute(f"SELECT id, content, embed_context FROM memories WHERE {where}", params).fetchall()
        for rid, content, context in rows:
            vec = embed(f"{context}\n{content}" if context else content)
            db.execute("UPDATE memories SET embedding=?, embed_model=?, updated_at=? WHERE id=?",
                       [json.dumps(vec), current, now, rid])
    return len(rows)


def _backup(db_path: Path) -> "Path | None":
    """Copy the DB aside before a rewrite (CONTRIBUTING §5). WAL is checkpointed first so the copy
    holds every committed write."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    with _open(db_path) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = db_path.with_name(db_path.name + f".bak.{ts}")
    shutil.copy2(db_path, backup)
    return backup


def cmd_migrate(db_path: Path, normalize: bool = False, reembed: bool = False) -> None:
    """Schema migration always runs (via get_db). With --normalize, additionally rewrite each user
    row's content to third person (§2.3) and re-embed. With --reembed, rebuild the vectors of rows
    stamped with a different embed model or stored without one. Both back the DB up first (and need
    the embed server up), per the backup-before-rewrite posture (CONTRIBUTING §5)."""
    with _open(db_path) as db:  # triggers the schema migration ladder
        version = db.execute("PRAGMA user_version").fetchone()[0]
        missing = db.execute("SELECT COUNT(*) FROM memories WHERE embedding = ''").fetchone()[0]
    print(f"Schema at v{version}.")
    current = current_embed_model()
    stale = stale_vector_count(db_path)
    if stale:
        print(f"{stale} row(s) hold vectors from a different embed model (current: {current}); "
              "they are excluded from semantic recall until re-embedded.")
    if missing and semantic_available():
        print(f"{missing} row(s) have no vector yet; they are keyword-searchable only until re-embedded.")
    if not (normalize or reembed):
        print("Pass --normalize to rewrite content to third person, or --reembed to rebuild stale or "
              "missing vectors (both re-embed and back up first).")
        return

    backup = _backup(db_path)
    if backup:
        print(f"Backup: {backup}")

    now = datetime.now(timezone.utc).isoformat()
    if normalize:
        with _open(db_path) as db:
            rows = db.execute("SELECT id, content, owner_id, type, embed_context FROM memories").fetchall()
            changed = 0
            for rid, content, owner, mtype, context in rows:
                if not _is_user_memory(owner, mtype):
                    continue
                normalized = _normalize_third_person(content)
                if normalized == content:
                    continue
                try:
                    vec = embed(f"{context}\n{normalized}" if context else normalized)
                except SemanticUnavailable:
                    vec = None
                db.execute(
                    "UPDATE memories SET content=?, embedding=?, embed_model=?, content_hash=?, "
                    "subject='user', updated_at=? WHERE id=?",
                    [normalized, json.dumps(vec) if vec is not None else "",
                     current if vec is not None else None, _content_hash(normalized), now, rid],
                )
                changed += 1
        print(f"Normalized {changed} of {len(rows)} row(s); re-embedded.")

    if reembed:
        n = rebuild_vectors(db_path)
        print(f"Re-embedded {n} row(s) onto {current}.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="bob_memory")
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="Path to SQLite DB")
    sub = parser.add_subparsers(dest="cmd", required=True)

    _TYPE_CHOICES = ["profile", "preference", "project", "fact", "episodic"]

    p_store = sub.add_parser("store")
    p_store.add_argument("text")
    p_store.add_argument("--source", default="user")
    p_store.add_argument("--type", dest="mem_type", default="fact", choices=_TYPE_CHOICES)

    p_recall = sub.add_parser("recall")
    p_recall.add_argument("query")
    p_recall.add_argument("--top", type=int, default=5)
    p_recall.add_argument("--threshold", type=float, default=0.3)
    p_recall.add_argument("--type", dest="type_filter", default=None, choices=_TYPE_CHOICES)

    sub.add_parser("status")

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("--yes", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--type", dest="type_filter", default=None, choices=_TYPE_CHOICES)
    p_list.add_argument("--owner", default="local")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--all", dest="include_inactive", action="store_true",
                        help="Include forgotten/superseded rows")

    p_show = sub.add_parser("show")
    p_show.add_argument("id", type=int)

    p_forget = sub.add_parser("forget")
    p_forget.add_argument("id", type=int, nargs="?")
    p_forget.add_argument("--query", default=None, help="Forget the best-matching memory instead of an id")
    p_forget.add_argument("--session", default=None, help="Forget every memory a session produced")
    p_forget.add_argument("--owner", default="local")
    p_forget.add_argument("--yes", action="store_true", help="Skip the --query confirmation")

    p_edit = sub.add_parser("edit")
    p_edit.add_argument("id", type=int)
    p_edit.add_argument("text")

    p_pin = sub.add_parser("pin")
    p_pin.add_argument("id", type=int)
    p_unpin = sub.add_parser("unpin")
    p_unpin.add_argument("id", type=int)

    p_export = sub.add_parser("export")
    p_export.add_argument("--owner", default=None, help="Restrict to one owner (default: all)")

    p_profile = sub.add_parser("init-profile")
    p_profile.add_argument("--name", required=True)
    p_profile.add_argument("--work", required=True)

    p_sum = sub.add_parser("summarize-session")
    p_sum.add_argument("--messages-file", required=True, help="Path to JSON file with messages array")
    p_sum.add_argument("--model", default="chat", help="LiteLLM model role to use for summarization")

    p_migrate = sub.add_parser("migrate")
    p_migrate.add_argument("--normalize", action="store_true",
                           help="Rewrite content to third person + re-embed (backs up the DB first)")
    p_migrate.add_argument("--reembed", action="store_true",
                           help="Rebuild vectors left stale by an embed-model swap or never made (backs up first)")

    args = parser.parse_args()
    db_path = Path(args.db)

    try:  # CLI boundary — a missing optional dep prints one line + exits 1, never a traceback.
        if args.cmd == "store":
            cmd_store(args.text, args.source, db_path, mem_type=args.mem_type)
        elif args.cmd == "recall":
            cmd_recall(args.query, args.top, args.threshold, db_path, type_filter=args.type_filter)
        elif args.cmd == "status":
            cmd_status(db_path)
        elif args.cmd == "clear":
            cmd_clear(args.yes, db_path)
        elif args.cmd == "list":
            cmd_list(db_path, args.type_filter, args.limit, args.owner, args.include_inactive)
        elif args.cmd == "show":
            cmd_show(args.id, db_path)
        elif args.cmd == "forget":
            cmd_forget(args.id, args.query, db_path, args.owner, session=args.session, yes=args.yes)
        elif args.cmd == "edit":
            cmd_edit(args.id, args.text, db_path)
        elif args.cmd == "pin":
            cmd_pin(args.id, db_path, True)
        elif args.cmd == "unpin":
            cmd_pin(args.id, db_path, False)
        elif args.cmd == "export":
            cmd_export(db_path, args.owner)
        elif args.cmd == "init-profile":
            cmd_init_profile(args.name, args.work, db_path)
        elif args.cmd == "summarize-session":
            cmd_summarize_session(args.messages_file, args.model, db_path)
        elif args.cmd == "migrate":
            cmd_migrate(db_path, normalize=args.normalize, reembed=args.reembed)
    except RuntimeError as e:
        print(f"bob memory: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
