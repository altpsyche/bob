"""Bob memory: store/recall via SQLite + BGE-M3 embeddings.

Usage:
  bob_memory.py [--db PATH] store "text" [--source user|session]
  bob_memory.py [--db PATH] recall "query" [--top 5] [--threshold 0.3]
  bob_memory.py [--db PATH] status
  bob_memory.py [--db PATH] clear [--yes]
  bob_memory.py [--db PATH] init-profile --name "Siva" --work "game dev"

Runs inside venv-litellm (has requests). Requires: sqlite-utils.
Embed endpoint resolved from config (litellmPort); BGE-M3, model=embed.
"""

# Lazy annotations so `-> sqlite_utils.Database` doesn't evaluate (and need the import) at def time.
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
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
EMBED_MODEL = "embed"

# --- Schema (v2 typed/owner-scoped; v3 provenance) ---------------
# `get_db` migrates a legacy DB in place (additive ALTERs + one-time backfill), gated by PRAGMA
# user_version so the common path is a cheap version read. Migrations run as an incremental ladder
# (v1→v2→v3), each step idempotent and column-presence-guarded.
SCHEMA_VERSION = 3

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

    If config.json isn't readable yet (missing or corrupt), return the central default WITHOUT
    memoizing — so a later call re-reads once the real config exists, instead of poisoning the
    process with the sk-local fallback."""
    cached = _LITELLM.get("v")
    if cached is not None:
        return cached
    import bob_core
    try:
        cfg = bob_core.load_config()
    except Exception:  # FileNotFoundError, JSONDecodeError, ... — fall back but don't cache it
        base = f"http://localhost:{bob_core._PORT_DEFAULTS['litellmPort']}/v1"
        return base, {"Authorization": "Bearer sk-local"}
    val = (
        f"http://localhost:{bob_core._port(cfg, 'litellmPort')}/v1",
        {"Authorization": f"Bearer {bob_core._litellm_key(cfg)}"},
    )
    _LITELLM["v"] = val
    return val


def get_db(db_path) -> sqlite_utils.Database:
    _require_deps()
    db_path = Path(db_path)  # accept str (e.g. bob_core._get_db_path) or Path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite_utils.Database(db_path)
    _ensure_schema(db)
    return db


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
            source_session TEXT
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


def embed(text: str) -> list[float]:
    _require_deps()
    base, headers = _litellm()
    url = f"{base}/embeddings"
    try:
        resp = requests.post(url, json={"model": EMBED_MODEL, "input": [text]}, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]
    except (requests.RequestException, KeyError, IndexError, ValueError) as e:
        raise RuntimeError(f"Embedding server unreachable or returned bad data at {url}: {e}") from e


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

def store(content: str, db_path: Path, source: str = "user", mem_type: str = "fact",
          owner: str = "local", scope: str = None, tags: str = None, salience: float = 1.0,
          dedup_threshold: float = 0.92, source_session: str = None,
          embed_optional: bool = False) -> tuple[int, bool]:
    """Insert a typed, owner-scoped memory. Returns (id, is_new).

    Content is normalized to third person (§2.3) before hashing/embedding, so recalled text never
    reads as Bob's own identity. Two-tier dedup — both return the existing id with is_new=False:
      - exact: a content_hash lookup scoped to the owner — O(1), *before* any embedding call;
      - near:  cosine >= dedup_threshold, scoped to (owner, mem_type).
    Dedup stays best-effort: read-then-insert isn't transactional — benign for a personal DB.

    Raises RuntimeError if the embed server is unreachable — UNLESS `embed_optional` is set, in which
    case the row is stored with a NULL embedding and near-dedup is skipped. That keeps durable identity
    persistable when inference isn't up yet (onboarding during a fresh setup): profile_block injection
    is a plain SQL read, so a NULL-embedding row is fully usable at session start; it just won't surface
    in *semantic* recall until re-embedded (`bob memory migrate --normalize` with the server up)."""
    normalized = _normalize_third_person(content)
    chash = _content_hash(normalized)
    db = get_db(db_path)
    # 1) exact dedup — a hash hit for the same owner short-circuits before we pay for an embedding.
    hit = db.execute(
        "SELECT id FROM memories WHERE content_hash=? AND owner_id=? AND superseded_by IS NULL",
        [chash, owner],
    ).fetchone()
    if hit:
        return hit[0], False
    # 2) near dedup — cosine over this owner's rows of the same type only (scoped, not a full scan).
    try:
        vec = embed(normalized)
    except RuntimeError:
        if not embed_optional:
            raise
        vec = None   # embed server down + caller opted in: persist without a vector, skip near-dedup
    if vec is not None:
        for eid, emb_json in db.execute(
            "SELECT id, embedding FROM memories WHERE owner_id=? AND type=? AND superseded_by IS NULL",
            [owner, mem_type],
        ).fetchall():
            try:
                if cosine(vec, json.loads(emb_json)) >= dedup_threshold:
                    return eid, False
            except Exception:
                continue
    # embedding is TEXT NOT NULL; an empty string is the "no vector yet" sentinel — every recall path
    # does json.loads(embedding) inside try/except, so a "" row is skipped by semantic recall (not
    # surfaced with a bogus 0-cosine) until re-embedded, while profile_block (a plain SQL read) still uses it.
    db["memories"].insert({
        "content": normalized,
        "content_hash": chash,
        "embedding": json.dumps(vec) if vec is not None else "",
        "type": mem_type,
        "subject": "user",
        "owner_id": owner,
        "scope": scope,
        "tags": tags,
        "salience": salience,
        "source": source,
        "source_session": source_session,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return db.execute("SELECT last_insert_rowid()").fetchone()[0], True


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


def _fts5_available(db) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts5_probe USING fts5(x)")
        db.execute("DROP TABLE IF EXISTS _fts5_probe")
        return True
    except Exception:
        return False


def _ensure_fts(db) -> bool:
    """Lazily build the external-content FTS5 index + sync triggers over memories.content and
    backfill it, once, the FIRST time hybrid recall runs. Deliberately NOT in _ensure_schema, so
    dense-mode DBs are byte-unchanged (no extra table, no per-write trigger overhead). Returns False if
    this SQLite build lacks FTS5 (caller falls back to dense)."""
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories_fts'").fetchone():
            return True
        if not _fts5_available(db):
            return False
        db.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, content='memories', content_rowid='id')")
        db.execute("CREATE TRIGGER memories_fts_ai AFTER INSERT ON memories BEGIN "
                   "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END")
        db.execute("CREATE TRIGGER memories_fts_ad AFTER DELETE ON memories BEGIN "
                   "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); END")
        db.execute("CREATE TRIGGER memories_fts_au AFTER UPDATE ON memories BEGIN "
                   "INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.id, old.content); "
                   "INSERT INTO memories_fts(rowid, content) VALUES (new.id, new.content); END")
        db.execute("INSERT INTO memories_fts(rowid, content) SELECT id, content FROM memories")
        db.conn.commit()
        return True
    except Exception:
        return False


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
                   threshold, k, rrf_k) -> list:
    """Fuse the dense (cosine) and lexical (BM25) rankings with Reciprocal Rank Fusion, then apply the
    recency/type/usage/salience terms on top of the fused candidates. Falls back to a dense scan over
    the candidate set when FTS5 is unavailable or the query has no lexical hits. `rows` already carries
    the owner/scope/active prefilter, so every candidate id resolves here."""
    meta = {}   # id -> (content, mtype, created_at, use_count, salience, last_used)
    cos = {}
    for row_id, content, emb_json, mtype, created_at, use_count, salience, last_used in rows:
        meta[row_id] = (content, mtype, created_at, use_count, salience, last_used)
        try:
            cos[row_id] = cosine(q_vec, json.loads(emb_json))
        except Exception:
            pass
    per_list = max(k * 5, 25)
    dense_ranked = sorted(cos, key=lambda i: (-cos[i], i))[:per_list]
    bm25_ranked = (_bm25_ranked_ids(db, query, owner, scope, now.isoformat(), type_filter, per_list)
                   if _ensure_fts(db) else [])

    def _blend(sem, i):
        return round(w["wSemantic"] * sem + _nonsemantic_score(w, tw, hl, *meta[i][1:], now), 4)

    if not bm25_ranked:
        # No lexical half -> dense over the candidate set (still returns [] to threshold like dense).
        return [{"id": i, "content": meta[i][0], "score": _blend(cos[i], i)}
                for i in dense_ranked if _blend(cos[i], i) >= threshold]

    rrf = {}
    for rank, i in enumerate(dense_ranked):
        rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
    for rank, i in enumerate(bm25_ranked):
        if i in meta:
            rrf[i] = rrf.get(i, 0.0) + 1.0 / (rrf_k + rank)
    max_rrf = max(rrf.values())               # normalize so the best candidate ~= a perfect cosine (1.0)
    scored = []
    for i, rscore in rrf.items():
        s = _blend(rscore / max_rrf, i)
        if s >= threshold:
            scored.append({"id": i, "content": meta[i][0], "score": s})
    return scored


def recall(query: str, db_path: Path, k: int = 5, threshold: float = 0.35,
           owner: str = "local", scope: str = None, weights: dict = None,
           type_weights: dict = None, half_lives: dict = None, type_filter: str = None,
           retrieval: str = "dense", rrf_k: int = 60) -> list[dict]:
    """Return up to k memories matching query as {id, content, score} dicts (highest blended score
    first) and bump last_used/use_count on the hits. Raises RuntimeError if the embed server is
    unreachable. Returns [] for an empty query or no candidates.

    Blended score (replaces the semantic-only cosine + magic 0.3):
        score = wSemantic*cosine + wRecency*exp(-age_days/halfLife[type]) + wType*typeWeight
                + wUsage*usage + wSalience*salience
    Recency ages off max(created_at, last_used) so a re-accessed fact stops decaying, and
    salience (importance/10, set by consolidation) is a live ranking term. An owner/scope SQL
    prefilter closes the former cross-owner leak and skips soft-deleted (superseded) and expired
    rows before scoring.

    `retrieval='hybrid'` fuses dense cosine + BM25 (SQLite FTS5) via Reciprocal Rank Fusion
    (`rrf_k`) before applying the recency/type/usage/salience terms — catching lexically-exact hits a
    dense-only scan misses. `retrieval='dense'` (**default**) is the dense-only path, byte-identical; it
    also silently backstops hybrid when this SQLite build lacks FTS5."""
    if not query.strip():
        return []
    w = {**_DEFAULT_WEIGHTS, **(weights or {})}
    tw = {**_DEFAULT_TYPE_WEIGHTS, **(type_weights or {})}
    hl = {**_DEFAULT_HALF_LIVES, **(half_lives or {})}
    db = get_db(db_path)
    now = datetime.now(timezone.utc)
    sql = ("SELECT id, content, embedding, type, created_at, use_count, salience, last_used "
           "FROM memories "
           "WHERE owner_id=? AND superseded_by IS NULL AND (expires_at IS NULL OR expires_at > ?)")
    params = [owner, now.isoformat()]
    if scope is not None:
        sql += " AND (scope IS NULL OR scope = ?)"   # global rows + this project's rows
        params.append(scope)
    if type_filter:
        sql += " AND type = ?"
        params.append(type_filter)
    rows = list(db.execute(sql, params).fetchall())
    if not rows:
        return []
    q_vec = embed(query)
    if retrieval == "hybrid":
        scored = _recall_hybrid(query, q_vec, rows, db, owner, scope, type_filter,
                                now, w, tw, hl, threshold, k, rrf_k)
    else:
        scored = []
        for row_id, content, emb_json, mtype, created_at, use_count, salience, last_used in rows:
            try:
                cos = cosine(q_vec, json.loads(emb_json))
            except Exception:
                continue
            # Age off the more recent of created_at / last_used — a reinforced fact stops decaying.
            age = _age_days(created_at, now)
            if last_used:
                age = min(age, _age_days(last_used, now))
            decay = math.exp(-age / max(1.0, float(hl.get(mtype, 365))))
            usage = min((use_count or 0) / 10.0, 1.0)
            score = (w["wSemantic"] * cos + w["wRecency"] * decay
                     + w["wType"] * tw.get(mtype, 0.5) + w["wUsage"] * usage
                     + w["wSalience"] * (salience if salience is not None else 1.0))
            if score >= threshold:
                scored.append({"id": row_id, "content": content, "score": round(score, 4)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:k]
    if results:
        stamp = now.isoformat()
        for r in results:
            db.execute(
                "UPDATE memories SET last_used=?, use_count=use_count+1 WHERE id=?",
                [stamp, r["id"]],
            )
        db.conn.commit()
    return results


def profile_block(owner: str, db_path: Path, limit: int = 5, max_chars: int = 800) -> "str | None":
    """The once-per-session profile body: up to `limit` durable identity rows
    (type in profile/preference) for this owner, as third-person bullets, capped at ~max_chars.
    Embedding-free (a single SQL read); returns None when there's nothing to inject. The caller
    wraps this in the shared context frame."""
    db = get_db(db_path)
    rows = db.execute(
        "SELECT content FROM memories WHERE owner_id=? AND type IN ('profile','preference') "
        "AND superseded_by IS NULL ORDER BY pinned DESC, salience DESC, created_at DESC LIMIT ?",
        [owner, int(limit)],
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


# --- Hygiene + inspect/edit surface ----------------------------------

def list_memories(db_path: Path, owner: str = "local", type_filter: str = None,
                  limit: int = 50, include_inactive: bool = False) -> list[dict]:
    """Rows for one owner (active by default: not superseded, not expired). For `bob memory list`."""
    db = get_db(db_path)
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
    return [dict(zip(keys, r)) for r in db.execute(sql, params).fetchall()]


def get_memory(mem_id: int, db_path: Path) -> "dict | None":
    db = get_db(db_path)
    cols = ("id, content, type, owner_id, scope, tags, salience, pinned, source, source_session, "
            "created_at, updated_at, last_used, use_count, superseded_by, expires_at")
    r = db.execute(f"SELECT {cols} FROM memories WHERE id=?", [mem_id]).fetchone()
    return dict(zip([c.strip() for c in cols.split(",")], r)) if r else None


def export_memories(db_path: Path, owner: str = None) -> list[dict]:
    db = get_db(db_path)
    keys = ["id", "content", "type", "owner_id", "scope", "tags", "salience", "pinned", "source",
            "source_session", "created_at"]
    sql = "SELECT " + ", ".join(keys) + " FROM memories"
    params: list = []
    if owner:
        sql += " WHERE owner_id=?"
        params.append(owner)
    sql += " ORDER BY id"
    return [dict(zip(keys, r)) for r in db.execute(sql, params).fetchall()]


def forget(mem_id: int, db_path: Path) -> bool:
    """Soft-delete: mark a memory expired so recall skips it, but keep the row (audit/export)."""
    db = get_db(db_path)
    stamp = datetime.now(timezone.utc).isoformat()
    cur = db.execute("UPDATE memories SET expires_at=?, updated_at=? WHERE id=?", [stamp, stamp, mem_id])
    db.conn.commit()
    return cur.rowcount > 0


def forget_by_query(query: str, db_path: Path, owner: str = "local", threshold: float = 0.5) -> list:
    """Soft-delete the best-matching active memory for a query. Returns the forgotten ids (0 or 1)."""
    hits = recall(query, db_path, k=1, threshold=threshold, owner=owner)
    ids = [h["id"] for h in hits]
    for i in ids:
        forget(i, db_path)
    return ids


def forget_by_session(session_id: str, db_path: Path, owner: str = "local") -> int:
    """Soft-delete every active memory a given session produced (provenance-based forget).
    Rows stay for audit/export; recall skips them. Returns the count hidden."""
    db = get_db(db_path)
    stamp = datetime.now(timezone.utc).isoformat()
    cur = db.execute(
        "UPDATE memories SET expires_at=?, updated_at=? "
        "WHERE owner_id=? AND source_session=? AND expires_at IS NULL",
        [stamp, stamp, owner, session_id])
    db.conn.commit()
    return cur.rowcount


def edit(mem_id: int, new_content: str, db_path: Path) -> "int | None":
    """Soft-update: insert a re-embedded replacement (inheriting type/owner/scope/tags/salience/pinned)
    and point the old row's superseded_by at it. The old row stays for audit; recall sees only the new
    one. Returns the new id, or None if mem_id is unknown."""
    db = get_db(db_path)
    old = db.execute(
        "SELECT type, owner_id, scope, tags, salience, pinned FROM memories WHERE id=?", [mem_id]
    ).fetchone()
    if not old:
        return None
    mtype, owner, scope, tags, salience, pinned = old
    normalized = _normalize_third_person(new_content)
    vec = embed(normalized)
    stamp = datetime.now(timezone.utc).isoformat()
    db["memories"].insert({
        "content": normalized, "content_hash": _content_hash(normalized), "embedding": json.dumps(vec),
        "type": mtype, "subject": "user", "owner_id": owner, "scope": scope, "tags": tags,
        "salience": salience, "pinned": pinned, "source": "edit", "created_at": stamp,
    })
    new_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute("UPDATE memories SET superseded_by=?, updated_at=? WHERE id=?", [new_id, stamp, mem_id])
    db.conn.commit()
    return new_id


def set_pinned(mem_id: int, db_path: Path, pinned: bool) -> bool:
    """Pin/unpin a memory. Pinned rows are never TTL/size-pruned and rank first in the
    profile block. Returns False if mem_id is unknown."""
    db = get_db(db_path)
    cur = db.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=?",
                     [1 if pinned else 0, datetime.now(timezone.utc).isoformat(), mem_id])
    db.conn.commit()
    return cur.rowcount > 0


def prune(db_path: Path, owner: str = "local", forget_after_days: dict = None,
          max_rows: int = 2000) -> dict:
    """Hygiene: hard-delete rows past their per-type TTL, then enforce a per-owner size cap by
    dropping the lowest-salience/oldest rows. NEVER removes pinned rows or type in (profile, preference).
    Run opportunistically at end of consolidation. Returns {'ttl_pruned', 'capped'}."""
    db = get_db(db_path)
    now = datetime.now(timezone.utc)
    ttl_pruned = 0
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
    db.conn.commit()
    return {"ttl_pruned": ttl_pruned, "capped": capped}


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


def cmd_forget(mem_id, query: str, db_path: Path, owner: str, session: str = None) -> None:
    try:
        if session:
            n = forget_by_session(session, db_path, owner=owner)
            print(f"Forgot {n} memory(ies) from session {session}." if n else
                  f"No memories from session {session}.")
        elif query:
            ids = forget_by_query(query, db_path, owner=owner)
            print(f"Forgot {ids}" if ids else "No matching memory to forget.")
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
    db = get_db(db_path)
    count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    size_kb = db_path.stat().st_size / 1024 if db_path.exists() else 0
    last_row = db.execute("SELECT created_at FROM memories ORDER BY id DESC LIMIT 1").fetchone()
    last_stored = last_row[0] if last_row else "none"
    # Breakdown by type of the active rows (replaces the dead profile key/value table).
    by_type = db.execute(
        "SELECT type, COUNT(*) FROM memories WHERE superseded_by IS NULL "
        "AND (expires_at IS NULL OR expires_at > datetime('now')) GROUP BY type ORDER BY type"
    ).fetchall()
    print(f"DB:           {db_path}")
    print(f"Size:         {size_kb:.1f} KB")
    print(f"Memories:     {count}")
    print(f"Last stored:  {last_stored}")
    if by_type:
        print("By type:")
        for mtype, n in by_type:
            print(f"  {mtype}: {n}")


def cmd_clear(yes: bool, db_path: Path) -> None:
    if not yes:
        ans = input("Delete ALL memories? This cannot be undone. Type 'yes' to confirm: ")
        if ans.strip().lower() != "yes":
            print("Aborted.")
            return
    db = get_db(db_path)
    db.execute("DELETE FROM memories")
    print("Memory cleared.")


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


def summarize_turns(turns: list, model: str = "chat", system_prompt: str = None,
                    max_tokens: int = 256, timeout: int = 60) -> str:
    """One LLM summarization call over a list of message dicts; returns the text ("" on failure or
    no usable turns). The shared summarizer core — consolidation and context compaction
    both call this instead of re-implementing the LLM plumbing. Never raises (best-effort).
    `timeout` bounds the call so an end-of-session consolidation can't stall exit indefinitely."""
    _require_deps()
    convo = [m for m in turns if m.get("role") in ("user", "assistant")]
    if not convo:
        return ""
    prompt = [{"role": "system", "content": system_prompt or _SUMMARY_SYSTEM},
              {"role": "user", "content": json.dumps(convo)}]
    base, headers = _litellm()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            json={"model": model, "messages": prompt, "max_tokens": max_tokens},
            headers=headers, timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, ValueError):
        return ""


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
    db = get_db(db_path)
    existing = _active_durable_facts(db, owner, reconcile_top_k, scope=scope)
    valid_ids = {row[0] for row in existing}
    # max_tokens must clear the reasoning budget too — reasoning models spend it on hidden thinking
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
            try:
                new_id, is_new = store(stmt, db_path, source="consolidation", mem_type=mtype,
                                       owner=owner, scope=(scope if mtype == "project" else None),
                                       salience=salience, dedup_threshold=dedup_threshold,
                                       source_session=source_session)
            except RuntimeError:
                break   # embed server down mid-run — stop; best-effort
            stored += 1 if is_new else 0
            # Only supersede a real, still-active existing row, and never point a row at itself
            # (store() may have deduped the "new" fact back onto the row we were asked to replace).
            if replaces is not None and replaces in valid_ids and replaces != new_id:
                supersede_pairs.append((new_id, replaces))
            # Auto-pin a core-identity profile fact so hygiene never prunes it.
            if is_new and mtype == "profile" and importance and importance >= _AUTO_PIN_IMPORTANCE:
                pin_ids.append(new_id)
        # Apply the invalidations/pins AFTER all store() writes committed (avoids two write
        # connections holding locks at once); the superseded row stays for audit/export.
        if supersede_pairs or pin_ids:
            stamp = datetime.now(timezone.utc).isoformat()
            for new_id, old_id in supersede_pairs:
                cur = db.execute(
                    "UPDATE memories SET superseded_by=?, updated_at=? WHERE id=? AND superseded_by IS NULL",
                    [new_id, stamp, old_id])
                superseded += cur.rowcount
            for pid in pin_ids:
                db.execute("UPDATE memories SET pinned=1, updated_at=? WHERE id=?", [stamp, pid])
            db.conn.commit()
    # A real prose recap (no type:/tag scaffolding), deterministic when extraction was empty,
    # so a session is never silently dropped.
    try:
        store(_prose_recap(raw, convo), db_path, source="consolidation", mem_type="episodic",
              owner=owner, dedup_threshold=dedup_threshold, source_session=source_session)
    except RuntimeError:
        pass
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


def cmd_migrate(db_path: Path, normalize: bool = False) -> None:
    """Schema migration always runs (via get_db). With --normalize, additionally rewrite each
    row's content to third person (§2.3) and re-embed — backs the DB up first (needs the embed
    server up), per the backup-before-rewrite posture (CONTRIBUTING §5)."""
    db = get_db(db_path)  # triggers the v1 -> v2 schema migration
    version = db.execute("PRAGMA user_version").fetchone()[0]
    print(f"Schema at v{version}.")
    if not normalize:
        print("Pass --normalize to rewrite content to third person (re-embeds; backs up first).")
        return

    db_path = Path(db_path)
    if db_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup = db_path.with_name(db_path.name + f".bak.{ts}")
        shutil.copy2(db_path, backup)
        print(f"Backup: {backup}")

    rows = db.execute("SELECT id, content FROM memories").fetchall()
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    for rid, content in rows:
        normalized = _normalize_third_person(content)
        if normalized == content:
            continue
        vec = embed(normalized)  # re-embed the rewritten text (fails loudly if the server is down)
        db.execute(
            "UPDATE memories SET content=?, embedding=?, content_hash=?, subject='user', updated_at=? "
            "WHERE id=?",
            [normalized, json.dumps(vec), _content_hash(normalized), now, rid],
        )
        changed += 1
    db.conn.commit()
    print(f"Normalized {changed} of {len(rows)} row(s); re-embedded.")


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
            cmd_forget(args.id, args.query, db_path, args.owner, session=args.session)
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
            cmd_migrate(db_path, normalize=args.normalize)
    except RuntimeError as e:
        print(f"bob memory: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
